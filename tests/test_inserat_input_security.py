"""Offline regression tests for detail-request resource and log boundaries."""

import asyncio
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.inserat as inserat_router
import routers.inserate_batch as batch_router
import scrapers.inserat as inserat_scraper
from utils.input_validation import sanitize_log_value


JOB_ID = "123e4567-e89b-12d3-a456-426614174000"


class FakeJobManager:
    """Capture persisted detail submissions without starting browser work."""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, list[str] | None, str | None]] = []

    async def create_detail_job(
        self,
        *,
        source_search_job_id: str | None = None,
        listing_ids: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((source_search_job_id, listing_ids, idempotency_key))
        return {
            "job_id": JOB_ID,
            "type": "details",
            "state": "queued",
            "idempotent_replay": False,
        }


@pytest.fixture
def client():
    app = FastAPI()
    manager = FakeJobManager()
    app.state.job_manager = manager
    app.include_router(inserat_router.router)
    app.include_router(batch_router.router)

    with TestClient(app) as test_client:
        yield test_client, manager


def test_batch_accepts_more_than_fifty_ids_as_one_persisted_job(client):
    test_client, manager = client
    listing_ids = [str(1_000_000 + index) for index in range(109)]

    response = test_client.post(
        "/inserate/batch",
        json={"ids": listing_ids},
    )

    assert response.status_code == 202
    assert manager.calls == [(None, listing_ids, None)]


@pytest.mark.parametrize("max_concurrent", [0, 11, 999])
def test_batch_ignores_legacy_concurrency_hint(client, max_concurrent):
    test_client, manager = client
    response = test_client.post(
        "/inserate/batch",
        json={"ids": ["3399373623"], "max_concurrent": max_concurrent},
    )

    assert response.status_code == 202
    assert manager.calls == [(None, ["3399373623"], None)]


def test_batch_deduplicates_stably_before_persisting(client):
    test_client, manager = client
    response = test_client.post(
        "/inserate/batch",
        json={
            "ids": [
                "3399373623",
                "3399373623-220-16792",
                "3399373623",
                "3400236352",
            ],
            "max_concurrent": 2,
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["job_id"] == JOB_ID
    assert body["state"] == "queued"
    assert body["status_url"] == f"/jobs/{JOB_ID}"
    assert manager.calls == [
        (
            None,
            ["3399373623", "3399373623-220-16792", "3400236352"],
            None,
        )
    ]


@pytest.mark.parametrize(
    "listing_id",
    ["3399373623\nFORGED", "../../../etc/passwd", "3399373623-220"],
)
def test_batch_rejects_malformed_listing_ids_before_persisting(client, listing_id):
    test_client, manager = client
    response = test_client.post("/inserate/batch", json={"ids": [listing_id]})

    assert response.status_code == 422
    assert manager.calls == []


def test_single_route_accepts_both_documented_id_formats(client):
    test_client, manager = client

    plain = test_client.get(
        "/inserat/3399373623", params={"batch_id": "test-plain-adid"}
    )
    segment = test_client.get(
        "/inserat/3399373623-220-16792",
        params={"batch_id": "test-full-segment"},
    )

    assert plain.status_code == 202
    assert segment.status_code == 202
    assert manager.calls == [
        (None, ["3399373623"], None),
        (None, ["3399373623-220-16792"], None),
    ]
    assert plain.headers["Deprecation"] == "true"
    assert segment.headers["Deprecation"] == "true"


def test_single_route_does_not_require_or_persist_legacy_correlation_id(client):
    test_client, manager = client

    response = test_client.get("/inserat/3399373623")

    assert response.status_code == 202
    assert manager.calls == [(None, ["3399373623"], None)]


@pytest.mark.parametrize(
    "listing_id",
    ["3399373623%0AFORGED", "not-numeric", "3399373623-220"],
)
def test_single_route_rejects_malformed_listing_ids_before_persisting(
    client, listing_id
):
    test_client, manager = client
    response = test_client.get(
        f"/inserat/{listing_id}", params={"batch_id": "valid-correlation"}
    )

    assert response.status_code == 422
    assert manager.calls == []


@pytest.mark.parametrize(
    "batch_id",
    ["ok\nFORGED", "ok\rFORGED", "tab\tFORGED", "a" * 65],
)
def test_single_route_rejects_log_injection_before_persisting(client, batch_id):
    test_client, manager = client
    response = test_client.get("/inserat/3399373623", params={"batch_id": batch_id})

    assert response.status_code == 422
    assert manager.calls == []


def test_log_sanitizer_neutralizes_all_control_characters(caplog):
    untrusted = "request\r\nFORGED\t\x00\u2028\u2029\u202e"
    safe = sanitize_log_value(untrusted)

    with caplog.at_level(logging.INFO, logger="security-test"):
        logging.getLogger("security-test").info("request_id=%s", safe)

    assert "\r" not in safe
    assert "\n" not in safe
    assert "\t" not in safe
    assert "\x00" not in safe
    assert "\u2028" not in safe
    assert "\u2029" not in safe
    assert "\u202e" not in safe
    assert "request\\x0d\\x0aFORGED\\x09\\x00\\u2028\\u2029\\u202e" in caplog.text


def test_scraper_shared_boundary_rejects_invalid_id_without_browser_access(
    outbound_controller,
):
    class BrowserMustNotBeUsed:
        def __getattr__(self, name):
            raise AssertionError(f"browser boundary unexpectedly reached: {name}")

    with pytest.raises(ValueError, match="Listing ID"):
        asyncio.run(
            inserat_scraper.get_inserate_details_optimized(
                BrowserMustNotBeUsed(),
                outbound_controller,
                "3399373623\nFORGED",
                request_id="valid",
            )
        )


def test_scraper_log_sink_escapes_correlation_controls(
    monkeypatch, caplog, outbound_controller
):
    class FakePage:
        async def close(self):
            pass

    class FakeContext:
        async def new_page(self):
            return FakePage()

    class FakeBrowserManager:
        async def get_context(self):
            return FakeContext()

        async def release_context(self, _context):
            pass

        async def execute_with_semaphore(self, operation):
            return await operation

        def get_performance_metrics(self):
            return {"contexts_in_use": 0, "contexts_in_pool": 1}

    async def fake_details(_url, _page, _outbound_controller, **_kwargs):
        return {"id": "3399373623", "status": "active"}

    monkeypatch.setattr(inserat_scraper, "get_inserate_details", fake_details)

    with caplog.at_level(logging.INFO, logger="inserat_scraper"):
        result = asyncio.run(
            inserat_scraper.get_inserate_details_optimized(
                FakeBrowserManager(),
                outbound_controller,
                "3399373623",
                request_id="valid\r\nFORGED",
                progress="1/1\tFORGED",
            )
        )

    detail_message = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("[DETAIL")
    )
    assert result["success"] is True
    assert "\r" not in detail_message
    assert "\n" not in detail_message
    assert "\t" not in detail_message
    assert "valid\\x0d\\x0aFORGED" in detail_message
    assert "1/1\\x09FORGED" in detail_message
