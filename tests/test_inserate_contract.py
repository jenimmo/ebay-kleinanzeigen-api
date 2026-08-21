"""Offline contracts for legacy search adapters backed by persistent jobs."""

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.inserate_by_url as by_url_router
import routers.inserate_ultra as inserate_router
import routers.jobs as jobs_router


JOB_ID = "123e4567-e89b-12d3-a456-426614174000"
SEARCH_URL = "https://www.kleinanzeigen.de/s-test"


class FakeJobManager:
    """Record search submissions without allocating browser resources."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], str | None]] = []

    async def create_search_job(
        self,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((payload, idempotency_key))
        return {
            "job_id": JOB_ID,
            "type": "search",
            "state": "queued",
            "idempotent_replay": False,
        }


def make_client() -> tuple[TestClient, FakeJobManager]:
    app = FastAPI()
    manager = FakeJobManager()
    app.state.job_manager = manager
    app.include_router(jobs_router.router)
    app.include_router(inserate_router.router)
    app.include_router(by_url_router.router)
    return TestClient(app), manager


def test_canonical_search_route_persists_filters_and_returns_polling_urls():
    client, manager = make_client()

    response = client.post(
        "/jobs/search",
        headers={"Idempotency-Key": "agent-a.search-1"},
        json={
            "query": "Innenputz",
            "location": "Berlin",
            "radius": 10,
            "min_price": 100,
            "max_price": 500,
        },
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
            {
                "query": "Innenputz",
                "location": "Berlin",
                "radius": 10,
                "min_price": 100,
                "max_price": 500,
            },
            "agent-a.search-1",
        )
    ]


def test_legacy_structured_search_submits_job_without_scraping_in_request():
    client, manager = make_client()

    response = client.get(
        "/inserate",
        params={"query": "Innenputz", "page_count": 20},
    )

    assert response.status_code == 202
    assert response.headers["Deprecation"] == "true"
    assert response.headers["Link"] == '</jobs/search>; rel="successor-version"'
    assert response.json()["state"] == "queued"
    assert manager.calls == [({"query": "Innenputz"}, None)]


def test_legacy_search_rejects_invalid_price_range_before_persisting():
    client, manager = make_client()

    response = client.get(
        "/inserate",
        params={"query": "Innenputz", "min_price": 500, "max_price": 100},
    )

    assert response.status_code == 422
    assert manager.calls == []


def test_by_url_route_submits_validated_url_job_and_ignores_page_hint():
    client, manager = make_client()

    response = client.post(
        "/inserate-by-url",
        headers={"Idempotency-Key": "agent-b.url-1"},
        json={"url": SEARCH_URL, "max_pages": 20},
    )

    assert response.status_code == 202
    assert response.headers["Deprecation"] == "true"
    assert response.json()["results_url"] == f"/jobs/{JOB_ID}/results"
    assert manager.calls == [
        ({"url": SEARCH_URL}, "agent-b.url-1"),
    ]


def test_by_url_route_rejects_non_kleinanzeigen_target_before_persisting():
    client, manager = make_client()

    response = client.post(
        "/inserate-by-url",
        json={"url": "https://example.org/s-test", "max_pages": 1},
    )

    assert response.status_code == 422
    assert manager.calls == []
