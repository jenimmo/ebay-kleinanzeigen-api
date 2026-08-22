"""In-Memory-Vertrag der öffentlichen MCP-v2-Oberfläche."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
from typing import Any, AsyncIterator

import pytest
from mcp import Client

from kleinanzeigen_mcp.models import JobStatus
from kleinanzeigen_mcp.server import (
    MAX_MERGE_SOURCE_JOBS,
    SERVER_INSTRUCTIONS,
    create_mcp_server,
)


JOB_ID = "123e4567-e89b-12d3-a456-426614174000"
EXPECTED_TOOLS = {
    "search_listings",
    "continue_search",
    "merge_search_results",
    "get_listing_details",
    "get_job_status",
    "get_job_results",
    "resume_job",
    "cancel_job",
}
EXPECTED_ANNOTATIONS = {
    # readOnly, destructive, idempotent, openWorld
    "search_listings": (False, False, False, True),
    "continue_search": (False, False, True, True),
    "merge_search_results": (True, False, True, False),
    "get_listing_details": (False, False, False, True),
    "get_job_status": (True, False, True, False),
    "get_job_results": (True, False, True, False),
    "resume_job": (False, False, True, True),
    "cancel_job": (False, True, True, False),
}
LARGE_RESULT_TOOLS = {
    "search_listings",
    "continue_search",
    "merge_search_results",
    "get_listing_details",
    "get_job_results",
}


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def get_job(self, job_id: str) -> JobStatus:
        self.calls.append(("get_job", job_id))
        return JobStatus(
            job_id=job_id,
            type="search",
            state="awaiting_continuation",
            progress={"completed": 200, "total": 200},
            results={"released": 200},
            continuation={"required": True, "token": "continue-token"},
            resume={"required": False},
            cooldown={"active": False},
            errors={"open": 0, "last": None},
        )


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def mcp_server(fake_client: FakeClient):
    @asynccontextmanager
    async def client_factory() -> AsyncIterator[FakeClient]:
        yield fake_client

    return create_mcp_server(client_factory)


@pytest.mark.asyncio
async def test_server_exposes_exactly_the_eight_public_tools(mcp_server: Any) -> None:
    async with Client(mcp_server, raise_exceptions=True) as client:
        tools = (await client.list_tools()).tools
        instructions = client.instructions
        server_info = client.server_info

    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    assert len(tools) == 8
    assert server_info.name == "kleinanzeigen"
    assert server_info.title == "Kleinanzeigen Agent"
    assert server_info.description is not None
    assert "Kleinanzeigen.de" in server_info.description
    assert all(tool.description for tool in tools)
    assert all(tool.title for tool in tools)
    assert all(tool.input_schema.get("type") == "object" for tool in tools)
    assert all(tool.output_schema is not None for tool in tools)
    assert instructions == SERVER_INSTRUCTIONS
    assert "parallelen Suchjobs" in instructions
    assert "next_action" in instructions

    by_name = {tool.name: tool for tool in tools}
    for tool in tools:
        for parameter_name, parameter_schema in tool.input_schema.get(
            "properties", {}
        ).items():
            assert parameter_schema.get("description"), (
                f"missing description for {tool.name}.{parameter_name}"
            )

    assert "continuation.required=true" in by_name["continue_search"].description
    assert "paralleler Suchjobs" in by_name["merge_search_results"].description
    assert "timed_out=true" in by_name["get_job_results"].description
    assert "state=needs_attention" in by_name["resume_job"].description

    detail_ids = by_name["get_listing_details"].input_schema["properties"][
        "listing_ids"
    ]
    assert detail_ids["type"] == "array"
    assert "maxItems" not in detail_ids
    merge_sources = by_name["merge_search_results"].input_schema["properties"][
        "search_job_ids"
    ]
    assert merge_sources["maxItems"] == MAX_MERGE_SOURCE_JOBS
    continue_cursor = by_name["continue_search"].input_schema["properties"][
        "after_cursor"
    ]
    assert continue_cursor["maxLength"] == 512
    for name in ("merge_search_results", "get_job_results"):
        limit = by_name[name].input_schema["properties"]["limit"]
        assert limit["minimum"] == 1
        assert limit["maximum"] == 200


@pytest.mark.asyncio
async def test_tool_annotations_and_client_output_metadata(mcp_server: Any) -> None:
    async with Client(mcp_server, raise_exceptions=True) as client:
        tools = (await client.list_tools()).tools

    by_name = {tool.name: tool for tool in tools}
    for name, expected in EXPECTED_ANNOTATIONS.items():
        annotations = by_name[name].annotations
        assert annotations is not None, name
        assert (
            annotations.read_only_hint,
            annotations.destructive_hint,
            annotations.idempotent_hint,
            annotations.open_world_hint,
        ) == expected

    for name, tool in by_name.items():
        max_chars = (tool.meta or {}).get("anthropic/maxResultSizeChars")
        if name in LARGE_RESULT_TOOLS:
            assert max_chars == 500_000, name
        else:
            assert max_chars is None, name


@pytest.mark.asyncio
async def test_tool_call_returns_structured_output(
    mcp_server: Any,
    fake_client: FakeClient,
) -> None:
    async with Client(mcp_server, raise_exceptions=True) as client:
        result = await client.call_tool("get_job_status", {"job_id": JOB_ID})

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["schema_version"] == 1
    assert result.structured_content.get("error") is None
    assert result.structured_content["job"]["job_id"] == JOB_ID
    assert result.structured_content["job"]["state"] == "awaiting_continuation"
    assert result.structured_content["job"]["action_required"] == "continue"
    assert result.structured_content["job"]["continuation"] == {
        "required": True,
        "token": "continue-token",
    }
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert json.loads(result.content[0].text) == result.structured_content
    assert fake_client.calls == [("get_job", JOB_ID)]


@pytest.mark.asyncio
async def test_sdk_argument_validation_uses_sanitised_structured_error(
    mcp_server: Any,
) -> None:
    async with Client(mcp_server, raise_exceptions=False) as client:
        result = await client.call_tool("get_job_status", {"job_id": ""})

    assert result.is_error is True
    assert result.structured_content is not None
    assert result.structured_content["schema_version"] == 1
    assert result.structured_content["error"] == {
        "code": "invalid_argument",
        "message": "Die Toolargumente sind ungültig",
        "retryable": False,
        "suggested_action": "Argumente korrigieren und erneut aufrufen",
    }
    assert "pydantic" not in result.content[0].text.lower()
    assert "traceback" not in result.content[0].text.lower()
