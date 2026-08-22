"""Offline contracts for the persistent job API and legacy adapters."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import (
    inserat,
    inserate_batch,
    inserate_by_url,
    inserate_detailed_ultra,
    inserate_ultra,
    jobs,
)
from utils.job_manager import (
    InvalidCursorError,
    JobConflictError,
    JobNotFoundError,
    JobStorageUnavailable,
)


JOB_ID = "123e4567-e89b-12d3-a456-426614174000"


class FakeJobManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.error: Exception | None = None

    def _raise_if_configured(self) -> None:
        if self.error is not None:
            raise self.error

    def _accepted(self, kind: str) -> dict[str, Any]:
        return {
            "job_id": JOB_ID,
            "type": kind,
            "state": "queued",
            "idempotent_replay": False,
        }

    async def create_search_job(
        self,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._raise_if_configured()
        self.calls.append(("create_search_job", (payload, idempotency_key)))
        return self._accepted("search")

    async def create_detail_job(
        self,
        *,
        source_search_job_id: str | None = None,
        listing_ids: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._raise_if_configured()
        self.calls.append(
            (
                "create_detail_job",
                (source_search_job_id, listing_ids, idempotency_key),
            )
        )
        return self._accepted("details")

    async def get_job(self, job_id: str) -> dict[str, Any]:
        self._raise_if_configured()
        self.calls.append(("get_job", job_id))
        return {
            **self._accepted("search"),
            "progress": {"completed": 0, "total": 1},
            "results": {"released": 0},
            "continuation": {"required": False},
            "resume": {"required": False},
            "cooldown": {"active": False},
            "errors": {"open": 0, "last": None},
        }

    async def get_results(
        self,
        job_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        self._raise_if_configured()
        self.calls.append(("get_results", (job_id, cursor, limit)))
        return {
            "job_id": job_id,
            "type": "search",
            "items": [],
            "next_cursor": None,
            "has_more": False,
        }

    async def continue_job(
        self,
        job_id: str,
        *,
        continuation_token: str,
        after_cursor: str | None = None,
    ) -> dict[str, Any]:
        self._raise_if_configured()
        self.calls.append(("continue_job", (job_id, continuation_token, after_cursor)))
        return self._accepted("search")

    async def resume_job(
        self,
        job_id: str,
        *,
        resume_token: str,
    ) -> dict[str, Any]:
        self._raise_if_configured()
        self.calls.append(("resume_job", (job_id, resume_token)))
        return self._accepted("details")

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        self._raise_if_configured()
        self.calls.append(("cancel_job", job_id))
        return {**self._accepted("details"), "state": "cancelled"}


@pytest.fixture
def manager() -> FakeJobManager:
    return FakeJobManager()


@pytest.fixture
def client(manager: FakeJobManager) -> TestClient:
    app = FastAPI()
    app.state.job_manager = manager
    app.include_router(jobs.router)
    app.include_router(inserate_ultra.router)
    app.include_router(inserate_by_url.router)
    app.include_router(inserat.router)
    app.include_router(inserate_batch.router)
    app.include_router(inserate_detailed_ultra.router)
    return TestClient(app)


def test_search_submission_is_immediate_and_forwards_idempotency_key(
    client: TestClient,
    manager: FakeJobManager,
):
    response = client.post(
        "/jobs/search",
        headers={"Idempotency-Key": "agent-a.search-1"},
        json={"query": "Fahrrad", "location": "Berlin", "radius": 10},
    )

    assert response.status_code == 202
    assert response.json() == {
        "job_id": JOB_ID,
        "type": "search",
        "state": "queued",
        "idempotent_replay": False,
        "status_url": f"/jobs/{JOB_ID}",
        "results_url": f"/jobs/{JOB_ID}/results",
    }
    assert manager.calls == [
        (
            "create_search_job",
            (
                {"query": "Fahrrad", "location": "Berlin", "radius": 10},
                "agent-a.search-1",
            ),
        )
    ]


def test_search_url_is_fail_closed_and_cannot_be_mixed_with_filters(
    client: TestClient,
    manager: FakeJobManager,
):
    unsafe = client.post(
        "/jobs/search",
        json={"url": "https://example.org/s-fahrrad/k0"},
    )
    mixed = client.post(
        "/jobs/search",
        json={
            "url": "https://www.kleinanzeigen.de/s-fahrrad/k0",
            "query": "Fahrrad",
        },
    )

    assert unsafe.status_code == 422
    assert mixed.status_code == 422
    assert manager.calls == []


def test_detail_submission_requires_one_source_and_deduplicates_ids(
    client: TestClient,
    manager: FakeJobManager,
):
    invalid = client.post("/jobs/details", json={})
    response = client.post(
        "/jobs/details",
        json={"listing_ids": ["123456", "123456", "654321-1-2"]},
    )

    assert invalid.status_code == 422
    assert response.status_code == 202
    assert manager.calls == [
        (
            "create_detail_job",
            (None, ["123456", "654321-1-2"], None),
        )
    ]


def test_status_results_and_actions_delegate_without_browser_work(
    client: TestClient,
    manager: FakeJobManager,
):
    assert client.get(f"/jobs/{JOB_ID}").status_code == 200
    assert (
        client.get(
            f"/jobs/{JOB_ID}/results",
            params={"cursor": "opaque", "limit": 17},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/jobs/{JOB_ID}/continue",
            json={"continuation_token": "continue-token"},
        ).status_code
        == 202
    )
    assert (
        client.post(
            f"/jobs/{JOB_ID}/resume",
            json={"resume_token": "resume-token"},
        ).status_code
        == 202
    )
    assert client.delete(f"/jobs/{JOB_ID}").status_code == 200

    assert manager.calls == [
        ("get_job", JOB_ID),
        ("get_results", (JOB_ID, "opaque", 17)),
        ("continue_job", (JOB_ID, "continue-token", None)),
        ("resume_job", (JOB_ID, "resume-token")),
        ("cancel_job", JOB_ID),
    ]


def test_continue_requires_the_status_token(
    client: TestClient,
    manager: FakeJobManager,
):
    response = client.post(f"/jobs/{JOB_ID}/continue")

    assert response.status_code == 422
    assert manager.calls == []


def test_continue_forwards_optional_after_cursor(
    client: TestClient,
    manager: FakeJobManager,
):
    response = client.post(
        f"/jobs/{JOB_ID}/continue",
        json={
            "continuation_token": "continue-token",
            "after_cursor": "opaque-search-cursor",
        },
    )

    assert response.status_code == 202
    assert manager.calls == [
        (
            "continue_job",
            (JOB_ID, "continue-token", "opaque-search-cursor"),
        )
    ]


@pytest.mark.parametrize("after_cursor", ["", "x" * 513])
def test_continue_rejects_invalid_after_cursor_before_manager_call(
    client: TestClient,
    manager: FakeJobManager,
    after_cursor: str,
):
    response = client.post(
        f"/jobs/{JOB_ID}/continue",
        json={
            "continuation_token": "continue-token",
            "after_cursor": after_cursor,
        },
    )

    assert response.status_code == 422
    assert manager.calls == []


def test_continue_maps_job_manager_cursor_mismatch_to_http_400(
    client: TestClient,
    manager: FakeJobManager,
):
    manager.error = InvalidCursorError("cursor boundary mismatch")

    response = client.post(
        f"/jobs/{JOB_ID}/continue",
        json={
            "continuation_token": "continue-token",
            "after_cursor": "opaque-search-cursor",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_cursor"


def test_resume_requires_the_status_token(
    client: TestClient,
    manager: FakeJobManager,
):
    response = client.post(f"/jobs/{JOB_ID}/resume")

    assert response.status_code == 422
    assert manager.calls == []


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (JobNotFoundError("missing"), 404, "job_not_found"),
        (JobConflictError("duplicate key"), 409, "job_conflict"),
        (JobStorageUnavailable("disk unavailable"), 503, "job_storage_unavailable"),
        (InvalidCursorError("invalid"), 400, "invalid_cursor"),
    ],
)
def test_manager_errors_have_controlled_http_mapping(
    client: TestClient,
    manager: FakeJobManager,
    error: Exception,
    expected_status: int,
    expected_code: str,
):
    manager.error = error

    response = client.get(f"/jobs/{JOB_ID}")

    assert response.status_code == expected_status
    assert response.json()["detail"]["error"] == expected_code


def test_missing_manager_is_a_clean_503():
    app = FastAPI()
    app.include_router(jobs.router)

    response = TestClient(app).post("/jobs/search", json={"query": "Fahrrad"})

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "job_manager_unavailable"


def test_legacy_search_routes_only_submit_jobs(
    client: TestClient,
    manager: FakeJobManager,
):
    structured = client.get(
        "/inserate",
        params={"query": "Fahrrad", "page_count": 20},
    )
    by_url = client.post(
        "/inserate-by-url",
        json={
            "url": "https://www.kleinanzeigen.de/s-fahrrad/k0",
            "max_pages": 20,
        },
    )

    assert structured.status_code == 202
    assert by_url.status_code == 202
    assert structured.headers["Deprecation"] == "true"
    assert by_url.headers["Deprecation"] == "true"
    assert manager.calls == [
        ("create_search_job", ({"query": "Fahrrad"}, None)),
        (
            "create_search_job",
            ({"url": "https://www.kleinanzeigen.de/s-fahrrad/k0"}, None),
        ),
    ]


def test_legacy_detail_batch_has_no_fifty_item_or_concurrency_cap(
    client: TestClient,
    manager: FakeJobManager,
):
    listing_ids = [str(100000 + index) for index in range(109)]

    response = client.post(
        "/inserate/batch",
        json={"ids": listing_ids, "max_concurrent": 999},
    )

    assert response.status_code == 202
    assert manager.calls == [
        ("create_detail_job", (None, listing_ids, None)),
    ]


def test_legacy_single_detail_and_removed_combined_endpoint(
    client: TestClient,
    manager: FakeJobManager,
):
    detail = client.get("/inserat/123456")
    combined = client.get("/inserate-detailed")

    assert detail.status_code == 202
    assert manager.calls == [
        ("create_detail_job", (None, ["123456"], None)),
    ]
    assert combined.status_code == 410
    assert combined.json()["detail"]["search_endpoint"] == "/jobs/search"
