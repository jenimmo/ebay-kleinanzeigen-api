"""Offline contract tests for the FastAPI application assembled in main.py."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

import main


JOB_ID = "123e4567-e89b-12d3-a456-426614174000"
EXPECTED_ENDPOINTS = {
    "/health",
    "/outbound-status",
    "/jobs/search",
    "/jobs/details",
    "/jobs/{job_id}",
    "/jobs/{job_id}/results",
    "/jobs/{job_id}/continue",
    "/jobs/{job_id}/resume",
    "/mcp",
    "/messaging/accounts",
    "/messaging/accounts/{account_id}/connect",
    "/messaging/accounts/{account_id}/disconnect",
    "/messaging/accounts/{account_id}/conversations",
    "/messaging/accounts/{account_id}/conversations/{conversation_id}",
    "/messaging/accounts/{account_id}/images/{image_id}",
    "/messaging/accounts/{account_id}/attachments",
    "/messaging/accounts/{account_id}/start-conversation",
    "/messaging/accounts/{account_id}/send-message",
    "/messaging/accounts/{account_id}/operations/{operation_id}",
    "/inserate",
    "/inserat/{id}",
    "/inserate-detailed",
    "/inserate/batch",
    "/inserate-by-url",
    "/convert-url",
}


class _FakeBrowser:
    def __init__(self, connected: bool = True):
        self.connected = connected

    def is_connected(self) -> bool:
        return self.connected


class _FakeBrowserManager:
    def __init__(self, connected: bool = True):
        self._browser = _FakeBrowser(connected)


class _FakeJobManager:
    def __init__(self, *, ready: bool = True) -> None:
        self.calls: list[tuple[dict[str, Any], str | None]] = []
        self.ready = ready

    async def status(self) -> dict[str, Any]:
        return {
            "workers": {
                "configured": 1,
                "running": int(self.ready),
                "executing": 0,
            }
        }

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


@pytest.fixture(autouse=True)
def restore_app_state():
    state = main.app.state
    original_values = {
        name: (hasattr(state, name), getattr(state, name, None))
        for name in ("browser_manager", "job_manager")
    }
    yield
    for name, (existed, value) in original_values.items():
        if existed:
            setattr(state, name, value)
        elif hasattr(state, name):
            delattr(state, name)


def _client_with_browser(
    connected: bool = True,
    *,
    job_manager: _FakeJobManager | None = None,
) -> TestClient:
    main.app.state.browser_manager = _FakeBrowserManager(connected)
    main.app.state.job_manager = job_manager or _FakeJobManager()
    return TestClient(main.app)


def test_root_preserves_health_fields_and_lists_every_public_endpoint():
    response = _client_with_browser().get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Welcome to the Kleinanzeigen API"
    assert set(body["endpoints"]) == EXPECTED_ENDPOINTS
    assert body["status"] == "operational"
    assert body["browser_status"] == "optimized"


def test_health_reports_ready_browser_without_network_access():
    response = _client_with_browser().get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "browser_status": "optimized",
    }


def test_health_rejects_requests_when_browser_is_disconnected():
    response = _client_with_browser(connected=False).get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "status": "unavailable",
            "browser_status": "unavailable",
            "job_manager_status": "ready",
        }
    }


def test_health_rejects_requests_before_browser_initialization():
    main.app.state.browser_manager = None
    main.app.state.job_manager = _FakeJobManager()

    response = TestClient(main.app).get("/health")

    assert response.status_code == 503


def test_health_rejects_requests_when_scheduler_workers_are_unavailable():
    response = _client_with_browser(job_manager=_FakeJobManager(ready=False)).get(
        "/health"
    )

    assert response.status_code == 503
    assert response.json()["detail"]["job_manager_status"] == "unavailable"


def test_main_app_accepts_search_job_without_browser_navigation():
    manager = _FakeJobManager()
    response = _client_with_browser(job_manager=manager).post(
        "/jobs/search",
        headers={"Idempotency-Key": "main-contract.search-1"},
        json={"query": "Fahrrad"},
    )

    assert response.status_code == 202
    assert response.json()["status_url"] == f"/jobs/{JOB_ID}"
    assert manager.calls == [({"query": "Fahrrad"}, "main-contract.search-1")]


def test_main_app_mounts_the_expected_api_methods_once():
    public_routes = {
        (route.path, method)
        for route in main.app.routes
        if hasattr(route, "methods")
        for method in (route.methods or ())
        if method in {"GET", "POST", "DELETE"}
    }

    assert public_routes >= {
        ("/", "GET"),
        ("/health", "GET"),
        ("/outbound-status", "GET"),
        ("/jobs/search", "POST"),
        ("/jobs/details", "POST"),
        ("/jobs/{job_id}", "GET"),
        ("/jobs/{job_id}/results", "GET"),
        ("/jobs/{job_id}/continue", "POST"),
        ("/jobs/{job_id}/resume", "POST"),
        ("/jobs/{job_id}", "DELETE"),
        ("/inserate", "GET"),
        ("/inserat/{id}", "GET"),
        ("/inserate-detailed", "GET"),
        ("/inserate/batch", "POST"),
        ("/inserate-by-url", "POST"),
        ("/convert-url", "POST"),
    }

    inserate_get_routes = [
        route
        for route in main.app.routes
        if getattr(route, "path", None) == "/inserate"
        and "GET" in getattr(route, "methods", set())
    ]
    assert len(inserate_get_routes) == 1

    detailed_get_routes = [
        route
        for route in main.app.routes
        if getattr(route, "path", None) == "/inserate-detailed"
        and "GET" in getattr(route, "methods", set())
    ]
    assert len(detailed_get_routes) == 1


def test_removed_combined_endpoint_returns_gone():
    response = _client_with_browser().get("/inserate-detailed")

    assert response.status_code == 410
    assert response.json()["detail"]["details_endpoint"] == "/jobs/details"
