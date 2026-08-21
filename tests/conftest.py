"""Gemeinsame Pytest-Konfiguration für Offline- und Live-Tests."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from utils.outbound_control import OutboundController, OutboundSettings


LIVE_SEARCH_URL = "https://www.kleinanzeigen.de/s-maerklin-3021/k0"
_LIVE_SEARCH_READY_STATES = {"completed", "awaiting_continuation"}
_LIVE_DETAIL_READY_STATES = {"completed"}
_LIVE_FAILED_STATES = {"needs_attention", "cancelled"}


def make_test_outbound_controller(*, retry_budget: int = 2) -> OutboundController:
    """Create a fast process-local controller for offline compatibility tests."""
    return OutboundController(
        OutboundSettings(
            max_concurrency=10,
            min_interval_seconds=0,
            jitter_seconds=0,
            queue_capacity=100,
            queue_timeout_seconds=0.1,
            navigation_timeout_seconds=1,
            retry_budget=retry_budget,
            cooldown_seconds=1,
        )
    )


@pytest.fixture
def outbound_controller() -> OutboundController:
    return make_test_outbound_controller()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Live-Tests ausführen (benötigt den API-Server und Netzwerkzugriff)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Live-Tests sind ein explizites Opt-in und standardmäßig übersprungen."""
    if config.getoption("--run-live"):
        return

    skip_live = pytest.mark.skip(
        reason="Live-Test deaktiviert; mit --run-live explizit aktivieren"
    )
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip_live)


def _live_poll_timeout() -> float:
    raw = os.environ.get("KLEINANZEIGEN_LIVE_POLL_TIMEOUT_SECONDS", "900")
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise ValueError(
            "KLEINANZEIGEN_LIVE_POLL_TIMEOUT_SECONDS muss eine Zahl sein"
        ) from exc
    if timeout <= 0:
        raise ValueError(
            "KLEINANZEIGEN_LIVE_POLL_TIMEOUT_SECONDS muss größer als 0 sein"
        )
    return timeout


def _response_excerpt(response: httpx.Response) -> str:
    return response.text.replace("\n", " ")[:500]


def _wait_for_live_job(
    client: httpx.Client,
    job_id: str,
    *,
    ready_states: set[str],
) -> dict[str, Any]:
    """Poll an accepted job without imposing a timeout on the server-side job."""

    deadline = time.monotonic() + _live_poll_timeout()
    last_status: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{job_id}")
        assert response.status_code == 200, (
            f"Statusabfrage für Job {job_id} lieferte HTTP {response.status_code}: "
            f"{_response_excerpt(response)}"
        )
        last_status = response.json()
        state = last_status.get("state")
        if state in ready_states:
            return last_status
        if state in _LIVE_FAILED_STATES:
            pytest.fail(f"Live-Job {job_id} benötigt Aufmerksamkeit: {last_status}")
        time.sleep(2)

    pytest.fail(
        "Der testseitige Polling-Zeitraum ist abgelaufen; der persistente Job bleibt "
        f"angenommen und kann weiterlaufen. Letzter Status: {last_status}"
    )


def _read_all_live_results(
    client: httpx.Client,
    results_url: str,
) -> list[dict[str, Any]]:
    """Read all currently released SQLite results without outbound navigation."""

    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": 200}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get(results_url, params=params)
        assert response.status_code == 200, (
            f"Ergebnisabruf lieferte HTTP {response.status_code}: "
            f"{_response_excerpt(response)}"
        )
        page = response.json()
        items.extend(page["items"])
        if not page["has_more"]:
            return items
        cursor = page.get("next_cursor")
        assert cursor, "Ergebnisseite meldet has_more ohne next_cursor"


@pytest.fixture(scope="session")
def live_http_client() -> httpx.Client:
    """Provide one conservative client for explicitly enabled live tests."""

    base_url = os.environ.get(
        "KLEINANZEIGEN_LIVE_BASE_URL", "http://localhost:8000"
    ).rstrip("/")
    timeout = httpx.Timeout(30, connect=5)
    with httpx.Client(base_url=base_url, timeout=timeout) as client:
        try:
            response = client.get("/")
            response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            pytest.skip(
                f"API-Server unter {base_url} nicht erreichbar; zuerst "
                f"uvicorn main:app starten ({exc})"
            )
        yield client


@pytest.fixture(scope="session")
def live_search_snapshot(live_http_client: httpx.Client) -> dict[str, Any]:
    """Submit exactly one narrow live search and reuse its persisted snapshot."""

    run_day = datetime.now(timezone.utc).date().isoformat()
    idempotency_key = f"live-suite-search-v2-{run_day}"
    response = live_http_client.post(
        "/inserate-by-url",
        headers={"Idempotency-Key": idempotency_key},
        json={"url": LIVE_SEARCH_URL, "max_pages": 1},
    )
    assert response.status_code == 202, (
        f"Suchjob lieferte HTTP {response.status_code}: {_response_excerpt(response)}"
    )
    assert response.headers.get("Deprecation") == "true"
    accepted = response.json()
    status = _wait_for_live_job(
        live_http_client,
        accepted["job_id"],
        ready_states=_LIVE_SEARCH_READY_STATES,
    )
    results = _read_all_live_results(live_http_client, accepted["results_url"])
    assert results, (
        "Die bewusst schmale Live-Suche lieferte keine Anzeige; Suchziel bei Bedarf "
        "in tests/conftest.py aktualisieren"
    )
    return {
        "accepted": accepted,
        "status": status,
        "results": results,
        "request": {"url": LIVE_SEARCH_URL},
        "idempotency_key": idempotency_key,
    }


@pytest.fixture(scope="session")
def live_detail_snapshot(
    live_http_client: httpx.Client,
    live_search_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Open one result through a single persistent detail job."""

    search_item = live_search_snapshot["results"][0]
    listing_id = str(search_item["listing_id"])
    run_day = datetime.now(timezone.utc).date().isoformat()
    idempotency_key = f"live-suite-detail-v2-{run_day}-{listing_id}"
    response = live_http_client.get(
        f"/inserat/{listing_id}",
        headers={"Idempotency-Key": idempotency_key},
    )
    assert response.status_code == 202, (
        f"Detailjob lieferte HTTP {response.status_code}: {_response_excerpt(response)}"
    )
    assert response.headers.get("Deprecation") == "true"
    accepted = response.json()
    status = _wait_for_live_job(
        live_http_client,
        accepted["job_id"],
        ready_states=_LIVE_DETAIL_READY_STATES,
    )
    results = _read_all_live_results(live_http_client, accepted["results_url"])
    assert len(results) == 1, f"Einzel-Detailjob lieferte {len(results)} Einträge"
    return {
        "accepted": accepted,
        "status": status,
        "result": results[0],
        "listing_id": listing_id,
        "request": {"listing_ids": [listing_id]},
        "idempotency_key": idempotency_key,
    }
