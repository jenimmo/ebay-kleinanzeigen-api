"""Konservative Live-Vertragstests für den persistenten Zwei-Schritt-Ablauf.

Alle Tests teilen sich einen schmalen Suchjob und einen einzelnen Detailjob aus
``tests/conftest.py``. Idempotente Wiederholungen und Ergebnis-Polling erzeugen
keine weiteren Kleinanzeigen-Navigationen. Es gibt absichtlich keine parallele
Live-Last.
"""

from typing import Any

import httpx
import pytest


pytestmark = pytest.mark.live

EXPECTED_SEARCH_FIELDS = {
    "adid",
    "url",
    "title",
    "price",
    "description",
    "published_at",
}
EXPECTED_DETAIL_FIELDS = {
    "id",
    "url_requested",
    "url_redirected",
    "categories",
    "title",
    "status",
    "price",
    "delivery",
    "location",
    "description",
    "media",
    "details",
    "features",
    "seller",
    "extra_info",
}


def test_root_and_health_advertise_the_job_api(live_http_client: httpx.Client):
    root = live_http_client.get("/")
    health = live_http_client.get("/health")

    assert root.status_code == 200
    assert root.json()["browser_status"] == "optimized"
    assert "/jobs/search" in root.json()["endpoints"]
    assert "/jobs/details" in root.json()["endpoints"]
    assert "/jobs/{job_id}/results" in root.json()["endpoints"]
    assert health.status_code == 200
    assert health.json() == {
        "status": "healthy",
        "browser_status": "optimized",
    }


def test_legacy_search_adapter_returns_202_and_persistent_urls(
    live_search_snapshot: dict[str, Any],
):
    accepted = live_search_snapshot["accepted"]

    assert accepted["type"] == "search"
    assert accepted["status_url"] == f"/jobs/{accepted['job_id']}"
    assert accepted["results_url"] == f"/jobs/{accepted['job_id']}/results"
    assert accepted["state"] in {
        "queued",
        "running",
        "waiting_cooldown",
        "awaiting_continuation",
        "completed",
    }


def test_search_job_exposes_all_current_released_results(
    live_search_snapshot: dict[str, Any],
):
    status = live_search_snapshot["status"]
    results = live_search_snapshot["results"]

    assert status["state"] in {"completed", "awaiting_continuation"}
    assert status["type"] == "search"
    assert status["results"]["available"] == len(results)
    assert status["errors"]["open"] == 0
    assert 0 < len(results) <= 200

    ids: list[str] = []
    for sequence, item in enumerate(results, start=1):
        assert item["sequence"] == sequence
        assert item["status"] == "complete"
        assert item["listing_id"] == str(item["data"]["adid"])
        assert not (EXPECTED_SEARCH_FIELDS - item["data"].keys())
        assert item["data"]["url"].startswith("https://www.kleinanzeigen.de/s-anzeige/")
        ids.append(item["listing_id"])
    assert len(ids) == len(set(ids))


def test_search_continuation_boundary_is_explicit(
    live_search_snapshot: dict[str, Any],
):
    status = live_search_snapshot["status"]

    if status["state"] == "awaiting_continuation":
        assert status["continuation"]["required"] is True
        assert status["results"]["available"] == 200
    else:
        assert status["continuation"]["required"] is False


def test_result_cursor_reads_the_persisted_snapshot_without_new_job(
    live_http_client: httpx.Client,
    live_search_snapshot: dict[str, Any],
):
    results_url = live_search_snapshot["accepted"]["results_url"]
    first = live_http_client.get(results_url, params={"limit": 1})

    assert first.status_code == 200
    first_page = first.json()
    assert len(first_page["items"]) == 1
    if first_page["has_more"]:
        second = live_http_client.get(
            results_url,
            params={"limit": 1, "cursor": first_page["next_cursor"]},
        )
        assert second.status_code == 200
        assert second.json()["items"][0]["sequence"] == 2


def test_canonical_search_submission_is_an_idempotent_replay(
    live_http_client: httpx.Client,
    live_search_snapshot: dict[str, Any],
):
    response = live_http_client.post(
        "/jobs/search",
        headers={"Idempotency-Key": live_search_snapshot["idempotency_key"]},
        json=live_search_snapshot["request"],
    )

    assert response.status_code == 202
    replay = response.json()
    assert replay["job_id"] == live_search_snapshot["accepted"]["job_id"]
    assert replay["idempotent_replay"] is True


def test_single_detail_job_completes_with_a_visible_item(
    live_detail_snapshot: dict[str, Any],
):
    status = live_detail_snapshot["status"]
    result = live_detail_snapshot["result"]
    data = result["data"]

    assert status["state"] == "completed"
    assert status["type"] == "details"
    assert status["progress"]["total_items"] == 1
    assert status["progress"]["processed_items"] == 1
    assert status["progress"]["complete"] == 1
    assert status["progress"]["not_found"] == 0
    assert status["errors"]["open"] == 0
    assert result["sequence"] == 1
    assert result["status"] == "complete"
    assert result["listing_id"] == live_detail_snapshot["listing_id"]
    assert data["id"] == live_detail_snapshot["listing_id"]
    assert not (EXPECTED_DETAIL_FIELDS - data.keys())
    assert data["status"] in {"active", "sold", "reserved"}
    assert data["title"].strip()


def test_detail_result_preserves_canonical_urls(
    live_detail_snapshot: dict[str, Any],
):
    data = live_detail_snapshot["result"]["data"]
    listing_id = live_detail_snapshot["listing_id"]

    assert data["url_requested"] == (
        f"https://www.kleinanzeigen.de/s-anzeige/{listing_id}"
    )
    assert data["url_redirected"].startswith("https://www.kleinanzeigen.de/s-anzeige/")
    assert listing_id in data["url_redirected"]
    assert isinstance(data["categories"], list)
    assert isinstance(data["media"]["images"]["urls"], list)


def test_canonical_detail_submission_is_an_idempotent_replay(
    live_http_client: httpx.Client,
    live_detail_snapshot: dict[str, Any],
):
    response = live_http_client.post(
        "/jobs/details",
        headers={"Idempotency-Key": live_detail_snapshot["idempotency_key"]},
        json=live_detail_snapshot["request"],
    )

    assert response.status_code == 202
    replay = response.json()
    assert replay["job_id"] == live_detail_snapshot["accepted"]["job_id"]
    assert replay["idempotent_replay"] is True


def test_removed_combined_endpoint_returns_410_without_navigation(
    live_http_client: httpx.Client,
):
    response = live_http_client.get(
        "/inserate-detailed",
        params={"query": "wird-nicht-gescraped", "page_count": 1},
    )

    assert response.status_code == 410
    detail = response.json()["detail"]
    assert detail["error"] == "endpoint_removed"
    assert detail["search_endpoint"] == "/jobs/search"
    assert detail["details_endpoint"] == "/jobs/details"


def test_outbound_status_contains_persistent_queue_metrics(
    live_http_client: httpx.Client,
):
    response = live_http_client.get("/outbound-status")

    assert response.status_code == 200
    status = response.json()
    assert "circuit" in status
    assert "jobs" in status
    assert "workers" in status["jobs"]
