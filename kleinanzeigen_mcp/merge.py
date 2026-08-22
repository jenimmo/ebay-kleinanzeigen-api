"""Stable, bounded merge helpers for job-result cursor pages.

Search pages are immutable snapshots and therefore retain the first envelope
for a ``listing_id``.  Detail pages form a change feed and retain the newest
sequence for each envelope ``listing_id``.  Payload IDs and URLs are never used
for deduplication.
"""

from __future__ import annotations

from .models import (
    DetailResultSnapshot,
    JobResultItem,
    JobResultsPage,
    MAX_SNAPSHOT_ITEMS,
    MergeSnapshotCursor,
    SearchResultSnapshot,
)


MAX_MERGED_RESULTS = MAX_SNAPSHOT_ITEMS


class MergeError(ValueError):
    """A result page cannot be merged into the supplied snapshot."""


def merge_search_results(
    snapshot: SearchResultSnapshot,
    page: JobResultsPage,
    *,
    max_items: int = MAX_MERGED_RESULTS,
) -> SearchResultSnapshot:
    """Merge a search page using stable first-seen order and values."""

    _validate_merge(snapshot.job_id, len(snapshot.items), page, "search", max_items)
    items = list(snapshot.items)
    seen = set(snapshot.cursor.seen_listing_ids)
    dropped = False

    for item in page.items:
        if item.listing_id in seen:
            continue
        if len(items) >= max_items:
            dropped = True
            continue
        items.append(item)
        seen.add(item.listing_id)

    return SearchResultSnapshot(
        job_id=snapshot.job_id,
        items=tuple(items),
        cursor=_next_cursor(snapshot.cursor, page, items),
        has_more=page.has_more,
        truncated=(
            snapshot.truncated or dropped or (len(items) >= max_items and page.has_more)
        ),
        pages_merged=snapshot.pages_merged + 1,
    )


def merge_detail_snapshot(
    snapshot: DetailResultSnapshot,
    page: JobResultsPage,
    *,
    max_items: int = MAX_MERGED_RESULTS,
) -> DetailResultSnapshot:
    """Apply a detail change-feed page while retaining first-seen ID order."""

    _validate_merge(snapshot.job_id, len(snapshot.items), page, "details", max_items)
    items = list(snapshot.items)
    positions = {item.listing_id: index for index, item in enumerate(items)}
    dropped = False

    for item in page.items:
        position = positions.get(item.listing_id)
        if position is not None:
            # Replayed/overlapping change pages must never roll a detail state back.
            if item.sequence >= items[position].sequence:
                items[position] = item
            continue
        if len(items) >= max_items:
            dropped = True
            continue
        positions[item.listing_id] = len(items)
        items.append(item)

    return DetailResultSnapshot(
        job_id=snapshot.job_id,
        items=tuple(items),
        cursor=_next_cursor(snapshot.cursor, page, items),
        has_more=page.has_more,
        truncated=(
            snapshot.truncated or dropped or (len(items) >= max_items and page.has_more)
        ),
        pages_merged=snapshot.pages_merged + 1,
    )


def apply_detail_changes(
    snapshot: DetailResultSnapshot,
    page: JobResultsPage,
    *,
    max_items: int = MAX_MERGED_RESULTS,
) -> DetailResultSnapshot:
    """Explicit change-feed spelling of :func:`merge_detail_snapshot`."""

    return merge_detail_snapshot(snapshot, page, max_items=max_items)


def _validate_merge(
    snapshot_job_id: str,
    snapshot_size: int,
    page: JobResultsPage,
    expected_type: str,
    max_items: int,
) -> None:
    if isinstance(max_items, bool) or not isinstance(max_items, int):
        raise MergeError("max_items must be an integer")
    if not 1 <= max_items <= MAX_MERGED_RESULTS:
        raise MergeError(f"max_items must be between 1 and {MAX_MERGED_RESULTS}")
    if snapshot_size > max_items:
        raise MergeError("max_items cannot be smaller than the current snapshot")
    if page.job_id != snapshot_job_id:
        raise MergeError("result page belongs to a different job")
    if page.type != expected_type:
        raise MergeError(f"expected a {expected_type} job result page, got {page.type}")


def _next_cursor(
    previous: MergeSnapshotCursor,
    page: JobResultsPage,
    items: list[JobResultItem],
) -> MergeSnapshotCursor:
    # Some terminal pages omit next_cursor. Retaining the last opaque API cursor
    # keeps a snapshot resumable without inventing or decoding cursor contents.
    api_cursor = page.next_cursor
    if api_cursor is None:
        api_cursor = previous.api_cursor
    return MergeSnapshotCursor(
        api_cursor=api_cursor,
        seen_listing_ids=tuple(item.listing_id for item in items),
    )


__all__ = [
    "MAX_MERGED_RESULTS",
    "MergeError",
    "apply_detail_changes",
    "merge_detail_snapshot",
    "merge_search_results",
]
