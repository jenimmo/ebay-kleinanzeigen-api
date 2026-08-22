"""In-Memory-Abnahmetests für vollständige MCP-Tool-Workflows."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp import Client
import pytest

from kleinanzeigen_mcp.client import KleinanzeigenAPIError
from kleinanzeigen_mcp.models import (
    JobAccepted,
    JobResultItem,
    JobResultsPage,
    JobStatus,
)
from kleinanzeigen_mcp.server import create_mcp_server


def _accepted(
    job_id: str,
    *,
    kind: str,
    state: str,
) -> JobAccepted:
    return JobAccepted(
        job_id=job_id,
        type=kind,
        state=state,
        status_url=f"/jobs/{job_id}",
        results_url=f"/jobs/{job_id}/results",
    )


def _status(
    job_id: str,
    *,
    kind: str,
    state: str,
    available: int,
    continuation: dict[str, Any] | None = None,
    resume: dict[str, Any] | None = None,
) -> JobStatus:
    return JobStatus(
        job_id=job_id,
        type=kind,
        state=state,
        progress={"completed": available},
        results={"available": available, "released": available},
        continuation=continuation or {"required": False},
        resume=resume or {"required": False},
        cooldown={"active": False},
        errors={"open": 0, "last": None},
    )


def _server_for(fake_client: Any):
    @asynccontextmanager
    async def client_factory() -> AsyncIterator[Any]:
        yield fake_client

    return create_mcp_server(client_factory)


def _payload(result: Any) -> dict[str, Any]:
    assert result.structured_content is not None
    return result.structured_content


def _cursor_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    prefix, value = cursor.split(":", 1)
    assert prefix == "c"
    return int(value)


class CandidateSearchClient:
    """Freigabe einer initialen und weiterer 200er-Tranchen."""

    job_id = "candidate-search"

    def __init__(self, total: int) -> None:
        self.total = total
        self.released = 0
        self.continue_calls: list[tuple[str, str]] = []

    @property
    def state(self) -> str:
        return "completed" if self.released >= self.total else "awaiting_continuation"

    @property
    def token(self) -> str:
        return f"continue:{self.released}"

    async def create_search_job(
        self,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> JobAccepted:
        assert payload == {"query": "Fahrrad"}
        assert idempotency_key
        self.released = min(self.total, 200)
        return _accepted(self.job_id, kind="search", state=self.state)

    async def continue_job(
        self,
        job_id: str,
        continuation_token: str,
        *,
        after_cursor: str | None = None,
    ) -> JobAccepted:
        assert job_id == self.job_id
        assert continuation_token == self.token
        assert after_cursor == f"c:{self.released}"
        self.continue_calls.append((job_id, continuation_token))
        self.released = min(self.total, self.released + 200)
        return _accepted(self.job_id, kind="search", state=self.state)

    async def get_job(self, job_id: str) -> JobStatus:
        assert job_id == self.job_id
        continuation = (
            {"required": True, "token": self.token}
            if self.state == "awaiting_continuation"
            else {"required": False}
        )
        return _status(
            job_id,
            kind="search",
            state=self.state,
            available=self.released,
            continuation=continuation,
        )

    async def get_results(
        self,
        job_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> JobResultsPage:
        assert job_id == self.job_id
        offset = _cursor_offset(cursor)
        end = min(self.released, offset + limit)
        items = [
            JobResultItem(
                sequence=index + 1,
                listing_id=f"listing-{index + 1}",
                status="complete",
                data={"title": f"Treffer {index + 1}"},
            )
            for index in range(offset, end)
        ]
        return JobResultsPage(
            job_id=job_id,
            type="search",
            items=items,
            next_cursor=f"c:{end}" if items else cursor,
            has_more=end < self.released,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_count", [0, 199, 200, 201, 400, 401])
async def test_search_and_continuations_release_at_most_200_new_candidates(
    candidate_count: int,
) -> None:
    fake = CandidateSearchClient(candidate_count)
    chunks: list[list[dict[str, Any]]] = []

    async with Client(_server_for(fake), raise_exceptions=False) as client:
        result = await client.call_tool(
            "search_listings",
            {"query": "Fahrrad", "wait_timeout_seconds": 0},
        )
        output = _payload(result)
        assert result.is_error is False
        chunks.append(output["listings"])

        while output["continuation"].get("required"):
            assert output["next_cursor"] is not None
            result = await client.call_tool(
                "continue_search",
                {
                    "job_id": fake.job_id,
                    "continuation_token": output["continuation"]["token"],
                    "after_cursor": output["next_cursor"],
                    "wait_timeout_seconds": 0,
                },
            )
            output = _payload(result)
            assert result.is_error is False
            chunks.append(output["listings"])

    expected_sizes: list[int] = []
    remaining = candidate_count
    while True:
        expected_sizes.append(min(remaining, 200))
        remaining -= expected_sizes[-1]
        if remaining <= 0:
            break

    assert [len(chunk) for chunk in chunks] == expected_sizes
    assert all(len(chunk) <= 200 for chunk in chunks)
    listing_ids = [item["listing_id"] for chunk in chunks for item in chunk]
    assert listing_ids == [
        f"listing-{index}" for index in range(1, candidate_count + 1)
    ]
    assert len(fake.continue_calls) == max(0, len(expected_sizes) - 1)
    assert output["complete"] is True
    assert output["continuation"] == {"required": False}


@pytest.mark.asyncio
async def test_derived_idempotency_key_is_scoped_to_one_server_instance() -> None:
    outputs: list[dict[str, Any]] = []
    for _ in range(2):
        fake = CandidateSearchClient(0)
        async with Client(_server_for(fake), raise_exceptions=False) as client:
            result = await client.call_tool(
                "search_listings",
                {"query": "Fahrrad", "wait_timeout_seconds": 0},
            )
        outputs.append(_payload(result))

    assert outputs[0]["idempotency_key"] != outputs[1]["idempotency_key"]


class OrderedDetailClient:
    job_id = "ordered-details"

    def __init__(self) -> None:
        self.listing_ids: list[str] = []

    async def create_detail_job(
        self,
        *,
        listing_ids: list[str] | None = None,
        source_search_job_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> JobAccepted:
        assert source_search_job_id is None
        assert listing_ids is not None
        assert idempotency_key
        self.listing_ids = listing_ids
        return _accepted(self.job_id, kind="details", state="completed")

    async def get_job(self, job_id: str) -> JobStatus:
        return _status(
            job_id,
            kind="details",
            state="completed",
            available=len(self.listing_ids),
        )

    async def get_results(
        self,
        job_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> JobResultsPage:
        assert cursor is None
        items = [
            JobResultItem(
                sequence=index,
                listing_id=listing_id,
                status="complete",
                data={"position": index},
            )
            for index, listing_id in enumerate(self.listing_ids, start=1)
        ][:limit]
        return JobResultsPage(
            job_id=job_id,
            type="details",
            items=items,
            next_cursor=f"c:{len(items)}",
            has_more=len(items) < len(self.listing_ids),
        )


@pytest.mark.asyncio
async def test_detail_ids_are_normalised_deduplicated_and_keep_first_seen_order() -> (
    None
):
    fake = OrderedDetailClient()
    async with Client(_server_for(fake), raise_exceptions=False) as client:
        result = await client.call_tool(
            "get_listing_details",
            {
                "listing_ids": [
                    " 123456 ",
                    "234567",
                    "123456",
                    "345678-12-34",
                    "234567",
                ],
                "wait_timeout_seconds": 0,
            },
        )

    output = _payload(result)
    assert result.is_error is False
    assert fake.listing_ids == ["123456", "234567", "345678-12-34"]
    assert output["requested_count"] == 3
    assert [item["listing_id"] for item in output["items"]] == fake.listing_ids


class PayloadRejectingDetailClient:
    async def create_detail_job(
        self,
        *,
        listing_ids: list[str] | None = None,
        source_search_job_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> JobAccepted:
        assert source_search_job_id is None
        assert listing_ids is not None and len(listing_ids) > 7_000
        raise KleinanzeigenAPIError(
            code="payload_too_large",
            message="Request body exceeds the configured limit",
            status_code=413,
        )


@pytest.mark.asyncio
async def test_large_detail_list_surfaces_backend_payload_too_large_guidance() -> None:
    listing_ids = [str(100_000 + index) for index in range(8_000)]
    async with Client(
        _server_for(PayloadRejectingDetailClient()),
        raise_exceptions=False,
    ) as client:
        result = await client.call_tool(
            "get_listing_details",
            {"listing_ids": listing_ids, "wait_timeout_seconds": 0},
        )

    output = _payload(result)
    assert result.is_error is True
    assert output["error"] == {
        "code": "payload_too_large",
        "message": "Request body exceeds the configured limit",
        "retryable": False,
        "status_code": 413,
        "suggested_action": "listing_ids in selbst gewählte Teilmengen aufteilen",
    }


class CharacterBudgetClient:
    job_id = "large-detail-results"

    def __init__(self) -> None:
        self.requested_limits: list[int] = []
        self.description = "ä" * 300_000

    async def get_job(self, job_id: str) -> JobStatus:
        return _status(job_id, kind="details", state="running", available=2)

    async def get_results(
        self,
        job_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> JobResultsPage:
        assert cursor is None
        self.requested_limits.append(limit)
        items = [
            JobResultItem(
                sequence=index,
                listing_id=str(100_000 + index),
                status="complete",
                data={"description": self.description},
            )
            for index in range(1, min(limit, 2) + 1)
        ]
        return JobResultsPage(
            job_id=job_id,
            type="details",
            items=items,
            next_cursor=f"large:{len(items)}",
            has_more=len(items) < 2,
        )


@pytest.mark.asyncio
async def test_large_detail_results_reduce_page_without_truncating_an_item() -> None:
    fake = CharacterBudgetClient()
    async with Client(_server_for(fake), raise_exceptions=False) as client:
        result = await client.call_tool(
            "get_job_results",
            {"job_id": fake.job_id, "limit": 2},
        )

    output = _payload(result)
    assert result.is_error is False
    assert fake.requested_limits == [2, 1]
    assert output["requested_limit"] == 2
    assert output["effective_limit"] == 1
    assert output["has_more"] is True
    assert output["items"][0]["data"]["description"] == fake.description


class DetailChangeFeedClient:
    job_id = "changing-details"
    listing_ids = ["123456", "234567"]

    async def create_detail_job(self, **kwargs: Any) -> JobAccepted:
        assert kwargs["listing_ids"] == self.listing_ids
        return _accepted(self.job_id, kind="details", state="running")

    async def get_job(self, job_id: str) -> JobStatus:
        return _status(job_id, kind="details", state="running", available=2)

    async def get_results(
        self,
        job_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> JobResultsPage:
        if cursor is None:
            items = [
                JobResultItem(
                    sequence=index,
                    listing_id=listing_id,
                    status="pending",
                )
                for index, listing_id in enumerate(self.listing_ids, start=1)
            ]
            next_cursor = "changes:2"
        elif cursor == "changes:2":
            items = [
                JobResultItem(
                    sequence=3,
                    listing_id="123456",
                    status="complete",
                    data={"title": "Vollständig"},
                ),
                JobResultItem(
                    sequence=4,
                    listing_id="234567",
                    status="not_found",
                    error={"code": "not_found", "message": "Nicht mehr verfügbar"},
                ),
            ]
            next_cursor = "changes:4"
        else:
            assert cursor == "changes:4"
            items = []
            next_cursor = cursor
        return JobResultsPage(
            job_id=job_id,
            type="details",
            items=items[:limit],
            next_cursor=next_cursor,
            has_more=False,
        )


@pytest.mark.asyncio
async def test_detail_timeout_returns_pending_and_cursor_reads_later_changes() -> None:
    fake = DetailChangeFeedClient()
    async with Client(_server_for(fake), raise_exceptions=False) as client:
        initial = await client.call_tool(
            "get_listing_details",
            {
                "listing_ids": fake.listing_ids,
                "wait_timeout_seconds": 0,
            },
        )
        initial_output = _payload(initial)
        changed = await client.call_tool(
            "get_job_results",
            {
                "job_id": fake.job_id,
                "cursor": initial_output["next_cursor"],
                "limit": 200,
            },
        )
        changed_output = _payload(changed)
        exhausted = await client.call_tool(
            "get_job_results",
            {
                "job_id": fake.job_id,
                "cursor": changed_output["next_cursor"],
                "limit": 200,
            },
        )

    assert initial.is_error is False
    assert initial_output["timed_out"] is True
    assert initial_output["job"]["state"] == "running"
    assert [item["status"] for item in initial_output["items"]] == [
        "pending",
        "pending",
    ]
    assert changed.is_error is False
    assert [item["status"] for item in changed_output["items"]] == [
        "complete",
        "not_found",
    ]
    assert changed_output["next_cursor"] == "changes:4"
    assert _payload(exhausted)["items"] == []


class NeedsAttentionDetailClient:
    job_id = "attention-details"

    def __init__(self) -> None:
        self.resumed = False
        self.resume_calls: list[str] = []

    async def create_detail_job(self, **kwargs: Any) -> JobAccepted:
        return _accepted(self.job_id, kind="details", state="queued")

    async def get_job(self, job_id: str) -> JobStatus:
        if self.resumed:
            return _status(job_id, kind="details", state="completed", available=1)
        return _status(
            job_id,
            kind="details",
            state="needs_attention",
            available=1,
            resume={"required": True, "token": "resume-exactly-once"},
        )

    async def get_results(self, job_id: str, **kwargs: Any) -> JobResultsPage:
        return JobResultsPage(
            job_id=job_id,
            type="details",
            items=[
                JobResultItem(
                    sequence=1,
                    listing_id="123456",
                    status="pending" if not self.resumed else "complete",
                )
            ],
            next_cursor="detail:1",
            has_more=False,
        )

    async def resume_job(self, job_id: str, resume_token: str) -> JobAccepted:
        assert resume_token == "resume-exactly-once"
        self.resume_calls.append(resume_token)
        self.resumed = True
        return _accepted(job_id, kind="details", state="queued")


@pytest.mark.asyncio
async def test_needs_attention_is_actionable_and_resume_uses_exact_token() -> None:
    fake = NeedsAttentionDetailClient()
    async with Client(_server_for(fake), raise_exceptions=False) as client:
        detail_result = await client.call_tool(
            "get_listing_details",
            {"listing_ids": ["123456"], "wait_timeout_seconds": 0},
        )
        detail = _payload(detail_result)
        resumed_result = await client.call_tool(
            "resume_job",
            {
                "job_id": fake.job_id,
                "resume_token": detail["resume"]["token"],
                "wait_timeout_seconds": 0,
            },
        )

    assert detail_result.is_error is False
    assert detail["timed_out"] is False
    assert detail["job"]["state"] == "needs_attention"
    assert detail["job"]["action_required"] == "resume"
    assert detail["resume"] == {
        "required": True,
        "token": "resume-exactly-once",
    }
    resumed = _payload(resumed_result)
    assert resumed_result.is_error is False
    assert resumed["job"]["state"] == "completed"
    assert resumed["job"]["terminal"] is True
    assert fake.resume_calls == ["resume-exactly-once"]


class MergeAfterContinuationClient:
    """Zwei Quellen, von denen eine später einen weiteren Treffer freigibt."""

    def __init__(self) -> None:
        self.items = {
            "source-a": [
                JobResultItem(
                    sequence=1,
                    listing_id="listing-a",
                    status="complete",
                    data={"title": "A"},
                ),
                JobResultItem(
                    sequence=2,
                    listing_id="listing-shared",
                    status="complete",
                    data={"title": "A gewinnt"},
                ),
                JobResultItem(
                    sequence=3,
                    listing_id="listing-late",
                    status="complete",
                    data={"title": "Später freigegeben"},
                ),
            ],
            "source-b": [
                JobResultItem(
                    sequence=1,
                    listing_id="listing-shared",
                    status="complete",
                    data={"title": "B verliert"},
                ),
                JobResultItem(
                    sequence=2,
                    listing_id="listing-b",
                    status="complete",
                    data={"title": "B"},
                ),
            ],
        }
        self.available = {"source-a": 2, "source-b": 2}

    async def get_job(self, job_id: str) -> JobStatus:
        waiting = job_id == "source-a" and self.available[job_id] == 2
        return _status(
            job_id,
            kind="search",
            state="awaiting_continuation" if waiting else "completed",
            available=self.available[job_id],
            continuation=(
                {"required": True, "token": "release-late"}
                if waiting
                else {"required": False}
            ),
        )

    async def continue_job(
        self,
        job_id: str,
        continuation_token: str,
        *,
        after_cursor: str | None = None,
    ) -> JobAccepted:
        assert job_id == "source-a"
        assert continuation_token == "release-late"
        assert after_cursor == "c:2"
        self.available[job_id] = 3
        return _accepted(job_id, kind="search", state="completed")

    async def get_results(
        self,
        job_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> JobResultsPage:
        offset = _cursor_offset(cursor)
        end = min(self.available[job_id], offset + limit)
        page_items = self.items[job_id][offset:end]
        return JobResultsPage(
            job_id=job_id,
            type="search",
            items=page_items,
            next_cursor=f"c:{end}" if page_items else cursor,
            has_more=end < self.available[job_id],
        )


@pytest.mark.asyncio
async def test_merge_cursor_freezes_snapshot_across_later_source_continuation() -> None:
    fake = MergeAfterContinuationClient()
    async with Client(_server_for(fake), raise_exceptions=False) as client:
        first_result = await client.call_tool(
            "merge_search_results",
            {"search_job_ids": ["source-a", "source-b"], "limit": 1},
        )
        first = _payload(first_result)

        continued_result = await client.call_tool(
            "continue_search",
            {
                "job_id": "source-a",
                "continuation_token": "release-late",
                "after_cursor": "c:2",
                "wait_timeout_seconds": 0,
            },
        )
        continued = _payload(continued_result)

        old_snapshot_result = await client.call_tool(
            "merge_search_results",
            {
                "search_job_ids": ["source-a", "source-b"],
                "cursor": first["next_cursor"],
                "limit": 200,
            },
        )
        old_snapshot = _payload(old_snapshot_result)

        new_snapshot_result = await client.call_tool(
            "merge_search_results",
            {"search_job_ids": ["source-a", "source-b"], "limit": 200},
        )
        new_snapshot = _payload(new_snapshot_result)

    assert first_result.is_error is False
    assert [item["listing_id"] for item in first["listings"]] == ["listing-a"]
    assert first["total_unique_in_snapshot"] == 3
    assert first["has_more"] is True
    assert continued_result.is_error is False
    assert [item["listing_id"] for item in continued["listings"]] == ["listing-late"]
    assert old_snapshot_result.is_error is False
    assert [item["listing_id"] for item in old_snapshot["listings"]] == [
        "listing-shared",
        "listing-b",
    ]
    assert old_snapshot["total_unique_in_snapshot"] == 3
    assert all(source["snapshot_watermark"] == 2 for source in old_snapshot["sources"])
    assert new_snapshot_result.is_error is False
    assert [item["listing_id"] for item in new_snapshot["listings"]] == [
        "listing-a",
        "listing-shared",
        "listing-late",
        "listing-b",
    ]
    assert new_snapshot["total_unique_in_snapshot"] == 4
