"""Regression tests for race and resource-bound findings from MCP review."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp import Client
import pytest

from kleinanzeigen_mcp.models import JobResultItem, JobResultsPage, JobStatus
from kleinanzeigen_mcp.server import (
    MAX_MERGE_SNAPSHOT_ITEMS,
    create_mcp_server,
)


def _server_for(fake_client: Any):
    @asynccontextmanager
    async def client_factory() -> AsyncIterator[Any]:
        yield fake_client

    return create_mcp_server(client_factory)


class _CompletingDetailClient:
    """A terminal status must be observed before its final result page."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_job(self, job_id: str) -> JobStatus:
        self.calls.append("status")
        return JobStatus(
            job_id=job_id,
            type="details",
            state="completed",
            results={"available": 1},
        )

    async def get_results(
        self,
        job_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> JobResultsPage:
        assert self.calls == ["status"]
        self.calls.append("results")
        return JobResultsPage(
            job_id=job_id,
            type="details",
            items=[
                JobResultItem(
                    sequence=1,
                    listing_id="1234567890",
                    status="complete",
                    data={"title": "Final committed"},
                )
            ],
            has_more=False,
        )


@pytest.mark.asyncio
async def test_job_results_reads_status_before_page_to_close_terminal_race() -> None:
    fake = _CompletingDetailClient()
    async with Client(_server_for(fake), raise_exceptions=False) as client:
        result = await client.call_tool(
            "get_job_results",
            {"job_id": "detail-race", "limit": 200},
        )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["items"][0]["data"]["title"] == ("Final committed")
    assert result.structured_content.get("next_action") is None
    assert fake.calls == ["status", "results"]


class _OversizedMergeClient:
    def __init__(self) -> None:
        self.result_reads = 0

    async def get_job(self, job_id: str) -> JobStatus:
        return JobStatus(
            job_id=job_id,
            type="search",
            state="completed",
            results={"available": MAX_MERGE_SNAPSHOT_ITEMS + 1},
        )

    async def get_results(
        self,
        job_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> JobResultsPage:
        self.result_reads += 1
        raise AssertionError("oversized merge must fail before materialization")


@pytest.mark.asyncio
async def test_merge_rejects_unbounded_snapshot_before_reading_results() -> None:
    fake = _OversizedMergeClient()
    async with Client(_server_for(fake), raise_exceptions=False) as client:
        result = await client.call_tool(
            "merge_search_results",
            {"search_job_ids": ["too-large"]},
        )

    assert result.is_error is True
    assert result.structured_content is not None
    error = result.structured_content["error"]
    assert error["code"] == "merge_snapshot_too_large"
    assert "Weniger" in error["suggested_action"]
    assert fake.result_reads == 0
