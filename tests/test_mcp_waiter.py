"""Deterministische Offline-Vertragstests für bounded MCP-Wartevorgänge."""

from __future__ import annotations

from typing import Any

import pytest

from kleinanzeigen_mcp.client import KleinanzeigenProtocolError
from kleinanzeigen_mcp.models import (
    DetailResultSnapshot,
    JobResultItem,
    JobResultsPage,
    JobStatus,
    MergeSnapshotCursor,
    WaitProgress,
)
from kleinanzeigen_mcp.waiter import (
    JobWaiter,
    collect_detail_changes,
    collect_search_results,
)


JOB_ID = "wait-job"


def _status(state: str) -> JobStatus:
    return JobStatus(
        job_id=JOB_ID,
        type="search",
        state=state,
        progress={},
        results={},
        continuation=(
            {"required": True, "token": "continue-token"}
            if state == "awaiting_continuation"
            else {"required": False}
        ),
        resume=(
            {"required": True, "token": "resume-token"}
            if state == "needs_attention"
            else {"required": False}
        ),
        cooldown={},
        errors={},
    )


class SequenceClient:
    """Minimaler Fake, der nur den vom Waiter benötigten Vertrag anbietet."""

    def __init__(self, states: list[str]) -> None:
        self._states = iter(states)
        self._last = states[-1]
        self.get_calls: list[str] = []
        self.cancel_calls: list[str] = []

    async def get_job(self, job_id: str) -> JobStatus:
        self.get_calls.append(job_id)
        try:
            self._last = next(self._states)
        except StopIteration:
            pass
        return _status(self._last)

    async def cancel_job(self, job_id: str) -> Any:
        self.cancel_calls.append(job_id)
        raise AssertionError(
            "Ein lokaler Timeout darf den langlebigen Job nicht abbrechen"
        )


class ManualTime:
    """Virtuelle Zeit verhindert flaky Timeout-Tests und echte Wartepausen."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        "completed",
        "cancelled",
        "waiting_cooldown",
        "awaiting_continuation",
        "needs_attention",
    ],
)
async def test_actionable_and_terminal_states_return_immediately(state: str) -> None:
    client = SequenceClient([state])
    waiter = JobWaiter(
        client,
        poll_interval_seconds=0.001,
        max_wait_seconds=1,
    )

    result = await waiter.wait(JOB_ID)

    assert result.status.state == state
    assert result.timed_out is False
    assert result.poll_count == 1
    assert client.get_calls == [JOB_ID]


@pytest.mark.asyncio
async def test_waiter_polls_non_actionable_states_and_reports_progress() -> None:
    client = SequenceClient(["queued", "running", "running", "completed"])
    waiter = JobWaiter(
        client,
        poll_interval_seconds=0.001,
        max_wait_seconds=1,
    )
    progress: list[WaitProgress] = []

    async def capture(update: WaitProgress) -> None:
        progress.append(update)

    result = await waiter.wait(JOB_ID, progress_callback=capture)

    assert result.status.state == "completed"
    assert result.timed_out is False
    assert result.poll_count == 4
    assert [update.state for update in progress] == [
        "queued",
        "running",
        "running",
        "completed",
    ]
    assert [update.poll_count for update in progress] == [1, 2, 3, 4]
    assert all(update.job_id == JOB_ID for update in progress)


@pytest.mark.asyncio
async def test_sync_progress_callback_is_supported() -> None:
    client = SequenceClient(["running", "completed"])
    waiter = JobWaiter(
        client,
        poll_interval_seconds=0.001,
        max_wait_seconds=1,
    )
    states: list[str] = []

    def capture(update: WaitProgress) -> None:
        states.append(update.state)

    result = await waiter.wait(JOB_ID, progress_callback=capture)

    assert result.status.state == "completed"
    assert states == ["running", "completed"]


@pytest.mark.asyncio
async def test_timeout_returns_last_status_without_cancelling_backend_job() -> None:
    client = SequenceClient(["running"])
    manual_time = ManualTime()
    waiter = JobWaiter(
        client,
        poll_interval_seconds=0.004,
        max_poll_interval_seconds=0.004,
        max_wait_seconds=0.01,
        clock=manual_time,
        sleep=manual_time.sleep,
    )

    result = await waiter.wait(JOB_ID)

    assert result.status.state == "running"
    assert result.timed_out is True
    assert result.poll_count == 3
    assert result.elapsed_seconds == pytest.approx(0.01)
    assert client.cancel_calls == []


@pytest.mark.asyncio
async def test_unchanged_status_uses_bounded_adaptive_polling() -> None:
    client = SequenceClient(["running"])
    manual_time = ManualTime()
    sleeps: list[float] = []

    async def capture_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await manual_time.sleep(seconds)

    waiter = JobWaiter(
        client,
        poll_interval_seconds=1,
        max_poll_interval_seconds=5,
        max_wait_seconds=20,
        clock=manual_time,
        sleep=capture_sleep,
    )

    result = await waiter.wait(JOB_ID, timeout_seconds=10)

    assert result.timed_out is True
    assert sleeps == [1, 1.5, 2.25, 3.375, 1.875]
    assert all(1 <= delay <= 5 for delay in sleeps)


@pytest.mark.asyncio
async def test_callback_exception_is_not_swallowed() -> None:
    client = SequenceClient(["running"])
    waiter = JobWaiter(
        client,
        poll_interval_seconds=0.001,
        max_wait_seconds=1,
    )

    def broken_callback(update: WaitProgress) -> None:
        raise RuntimeError(f"callback failed in {update.state}")

    with pytest.raises(RuntimeError, match="callback failed in running"):
        await waiter.wait(JOB_ID, progress_callback=broken_callback)

    assert client.get_calls == [JOB_ID]
    assert client.cancel_calls == []


@pytest.mark.asyncio
async def test_detail_collector_applies_all_changes_from_stable_cursor() -> None:
    initial = DetailResultSnapshot(
        job_id="detail-job",
        items=(
            JobResultItem(
                sequence=1,
                listing_id="listing-a",
                status="pending",
            ),
        ),
        cursor=MergeSnapshotCursor(
            api_cursor="detail-cursor-1",
            seen_listing_ids=("listing-a",),
        ),
    )
    pages = {
        "detail-cursor-1": JobResultsPage(
            job_id="detail-job",
            type="details",
            items=[
                JobResultItem(
                    sequence=3,
                    listing_id="listing-a",
                    status="complete",
                    data={"title": "A"},
                ),
                JobResultItem(
                    sequence=2,
                    listing_id="listing-b",
                    status="complete",
                    data={"title": "B"},
                ),
            ],
            next_cursor="detail-cursor-3",
            has_more=True,
        ),
        "detail-cursor-3": JobResultsPage(
            job_id="detail-job",
            type="details",
            items=[
                JobResultItem(
                    sequence=4,
                    listing_id="listing-a",
                    status="error",
                    error={"code": "detail_failed", "message": "Fehler"},
                ),
            ],
            next_cursor="detail-cursor-4",
            has_more=False,
        ),
    }

    class DetailClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str | None, int]] = []

        async def get_results(
            self,
            job_id: str,
            *,
            cursor: str | None = None,
            limit: int = 50,
        ) -> JobResultsPage:
            assert job_id == "detail-job"
            self.calls.append((cursor, limit))
            assert cursor is not None
            return pages[cursor]

    client = DetailClient()
    result = await collect_detail_changes(client, initial, page_size=17)

    assert client.calls == [
        ("detail-cursor-1", 17),
        ("detail-cursor-3", 17),
    ]
    assert tuple(item.listing_id for item in result.items) == (
        "listing-a",
        "listing-b",
    )
    assert result.items[0].sequence == 4
    assert result.items[0].status == "error"
    assert result.items[1].sequence == 2
    assert result.cursor.api_cursor == "detail-cursor-4"
    assert result.cursor.seen_listing_ids == ("listing-a", "listing-b")


@pytest.mark.asyncio
async def test_search_collector_follows_opaque_cursors_and_remaining_limit() -> None:
    pages = {
        None: JobResultsPage(
            job_id="search-job",
            type="search",
            items=[
                JobResultItem(
                    sequence=1,
                    listing_id="listing-a",
                    status="complete",
                    data={"title": "A zuerst"},
                )
            ],
            next_cursor="opaque:1/?",
            has_more=True,
        ),
        "opaque:1/?": JobResultsPage(
            job_id="search-job",
            type="search",
            items=[
                JobResultItem(
                    sequence=2,
                    listing_id="listing-a",
                    status="complete",
                    data={"title": "A Duplikat"},
                ),
                JobResultItem(
                    sequence=3,
                    listing_id="listing-b",
                    status="complete",
                    data={"title": "B"},
                ),
            ],
            next_cursor="opaque:2/?",
            has_more=True,
        ),
        "opaque:2/?": JobResultsPage(
            job_id="search-job",
            type="search",
            items=[
                JobResultItem(
                    sequence=4,
                    listing_id="listing-c",
                    status="complete",
                    data={"title": "C"},
                )
            ],
            next_cursor="opaque:3/?",
            has_more=False,
        ),
    }

    class SearchClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str | None, int]] = []

        async def get_results(
            self,
            job_id: str,
            *,
            cursor: str | None = None,
            limit: int = 50,
        ) -> JobResultsPage:
            assert job_id == "search-job"
            self.calls.append((cursor, limit))
            return pages[cursor]

    client = SearchClient()
    result = await collect_search_results(
        client,
        "search-job",
        max_results=3,
        page_size=2,
    )

    assert client.calls == [
        (None, 2),
        ("opaque:1/?", 2),
        ("opaque:2/?", 1),
    ]
    assert tuple(item.listing_id for item in result.items) == (
        "listing-a",
        "listing-b",
        "listing-c",
    )
    assert result.items[0].data == {"title": "A zuerst"}
    assert result.cursor.api_cursor == "opaque:3/?"
    assert result.cursor.seen_listing_ids == (
        "listing-a",
        "listing-b",
        "listing-c",
    )


@pytest.mark.asyncio
async def test_collector_rejects_non_advancing_pagination_cursor() -> None:
    class StalledClient:
        async def get_results(
            self,
            job_id: str,
            *,
            cursor: str | None = None,
            limit: int = 50,
        ) -> JobResultsPage:
            return JobResultsPage(
                job_id=job_id,
                type="search",
                items=[],
                next_cursor=cursor,
                has_more=True,
            )

    with pytest.raises(KleinanzeigenProtocolError) as raised:
        await collect_search_results(StalledClient(), "search-job")

    assert raised.value.code == "invalid_results_page"
