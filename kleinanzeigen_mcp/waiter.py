"""Bounded waiting and result-collection helpers for durable jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
import inspect
import math
import time
from typing import Protocol, TypeAlias

from .client import KleinanzeigenProtocolError
from .merge import (
    MAX_MERGED_RESULTS,
    MergeError,
    merge_detail_snapshot,
    merge_search_results,
)
from .models import (
    ACTIONABLE_JOB_STATES,
    DetailResultSnapshot,
    JobAccepted,
    JobResultsPage,
    JobState,
    JobStatus,
    MergeSnapshotCursor,
    SearchResultSnapshot,
    WaitProgress,
    WaitResult,
)


MAX_WAIT_SECONDS = 600.0
ProgressCallback: TypeAlias = Callable[[WaitProgress], Awaitable[None] | None]


class JobReadClient(Protocol):
    """Minimal structural client contract used by the helpers."""

    async def get_job(self, job_id: str) -> JobStatus: ...

    async def get_results(
        self,
        job_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> JobResultsPage: ...

    async def cancel_job(self, job_id: str) -> JobAccepted: ...


class JobWaiter:
    """Poll a durable job until it is actionable, terminal or locally timed out."""

    def __init__(
        self,
        client: JobReadClient,
        *,
        poll_interval_seconds: float = 1.0,
        max_poll_interval_seconds: float = 5.0,
        max_wait_seconds: float = MAX_WAIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._poll_interval_seconds = _positive_finite(
            poll_interval_seconds, "poll_interval_seconds"
        )
        self._max_poll_interval_seconds = _positive_finite(
            max_poll_interval_seconds, "max_poll_interval_seconds"
        )
        if self._max_poll_interval_seconds < self._poll_interval_seconds:
            raise ValueError(
                "max_poll_interval_seconds must not be smaller than "
                "poll_interval_seconds"
            )
        self._max_wait_seconds = _positive_finite(max_wait_seconds, "max_wait_seconds")
        if self._max_wait_seconds > MAX_WAIT_SECONDS:
            raise ValueError(
                f"max_wait_seconds must not exceed {MAX_WAIT_SECONDS:g} seconds"
            )
        self._clock = clock
        self._sleep = sleep

    async def wait(
        self,
        job_id: str,
        *,
        timeout_seconds: float | None = None,
        stop_states: Iterable[JobState] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> WaitResult:
        """Return a bounded outcome without cancelling a still-running job.

        By default, waiting stops for terminal states as well as states that
        require an explicit continuation or resume action. A local timeout is a
        normal ``WaitResult`` with ``timed_out=True``; transport/API/callback
        failures remain exceptions.
        """

        timeout = self._max_wait_seconds
        if timeout_seconds is not None:
            timeout = _nonnegative_finite(timeout_seconds, "timeout_seconds")
            if timeout > self._max_wait_seconds:
                raise ValueError(
                    "timeout_seconds must not exceed the configured max_wait_seconds"
                )
        targets = (
            ACTIONABLE_JOB_STATES if stop_states is None else frozenset(stop_states)
        )

        started_at = self._clock()
        deadline = started_at + timeout
        poll_count = 0
        last_status: JobStatus | None = None
        last_signature: tuple[object, ...] | None = None
        next_poll_interval = self._poll_interval_seconds

        while True:
            now = self._clock()
            if last_status is not None and now >= deadline:
                return WaitResult(
                    status=last_status,
                    timed_out=True,
                    poll_count=poll_count,
                    elapsed_seconds=max(0.0, now - started_at),
                )

            status = await self._client.get_job(job_id)
            last_status = status
            poll_count += 1
            signature = (
                status.state,
                repr(status.progress),
                repr(status.results),
                repr(status.cooldown),
            )
            if last_signature is not None and signature == last_signature:
                next_poll_interval = min(
                    self._max_poll_interval_seconds,
                    next_poll_interval * 1.5,
                )
            else:
                next_poll_interval = self._poll_interval_seconds
            last_signature = signature
            observed_at = self._clock()
            progress = WaitProgress(
                job_id=status.job_id,
                state=status.state,
                poll_count=poll_count,
                elapsed_seconds=max(0.0, observed_at - started_at),
                remaining_seconds=max(0.0, deadline - observed_at),
                status=status,
            )
            if progress_callback is not None:
                callback_result = progress_callback(progress)
                if inspect.isawaitable(callback_result):
                    await callback_result

            after_callback = self._clock()
            elapsed = max(0.0, after_callback - started_at)
            if status.state in targets:
                return WaitResult(
                    status=status,
                    timed_out=False,
                    poll_count=poll_count,
                    elapsed_seconds=elapsed,
                )
            if after_callback >= deadline:
                return WaitResult(
                    status=status,
                    timed_out=True,
                    poll_count=poll_count,
                    elapsed_seconds=elapsed,
                )

            await self._sleep(
                min(next_poll_interval, max(0.0, deadline - after_callback))
            )


async def wait_for_job(
    client: JobReadClient,
    job_id: str,
    *,
    poll_interval_seconds: float = 1.0,
    max_poll_interval_seconds: float = 5.0,
    max_wait_seconds: float = MAX_WAIT_SECONDS,
    timeout_seconds: float | None = None,
    stop_states: Iterable[JobState] | None = None,
    progress_callback: ProgressCallback | None = None,
) -> WaitResult:
    """Convenience wrapper for a one-off :class:`JobWaiter`."""

    return await JobWaiter(
        client,
        poll_interval_seconds=poll_interval_seconds,
        max_poll_interval_seconds=max_poll_interval_seconds,
        max_wait_seconds=max_wait_seconds,
    ).wait(
        job_id,
        timeout_seconds=timeout_seconds,
        stop_states=stop_states,
        progress_callback=progress_callback,
    )


async def collect_search_results(
    client: JobReadClient,
    job_id: str,
    *,
    cursor: str | None = None,
    max_results: int = MAX_MERGED_RESULTS,
    page_size: int = MAX_MERGED_RESULTS,
) -> SearchResultSnapshot:
    """Collect currently persisted search pages into one stable snapshot."""

    _bounded_count(max_results, "max_results")
    _bounded_count(page_size, "page_size")
    snapshot = SearchResultSnapshot(
        job_id=job_id,
        cursor=MergeSnapshotCursor(api_cursor=cursor),
    )

    while len(snapshot.items) < max_results:
        previous_cursor = snapshot.cursor.api_cursor
        page = await client.get_results(
            job_id,
            cursor=previous_cursor,
            limit=min(page_size, max_results - len(snapshot.items)),
        )
        try:
            snapshot = merge_search_results(
                snapshot,
                page,
                max_items=max_results,
            )
        except MergeError as exc:
            raise _invalid_results_contract() from exc
        if not page.has_more:
            break
        _require_advancing_cursor(previous_cursor, page)

    return snapshot


async def collect_detail_snapshot(
    client: JobReadClient,
    job_id: str,
    *,
    cursor: str | None = None,
    max_results: int = MAX_MERGED_RESULTS,
    page_size: int = MAX_MERGED_RESULTS,
) -> DetailResultSnapshot:
    """Collect the current detail state starting at an optional opaque cursor."""

    _bounded_count(max_results, "max_results")
    _bounded_count(page_size, "page_size")
    snapshot = DetailResultSnapshot(
        job_id=job_id,
        cursor=MergeSnapshotCursor(api_cursor=cursor),
    )
    return await _collect_detail_pages(
        client,
        snapshot,
        max_results=max_results,
        page_size=page_size,
    )


async def collect_detail_changes(
    client: JobReadClient,
    snapshot: DetailResultSnapshot,
    *,
    max_results: int = MAX_MERGED_RESULTS,
    page_size: int = MAX_MERGED_RESULTS,
) -> DetailResultSnapshot:
    """Apply all currently available detail change-feed pages to a snapshot."""

    _bounded_count(max_results, "max_results")
    _bounded_count(page_size, "page_size")
    if len(snapshot.items) > max_results:
        raise ValueError("max_results cannot be smaller than the current snapshot")
    return await _collect_detail_pages(
        client,
        snapshot,
        max_results=max_results,
        page_size=page_size,
    )


async def _collect_detail_pages(
    client: JobReadClient,
    snapshot: DetailResultSnapshot,
    *,
    max_results: int,
    page_size: int,
) -> DetailResultSnapshot:
    while True:
        previous_cursor = snapshot.cursor.api_cursor
        page = await client.get_results(
            snapshot.job_id,
            cursor=previous_cursor,
            limit=page_size,
        )
        try:
            snapshot = merge_detail_snapshot(
                snapshot,
                page,
                max_items=max_results,
            )
        except MergeError as exc:
            raise _invalid_results_contract() from exc
        if not page.has_more:
            break
        _require_advancing_cursor(previous_cursor, page)
    return snapshot


def _require_advancing_cursor(
    previous_cursor: str | None, page: JobResultsPage
) -> None:
    if page.next_cursor is None or page.next_cursor == previous_cursor:
        raise _invalid_results_contract()


def _invalid_results_contract() -> KleinanzeigenProtocolError:
    return KleinanzeigenProtocolError(
        code="invalid_results_page",
        message="The jobs API returned an inconsistent result page",
    )


def _bounded_count(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not 1 <= value <= MAX_MERGED_RESULTS:
        raise ValueError(f"{name} must be between 1 and {MAX_MERGED_RESULTS}")
    return value


def _positive_finite(value: float, name: str) -> float:
    number = _nonnegative_finite(value, name)
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def _nonnegative_finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a non-negative finite number")
    return number


__all__ = [
    "JobReadClient",
    "JobWaiter",
    "MAX_WAIT_SECONDS",
    "ProgressCallback",
    "collect_detail_changes",
    "collect_detail_snapshot",
    "collect_search_results",
    "wait_for_job",
]
