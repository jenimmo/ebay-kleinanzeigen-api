"""Focused durability and storage-failure regressions for persistent jobs."""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from utils.job_manager import (
    DetailItemResult,
    JobManager,
    JobNotFoundError,
    JobSettings,
    JobStorageUnavailable,
    SearchPageResult,
)


class FakeClock:
    def __init__(self, value: float = 100_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def settings(path: Path) -> JobSettings:
    return JobSettings(
        database_path=path,
        worker_count=1,
        lease_seconds=30,
        heartbeat_seconds=1,
        retry_budget=1,
        poll_interval_seconds=0.01,
        min_dispatch_interval_seconds=0,
        dispatch_jitter_seconds=0,
        cooldown_initial_seconds=1,
        cooldown_max_seconds=4,
        page_setup_timeout_seconds=1,
        extraction_timeout_seconds=1,
        cleanup_timeout_seconds=1,
        retention_days=7,
        min_free_disk_bytes=0,
    )


async def search(_task) -> SearchPageResult:
    return SearchPageResult([{"adid": "3900000000"}], total_results=1, total_pages=1)


async def detail(task) -> DetailItemResult:
    return DetailItemResult("complete", {"id": task.listing_id})


@pytest.mark.asyncio
async def test_sqlite_uses_full_synchronous_durability(tmp_path) -> None:
    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
    )
    await manager.start(start_workers=False)
    try:
        cursor = await manager._require_db().execute("PRAGMA synchronous")
        assert (await cursor.fetchone())[0] == 2  # SQLite FULL
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_periodic_pruning_does_not_require_process_restart(tmp_path) -> None:
    clock = FakeClock()
    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
        clock=clock,
    )
    await manager.start()
    try:
        job = await manager.create_search_job({"query": "retention"})
        await manager.wait_for_idle(2)
        clock.advance(8 * 86_400)

        async with asyncio.timeout(2):
            while True:
                try:
                    await manager.get_job(job["job_id"])
                except JobNotFoundError:
                    break
                await asyncio.sleep(0.01)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_public_read_translates_sqlite_failure_to_controlled_503(
    tmp_path, monkeypatch
) -> None:
    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
    )
    await manager.start(start_workers=False)

    async def broken_fetch(*_args, **_kwargs):
        raise aiosqlite.OperationalError("simulated storage outage")

    monkeypatch.setattr(manager, "_fetch_job_locked", broken_fetch)
    try:
        with pytest.raises(JobStorageUnavailable) as caught:
            await manager.get_job("123e4567-e89b-12d3-a456-426614174000")
        assert caught.value.http_status == 503
        assert "simulated" not in str(caught.value)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_shutdown_during_completion_is_bounded_and_work_is_resumable(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "jobs.sqlite3"
    manager = JobManager(
        settings(database),
        search_executor=search,
        detail_executor=detail,
    )
    completion_entered = asyncio.Event()
    never_complete = asyncio.Event()

    async def blocked_completion(*_args, **_kwargs):
        completion_entered.set()
        await never_complete.wait()

    monkeypatch.setattr(manager, "_complete_detail_work", blocked_completion)
    await manager.start()
    job = await manager.create_detail_job(listing_ids=["3900000000"])
    await asyncio.wait_for(completion_entered.wait(), timeout=0.2)

    await asyncio.wait_for(manager.close(), timeout=0.2)

    recovered = JobManager(
        settings(database),
        search_executor=search,
        detail_executor=detail,
    )
    await recovered.start(start_workers=False)
    try:
        page = await recovered.get_results(job["job_id"], limit=10)
        assert page["items"][0]["status"] == "pending"
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_abandoned_half_open_heartbeat_releases_probe_reservation(
    tmp_path,
) -> None:
    clock = FakeClock()
    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
        clock=clock,
    )
    await manager.start(start_workers=False)
    try:
        await manager.create_search_job({"query": "probe-heartbeat"})
        await manager.save_runtime_state(
            {
                "circuit_reason": "http_429",
                "circuit_open_until": clock() + 1,
                "circuit_generation": 1,
                "block_streak": 1,
            }
        )
        clock.advance(1.1)
        claimed, _ = await manager._claim_next_work()
        assert claimed is not None and claimed.is_probe

        # This is the same release path used after a heartbeat failure has
        # fully cancelled its executor.
        await manager._release_cancelled_execution(claimed)

        runtime = await manager.load_runtime_state()
        assert runtime["circuit"]["probe_in_flight"] is False
        retry_claim, _ = await manager._claim_next_work()
        assert retry_claim is not None and retry_claim.is_probe

        # Even if the explicit release transaction itself had been unavailable,
        # lease recovery must eventually clear the durable probe reservation.
        clock.advance(31)
        recovered_claim, _ = await manager._claim_next_work()
        assert recovered_claim is not None and recovered_claim.is_probe
        assert recovered_claim.work_item_id == retry_claim.work_item_id
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_old_claim_finally_does_not_forget_replacement_executor(
    tmp_path, monkeypatch
) -> None:
    first_executor_started = asyncio.Event()
    replacement_executor_started = asyncio.Event()
    release_replacement = asyncio.Event()
    executor_calls = 0

    async def controlled_detail(task):
        nonlocal executor_calls
        executor_calls += 1
        if executor_calls == 1:
            first_executor_started.set()
            await asyncio.Event().wait()
        replacement_executor_started.set()
        await release_replacement.wait()
        return DetailItemResult("complete", {"id": task.listing_id})

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=controlled_detail,
    )
    await manager.start(start_workers=False)
    replacement_runner = None
    try:
        await manager.create_detail_job(listing_ids=["3900000000"])
        first_claim, _ = await manager._claim_next_work()
        assert first_claim is not None

        async def controlled_heartbeat(claimed):
            if claimed.lease_token == first_claim.lease_token:
                await first_executor_started.wait()
                raise aiosqlite.OperationalError("heartbeat failed")
            await asyncio.Event().wait()

        original_release = manager._release_cancelled_execution

        async def release_then_reclaim(claimed):
            nonlocal replacement_runner
            await original_release(claimed)
            replacement_claim, _ = await manager._claim_next_work()
            assert replacement_claim is not None
            replacement_runner = asyncio.create_task(
                manager._execute_claim(replacement_claim)
            )
            await replacement_executor_started.wait()

        monkeypatch.setattr(manager, "_heartbeat_lease", controlled_heartbeat)
        monkeypatch.setattr(
            manager, "_release_cancelled_execution", release_then_reclaim
        )

        with pytest.raises(aiosqlite.OperationalError, match="heartbeat failed"):
            await manager._execute_claim(first_claim)

        tracked = manager._executions.get(first_claim.work_item_id)
        assert tracked is not None and not tracked.done()
        assert replacement_runner is not None and not replacement_runner.done()

        release_replacement.set()
        await asyncio.wait_for(replacement_runner, timeout=0.2)
    finally:
        if replacement_runner is not None and not replacement_runner.done():
            replacement_runner.cancel()
            await asyncio.gather(replacement_runner, return_exceptions=True)
        await manager.close()
