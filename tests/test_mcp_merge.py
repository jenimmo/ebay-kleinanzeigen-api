"""Offline-Vertragstests für stabile MCP-Ergebnis-Snapshots."""

from __future__ import annotations

import pytest

from kleinanzeigen_mcp.merge import merge_detail_snapshot, merge_search_results
from kleinanzeigen_mcp.models import (
    DetailResultSnapshot,
    JobResultItem,
    JobResultsPage,
    SearchResultSnapshot,
)


SEARCH_JOB_ID = "search-job"
DETAIL_JOB_ID = "detail-job"


def _item(
    sequence: int,
    listing_id: str,
    *,
    title: str | None = None,
    status: str = "complete",
    error: dict[str, str] | None = None,
) -> JobResultItem:
    data = (
        None
        if title is None
        else {
            "adid": "same-payload-id",
            "url": "https://www.kleinanzeigen.de/s-anzeige/shared/999",
            "title": title,
        }
    )
    return JobResultItem(
        sequence=sequence,
        listing_id=listing_id,
        status=status,
        data=data,
        error=error,
    )


def _page(
    *items: JobResultItem,
    job_id: str = SEARCH_JOB_ID,
    kind: str = "search",
    next_cursor: str | None = None,
    has_more: bool = False,
) -> JobResultsPage:
    return JobResultsPage(
        job_id=job_id,
        type=kind,
        items=list(items),
        next_cursor=next_cursor,
        has_more=has_more,
    )


def test_search_merge_deduplicates_only_by_envelope_listing_id() -> None:
    snapshot = SearchResultSnapshot(job_id=SEARCH_JOB_ID)
    page = _page(
        _item(1, "listing-a", title="erste Fassung"),
        # Payload-ID und URL sind absichtlich gleich: Die Envelope-ID ist maßgeblich.
        _item(2, "listing-b", title="anderes Inserat"),
        _item(3, "listing-a", title="spätere Fassung"),
        next_cursor="opaque-page-1",
    )

    merged = merge_search_results(snapshot, page)

    assert tuple(item.listing_id for item in merged.items) == (
        "listing-a",
        "listing-b",
    )
    assert merged.items[0].data is not None
    assert merged.items[0].data["title"] == "erste Fassung"
    assert merged.cursor.seen_listing_ids == ("listing-a", "listing-b")
    assert merged.cursor.api_cursor == "opaque-page-1"
    assert merged.pages_merged == 1
    assert merged.has_more is False
    assert merged.truncated is False


def test_search_merge_keeps_first_seen_order_across_cursor_pages() -> None:
    first = merge_search_results(
        SearchResultSnapshot(job_id=SEARCH_JOB_ID),
        _page(
            _item(1, "listing-b", title="B"),
            _item(2, "listing-a", title="A alt"),
            next_cursor="opaque:1/?",
            has_more=True,
        ),
    )
    second = merge_search_results(
        first,
        _page(
            _item(3, "listing-a", title="A neu"),
            _item(4, "listing-c", title="C"),
            next_cursor="opaque:2/?",
            has_more=False,
        ),
    )

    assert tuple(item.listing_id for item in second.items) == (
        "listing-b",
        "listing-a",
        "listing-c",
    )
    assert second.items[1].data is not None
    assert second.items[1].data["title"] == "A alt"
    assert second.cursor.seen_listing_ids == (
        "listing-b",
        "listing-a",
        "listing-c",
    )
    assert second.cursor.api_cursor == "opaque:2/?"
    assert second.pages_merged == 2
    assert second.has_more is False


def test_search_merge_caps_snapshot_and_marks_dropped_results() -> None:
    merged = merge_search_results(
        SearchResultSnapshot(job_id=SEARCH_JOB_ID),
        _page(
            _item(1, "listing-a", title="A"),
            _item(2, "listing-b", title="B"),
            _item(3, "listing-c", title="C"),
            next_cursor="after-three",
            has_more=False,
        ),
        max_items=2,
    )

    assert tuple(item.listing_id for item in merged.items) == (
        "listing-a",
        "listing-b",
    )
    assert merged.cursor.seen_listing_ids == ("listing-a", "listing-b")
    assert merged.cursor.api_cursor == "after-three"
    assert merged.truncated is True


def test_merge_rejects_page_from_another_job() -> None:
    with pytest.raises(ValueError, match="job"):
        merge_search_results(
            SearchResultSnapshot(job_id=SEARCH_JOB_ID),
            _page(
                _item(1, "listing-a", title="A"),
                job_id="wrong-job",
            ),
        )


def test_detail_change_feed_replaces_state_but_keeps_first_seen_order() -> None:
    first = merge_detail_snapshot(
        DetailResultSnapshot(job_id=DETAIL_JOB_ID),
        _page(
            _item(1, "listing-a", status="pending"),
            _item(2, "listing-b", title="B", status="complete"),
            _item(3, "listing-a", title="A vollständig", status="complete"),
            job_id=DETAIL_JOB_ID,
            kind="details",
            next_cursor="detail-3",
            has_more=True,
        ),
    )
    second = merge_detail_snapshot(
        first,
        _page(
            _item(
                4,
                "listing-a",
                status="error",
                error={"code": "detail_failed", "message": "Abruf fehlgeschlagen"},
            ),
            job_id=DETAIL_JOB_ID,
            kind="details",
            next_cursor="detail-4",
            has_more=False,
        ),
    )

    assert tuple(item.listing_id for item in first.items) == (
        "listing-a",
        "listing-b",
    )
    assert first.items[0].status == "complete"
    assert first.items[0].sequence == 3
    assert tuple(item.listing_id for item in second.items) == (
        "listing-a",
        "listing-b",
    )
    assert second.items[0].status == "error"
    assert second.items[0].sequence == 4
    assert second.items[0].data is None
    assert second.cursor.api_cursor == "detail-4"
    assert second.cursor.seen_listing_ids == ("listing-a", "listing-b")
