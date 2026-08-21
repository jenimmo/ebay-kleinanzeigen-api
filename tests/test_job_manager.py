import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from utils.job_manager import (
    DetailItemResult,
    InvalidCursorError,
    JobConflictError,
    JobManager,
    JobManagerAlreadyRunning,
    JobNotFoundError,
    JobSettings,
    JobStorageUnavailable,
    RetryableJobError,
    SearchPageResult,
)
from utils.outbound_control import UpstreamRateLimited


class FakeClock:
    def __init__(self, value: float = 10_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def settings(path: Path, **overrides) -> JobSettings:
    values = {
        "database_path": path,
        "worker_count": 1,
        "lease_seconds": 30.0,
        "heartbeat_seconds": 1.0,
        "retry_budget": 1,
        "poll_interval_seconds": 0.01,
        "min_dispatch_interval_seconds": 0.0,
        "dispatch_jitter_seconds": 0.0,
        "cooldown_initial_seconds": 1.0,
        "cooldown_max_seconds": 4.0,
        "page_setup_timeout_seconds": 1.0,
        "extraction_timeout_seconds": 1.0,
        "cleanup_timeout_seconds": 1.0,
        "retention_days": 7,
        "min_free_disk_bytes": 0,
    }
    values.update(overrides)
    return JobSettings(**values)


async def wait_for_state(
    manager: JobManager,
    job_id: str,
    expected: str,
    *,
    timeout: float = 5.0,
) -> dict:
    async with asyncio.timeout(timeout):
        while True:
            status = await manager.get_job(job_id)
            if status["state"] == expected:
                return status
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_parallel_searches_and_109_details_are_lossless_and_fair(tmp_path):
    totals = {"alpha": 60, "beta": 14, "gamma": 35}
    search_order: list[str] = []

    async def search(task):
        query = task.search["query"]
        search_order.append(query)
        total = totals[query]
        start = (task.page_number - 1) * 25
        stop = min(start + 25, total)
        base = {"alpha": 3_000_000_000, "beta": 3_100_000_000, "gamma": 3_200_000_000}[
            query
        ]
        return SearchPageResult(
            [
                {"adid": str(base + index), "title": f"{query}-{index}"}
                for index in range(start, stop)
            ],
            total_results=total,
            total_pages=(total + 24) // 25,
            page_size=25,
        )

    detail_order: list[str] = []

    async def detail(task):
        detail_order.append(task.job_id)
        return DetailItemResult("complete", {"id": task.listing_id})

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
    )
    await manager.start()
    try:
        search_jobs = await asyncio.gather(
            *(manager.create_search_job({"query": query}) for query in totals)
        )
        await manager.wait_for_idle(5)
        for job, expected in zip(search_jobs, totals.values(), strict=True):
            status = await manager.get_job(job["job_id"])
            results = await manager.get_results(job["job_id"], limit=200)
            assert status["state"] == "completed"
            assert status["results"]["available"] == expected
            assert len(results["items"]) == expected

        # No multi-page search can monopolize a complete scheduler round.
        assert set(search_order[:3]) == set(totals)

        detail_jobs = await asyncio.gather(
            *(
                manager.create_detail_job(
                    {"source_search_job_id": search_job["job_id"]}
                )
                for search_job in search_jobs
            )
        )
        await manager.wait_for_idle(10)
        collected = []
        for job in detail_jobs:
            page = await manager.get_results(job["job_id"], limit=200)
            assert (await manager.get_job(job["job_id"]))["state"] == "completed"
            collected.extend(page["items"])
        assert len(collected) == 109
        assert {item["status"] for item in collected} == {"complete"}
        assert len(detail_order) == 109
        assert len(set(detail_order[:3])) == 3
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_search_pauses_at_200_and_releases_overhang_only_after_continue(tmp_path):
    async def search(_task):
        return SearchPageResult(
            [
                {"adid": str(3_300_000_000 + index), "is_topad": index == 0}
                for index in range(201)
            ],
            total_results=201,
            total_pages=1,
            page_size=201,
        )

    async def detail(_task):
        raise AssertionError("detail executor must not run")

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
    )
    await manager.start()
    try:
        job = await manager.create_search_job({})
        status = await wait_for_state(manager, job["job_id"], "awaiting_continuation")
        first = await manager.get_results(job["job_id"], limit=200)
        assert status["results"] == {
            "discovered": 201,
            "available": 200,
            "reported_total": 201,
            "reported_pages": 1,
        }
        assert len(first["items"]) == 200
        assert first["has_more"] is False
        assert first["next_cursor"] is not None

        continuation_token = status["continuation"]["token"]
        continued = await manager.continue_job(
            job["job_id"], continuation_token=continuation_token
        )
        assert continued["state"] == "completed"
        final_page = await manager.get_results(
            job["job_id"], cursor=first["next_cursor"], limit=200
        )
        assert len(final_page["items"]) == 1
        assert final_page["items"][0]["sequence"] == 201
        replay = await manager.continue_job(
            job["job_id"], continuation_token=continuation_token
        )
        assert replay["idempotent_replay"] is True
        assert replay["state"] == "completed"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_continuation_token_retry_never_releases_two_tranches(
    tmp_path, monkeypatch
):
    async def search(_task):
        return SearchPageResult(
            [{"adid": str(3_325_000_000 + index)} for index in range(500)],
            total_results=500,
            total_pages=1,
            page_size=500,
        )

    async def detail(_task):
        raise AssertionError

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
    )
    await manager.start()
    try:
        job = await manager.create_search_job({"query": "five-hundred"})
        status = await wait_for_state(manager, job["job_id"], "awaiting_continuation")
        first_token = status["continuation"]["token"]
        assert status["results"]["available"] == 200

        original_get_job = manager.get_job
        original_finish = manager._finish_db_operation
        cancel_next_ack = True
        request_task = asyncio.current_task()

        async def forbidden_post_commit_read(_job_id):
            raise AssertionError("continue ACK must not read after commit")

        async def cancel_after_durable_commit(operation):
            nonlocal cancel_next_ack
            result = await original_finish(operation)
            if cancel_next_ack and asyncio.current_task() is request_task:
                cancel_next_ack = False
                raise asyncio.CancelledError
            return result

        monkeypatch.setattr(manager, "get_job", forbidden_post_commit_read)
        monkeypatch.setattr(
            manager,
            "_finish_db_operation",
            cancel_after_durable_commit,
        )
        # Cancellation is delivered only after COMMIT completed, modelling an
        # HTTP disconnect in the otherwise ambiguous durable-ACK window.
        with pytest.raises(asyncio.CancelledError):
            await manager.continue_job(job["job_id"], continuation_token=first_token)

        # Retrying the exact token acknowledges the committed tranche without
        # accidentally releasing results 401-500.
        retry_ack = await manager.continue_job(
            job["job_id"], continuation_token=first_token
        )

        assert retry_ack["idempotent_replay"] is True
        assert retry_ack["continuation"]["approved_limit"] == 400
        assert retry_ack["continuation"]["revision"] == 1

        second_token = retry_ack["continuation"]["token"]
        second_ack = await manager.continue_job(
            job["job_id"], continuation_token=second_token
        )
        second_retry = await manager.continue_job(
            job["job_id"], continuation_token=second_token
        )
        assert second_ack["state"] == "completed"
        assert second_retry["idempotent_replay"] is True

        old_replay = await manager.continue_job(
            job["job_id"], continuation_token=first_token
        )
        assert old_replay["idempotent_replay"] is True
        assert old_replay["continuation"]["approved_limit"] == 400

        monkeypatch.setattr(manager, "get_job", original_get_job)
        final_status = await manager.get_job(job["job_id"])
        assert final_status["results"]["available"] == 500
        assert final_status["continuation"] == {
            "required": False,
            "chunk_size": 200,
            "approved_limit": 600,
            "revision": 2,
            "token": None,
        }
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_running_search_cursor_reads_only_later_results(tmp_path):
    second_page_started = asyncio.Event()
    release_second_page = asyncio.Event()

    async def search(task):
        start = (task.page_number - 1) * 25
        if task.page_number == 2:
            second_page_started.set()
            await release_second_page.wait()
        return SearchPageResult(
            [
                {"adid": str(3_330_000_000 + index)}
                for index in range(start, start + 25)
            ],
            total_results=50,
            total_pages=2,
            page_size=25,
        )

    async def detail(_task):
        raise AssertionError

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
    )
    await manager.start()
    try:
        job = await manager.create_search_job({"query": "growing"})
        await second_page_started.wait()

        first_page = await manager.get_results(job["job_id"], limit=50)
        assert len(first_page["items"]) == 25
        assert first_page["has_more"] is False
        assert first_page["next_cursor"] is not None

        release_second_page.set()
        await wait_for_state(manager, job["job_id"], "completed")
        later = await manager.get_results(
            job["job_id"], cursor=first_page["next_cursor"], limit=50
        )
        assert len(later["items"]) == 25
        assert [item["sequence"] for item in later["items"]] == list(range(26, 51))
    finally:
        release_second_page.set()
        await manager.close()


@pytest.mark.asyncio
async def test_cross_page_and_promoted_duplicates_are_deduplicated(tmp_path):
    async def search(task):
        if task.page_number == 1:
            listings = [
                {
                    "adid": str(3_350_000_000 + index),
                    "promotion": "top" if index == 0 else None,
                }
                for index in range(150)
            ]
        else:
            listings = [
                {"adid": "3350000149", "promotion": "pro"},
                *[{"adid": str(3_350_000_000 + index)} for index in range(150, 201)],
            ]
        return SearchPageResult(
            listings, total_results=201, total_pages=2, page_size=150
        )

    async def detail(_task):
        raise AssertionError

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
    )
    await manager.start()
    try:
        job = await manager.create_search_job({"query": "dedupe"})
        status = await wait_for_state(manager, job["job_id"], "awaiting_continuation")
        assert status["results"]["discovered"] == 201
        await manager.continue_job(
            job["job_id"],
            continuation_token=status["continuation"]["token"],
        )
        first = await manager.get_results(job["job_id"], limit=200)
        second = await manager.get_results(
            job["job_id"], cursor=first["next_cursor"], limit=200
        )
        ids = [item["listing_id"] for item in first["items"] + second["items"]]
        assert len(ids) == len(set(ids)) == 201
        assert "3350000149" in ids
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_idempotency_and_opaque_cursor_are_stable(tmp_path):
    calls = 0

    async def search(_task):
        nonlocal calls
        calls += 1
        return SearchPageResult(
            [{"adid": str(3_400_000_000 + index)} for index in range(3)],
            total_results=3,
            total_pages=1,
        )

    async def detail(_task):
        raise AssertionError

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
    )
    await manager.start(start_workers=False)
    try:
        first = await manager.create_search_job(
            {"query": "one"}, idempotency_key="same-request"
        )
        replay = await manager.create_search_job(
            {"query": "one"}, idempotency_key="same-request"
        )
        assert replay["job_id"] == first["job_id"]
        assert replay["idempotent_replay"] is True
        with pytest.raises(JobConflictError):
            await manager.create_search_job(
                {"query": "different"}, idempotency_key="same-request"
            )

        assert calls == 0
        await manager.start_workers()
        await manager.wait_for_idle()
        page = await manager.get_results(first["job_id"], limit=2)
        assert len(page["items"]) == 2
        assert page["has_more"] is True
        last = await manager.get_results(
            first["job_id"], cursor=page["next_cursor"], limit=2
        )
        assert [item["sequence"] for item in last["items"]] == [3]
        with pytest.raises(InvalidCursorError):
            await manager.get_results(first["job_id"], cursor="not-a-cursor")
        assert calls == 1
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_create_ack_has_no_fallible_post_commit_status_read(
    tmp_path, monkeypatch
):
    async def search(_task):
        raise AssertionError("workers remain stopped")

    async def detail(_task):
        raise AssertionError("workers remain stopped")

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
    )
    await manager.start(start_workers=False)
    try:

        async def forbidden_status_read(_job_id):
            raise AssertionError("create must not read status after commit")

        monkeypatch.setattr(manager, "get_job", forbidden_status_read)

        accepted = await manager.create_search_job({"query": "ack"})
        keyed = await manager.create_search_job(
            {"query": "idempotent"}, idempotency_key="stable-ack"
        )
        replay = await manager.create_search_job(
            {"query": "idempotent"}, idempotency_key="stable-ack"
        )

        assert accepted == {
            "job_id": accepted["job_id"],
            "type": "search",
            "state": "queued",
            "idempotent_replay": False,
        }
        assert keyed["idempotent_replay"] is False
        assert replay == {**keyed, "idempotent_replay": True}

        db = manager._require_db()
        async with manager._db_lock:
            cursor = await db.execute(
                "SELECT id FROM jobs WHERE id IN (?, ?) ORDER BY id",
                (accepted["job_id"], keyed["job_id"]),
            )
            persisted = await cursor.fetchall()
        assert len(persisted) == 2
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_detail_change_cursor_never_skips_final_state_updates(tmp_path):
    async def search(_task):
        raise AssertionError

    async def detail(task):
        return DetailItemResult("complete", {"id": task.listing_id})

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
    )
    await manager.start(start_workers=False)
    try:
        job = await manager.create_detail_job(
            {"listing_ids": ["3456700001", "3456700002"]}
        )
        first_page = await manager.get_results(job["job_id"], limit=1)
        assert first_page["items"] == [
            {
                "sequence": 1,
                "listing_id": "3456700001",
                "status": "pending",
                "data": None,
            }
        ]
        assert first_page["next_cursor"] is not None

        first_claim, _ = await manager._claim_next_work()
        assert first_claim is not None
        await manager._execute_claim(first_claim)

        changes = await manager.get_results(
            job["job_id"], cursor=first_page["next_cursor"], limit=10
        )
        assert any(
            item["listing_id"] == "3456700001"
            and item["status"] == "complete"
            and item["data"] == {"id": "3456700001"}
            for item in changes["items"]
        )
        assert changes["next_cursor"] is not None

        second_claim, _ = await manager._claim_next_work()
        assert second_claim is not None
        await manager._execute_claim(second_claim)
        final_changes = await manager.get_results(
            job["job_id"], cursor=changes["next_cursor"], limit=10
        )
        assert any(
            item["listing_id"] == "3456700002" and item["status"] == "complete"
            for item in final_changes["items"]
        )

        # A fresh traversal after completion remains a duplicate-free snapshot.
        snapshot = await manager.get_results(job["job_id"], limit=10)
        assert len(snapshot["items"]) == 2
        assert {item["status"] for item in snapshot["items"]} == {"complete"}
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_schema_v1_detail_rows_are_backfilled_for_change_cursors(tmp_path):
    database = tmp_path / "jobs.sqlite3"

    async def search(_task):
        raise AssertionError

    async def detail(_task):
        raise AssertionError

    first = JobManager(
        settings(database),
        search_executor=search,
        detail_executor=detail,
    )
    await first.start(start_workers=False)
    job = await first.create_detail_job({"listing_ids": ["3456700003"]})
    await first.close()

    # Reproduce the persistent shape from schema v1, which had work_items but
    # no detail change-event table or triggers.
    database_connection = await aiosqlite.connect(database)
    await database_connection.executescript(
        """
        DROP TRIGGER IF EXISTS detail_result_event_after_insert;
        DROP TRIGGER IF EXISTS detail_result_event_after_state_change;
        DROP TABLE detail_result_events;
        ALTER TABLE jobs DROP COLUMN continuation_revision;
        ALTER TABLE jobs DROP COLUMN resume_revision;
        PRAGMA user_version = 1;
        """
    )
    await database_connection.commit()
    await database_connection.close()

    restarted = JobManager(
        settings(database),
        search_executor=search,
        detail_executor=detail,
    )
    await restarted.start(start_workers=False)
    try:
        page = await restarted.get_results(job["job_id"])
        assert page["items"] == [
            {
                "sequence": 1,
                "listing_id": "3456700003",
                "status": "pending",
                "data": None,
            }
        ]
        assert page["next_cursor"] is not None
        version_cursor = await restarted._require_db().execute("PRAGMA user_version")
        assert (await version_cursor.fetchone())[0] == 4
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_backlog_has_no_queue_age_or_total_job_timeout(tmp_path):
    gate = asyncio.Event()
    started = asyncio.Event()

    async def search(_task):
        return SearchPageResult([], total_results=0, total_pages=1)

    async def detail(task):
        started.set()
        await gate.wait()
        return DetailItemResult("complete", {"id": task.listing_id})

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
    )
    await manager.start()
    try:
        job = await manager.create_detail_job(
            {"listing_ids": [str(3_500_000_000 + index) for index in range(40)]}
        )
        await started.wait()
        await asyncio.sleep(0.1)
        status = await manager.get_job(job["job_id"])
        assert status["state"] == "running"
        assert status["progress"]["pending"] == 39
        assert status["errors"]["open"] == 0
        gate.set()
        await manager.wait_for_idle(5)
        assert (await manager.get_job(job["job_id"]))["state"] == "completed"
    finally:
        gate.set()
        await manager.close()


@pytest.mark.asyncio
async def test_exhausted_retry_needs_attention_and_resume_only_reopens_it(
    tmp_path, monkeypatch
):
    clock = FakeClock()
    calls = 0

    async def search(_task):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RetryableJobError("temporary failure")
        return SearchPageResult([], total_results=0, total_pages=1)

    async def detail(_task):
        raise AssertionError

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
        clock=clock,
    )
    await manager.start()
    try:
        job = await manager.create_search_job({"query": "retry"})
        async with asyncio.timeout(2):
            while calls < 1:
                await asyncio.sleep(0.005)
        clock.advance(1.1)
        attention = await wait_for_state(manager, job["job_id"], "needs_attention")
        assert calls == 2
        assert attention["errors"]["open"] == 1

        original_get_job = manager.get_job

        async def forbidden_post_commit_read(_job_id):
            raise AssertionError("resume ACK must not read after commit")

        monkeypatch.setattr(manager, "get_job", forbidden_post_commit_read)
        resumed = await manager.resume_job(
            job["job_id"], resume_token=attention["resume"]["token"]
        )
        assert resumed["state"] in {"queued", "running"}
        monkeypatch.setattr(manager, "get_job", original_get_job)
        await wait_for_state(manager, job["job_id"], "completed")
        assert calls == 3
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_resume_retry_cannot_authorize_a_new_attention_cycle(
    tmp_path, monkeypatch
):
    calls = 0

    async def search(_task):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RetryableJobError(f"failure cycle {calls}")
        return SearchPageResult([], total_results=0, total_pages=1)

    async def detail(_task):
        raise AssertionError

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3", retry_budget=0),
        search_executor=search,
        detail_executor=detail,
    )
    await manager.start()
    try:
        job = await manager.create_search_job({"query": "resume-ack"})
        first_attention = await wait_for_state(
            manager, job["job_id"], "needs_attention"
        )
        first_token = first_attention["resume"]["token"]
        original_finish = manager._finish_db_operation
        cancel_next_ack = True
        request_task = asyncio.current_task()

        async def cancel_after_durable_commit(operation):
            nonlocal cancel_next_ack
            result = await original_finish(operation)
            if cancel_next_ack and asyncio.current_task() is request_task:
                cancel_next_ack = False
                raise asyncio.CancelledError
            return result

        monkeypatch.setattr(
            manager,
            "_finish_db_operation",
            cancel_after_durable_commit,
        )
        with pytest.raises(asyncio.CancelledError):
            await manager.resume_job(job["job_id"], resume_token=first_token)

        second_attention = await wait_for_state(
            manager, job["job_id"], "needs_attention"
        )
        assert calls == 2
        assert second_attention["resume"]["revision"] == 1

        # The lost first ACK is replayed. It cannot silently authorize the new
        # needs-attention cycle that arose after another outbound navigation.
        replay = await manager.resume_job(job["job_id"], resume_token=first_token)
        assert replay["idempotent_replay"] is True
        assert replay["state"] == "queued"
        await asyncio.sleep(0.05)
        assert calls == 2

        resumed = await manager.resume_job(
            job["job_id"], resume_token=second_attention["resume"]["token"]
        )
        assert resumed["resume"]["revision"] == 2
        await wait_for_state(manager, job["job_id"], "completed")
        assert calls == 3
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_429_cooldown_stops_all_work_then_allows_one_probe(tmp_path):
    clock = FakeClock()
    calls: list[tuple[str, float]] = []

    async def search(task):
        calls.append((task.job_id, clock()))
        if len(calls) == 1:
            raise UpstreamRateLimited("rate limited", retry_after=2)
        return SearchPageResult([], total_results=0, total_pages=1)

    async def detail(_task):
        raise AssertionError

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
        clock=clock,
    )
    await manager.start()
    try:
        first, second = await asyncio.gather(
            manager.create_search_job({"query": "one"}),
            manager.create_search_job({"query": "two"}),
        )
        await wait_for_state(manager, first["job_id"], "waiting_cooldown")
        await asyncio.sleep(0.05)
        assert len(calls) == 1
        runtime = await manager.load_runtime_state()
        assert runtime["circuit"]["level"] == 1
        assert runtime["circuit"]["reason"] == "http_429"
        assert runtime["circuit"]["cooldown_remaining_seconds"] == 2.0

        clock.advance(1.9)
        await asyncio.sleep(0.05)
        assert len(calls) == 1
        clock.advance(0.2)
        await wait_for_state(manager, first["job_id"], "completed")
        await wait_for_state(manager, second["job_id"], "completed")
        assert len(calls) == 3
        assert (await manager.get_job(first["job_id"]))["state"] == "completed"
        assert (await manager.get_job(second["job_id"]))["state"] == "completed"
        assert (await manager.load_runtime_state())["circuit"]["state"] == "closed"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_controller_persisted_trip_is_not_counted_twice(tmp_path):
    clock = FakeClock()
    manager: JobManager

    async def search(_task):
        await manager.save_runtime_state(
            {
                "version": 1,
                "captured_at": datetime.fromtimestamp(
                    clock(), tz=timezone.utc
                ).isoformat(),
                "next_start_at": None,
                "circuit_reason": "http_429",
                "circuit_open_until": datetime.fromtimestamp(
                    clock() + 2, tz=timezone.utc
                ).isoformat(),
                "circuit_generation": 1,
                "block_streak": 1,
            }
        )
        raise UpstreamRateLimited("rate limited", retry_after=2)

    async def detail(_task):
        raise AssertionError

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
        clock=clock,
    )
    await manager.start()
    try:
        job = await manager.create_search_job({"query": "one"})
        await wait_for_state(manager, job["job_id"], "waiting_cooldown")
        snapshot = await manager.load_controller_snapshot()
        assert snapshot["circuit_generation"] == 1
        assert snapshot["block_streak"] == 1
        assert snapshot["circuit_reason"] == "http_429"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_controller_start_snapshot_preserves_scheduler_probe_reservation(
    tmp_path,
):
    clock = FakeClock()

    async def search(_task):
        raise AssertionError

    async def detail(_task):
        raise AssertionError

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3", worker_count=2),
        search_executor=search,
        detail_executor=detail,
        clock=clock,
    )
    await manager.start(start_workers=False)
    try:
        await manager.create_search_job({"query": "probe"})
        await manager.save_runtime_state(
            {
                "version": 1,
                "next_start_at": None,
                "circuit_reason": "http_429",
                "circuit_open_until": datetime.fromtimestamp(
                    clock() + 1, tz=timezone.utc
                ).isoformat(),
                "circuit_generation": 1,
                "block_streak": 1,
            }
        )
        clock.advance(1.1)
        claimed, _ = await manager._claim_next_work()
        assert claimed is not None and claimed.is_probe
        assert (await manager.load_runtime_state())["circuit"]["probe_in_flight"]

        # This is the callback shape emitted by the controller when the
        # half-open page has started: the circuit reason remains, but its old
        # deadline has elapsed and is serialized as null.
        await manager.save_runtime_state(
            {
                "version": 1,
                "next_start_at": datetime.fromtimestamp(
                    clock() + 3, tz=timezone.utc
                ).isoformat(),
                "circuit_reason": "http_429",
                "circuit_open_until": None,
                "circuit_generation": 1,
                "block_streak": 1,
            }
        )

        assert (await manager.load_runtime_state())["circuit"]["probe_in_flight"]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_retention_keeps_referenced_search_until_younger_detail_expires(
    tmp_path,
):
    database = tmp_path / "jobs.sqlite3"
    clock = FakeClock()

    async def search(_task):
        return SearchPageResult(
            [{"adid": "3650000000"}], total_results=1, total_pages=1
        )

    async def detail(task):
        return DetailItemResult("complete", {"id": task.listing_id})

    def make_manager():
        return JobManager(
            settings(database, retention_days=7),
            search_executor=search,
            detail_executor=detail,
            clock=clock,
        )

    manager = make_manager()
    await manager.start()
    search_job = await manager.create_search_job({"query": "retention"})
    await wait_for_state(manager, search_job["job_id"], "completed")
    clock.advance(6 * 86_400)
    detail_job = await manager.create_detail_job(
        source_search_job_id=search_job["job_id"]
    )
    await wait_for_state(manager, detail_job["job_id"], "completed")
    clock.advance(2 * 86_400)
    await manager.close()

    manager = make_manager()
    await manager.start(start_workers=False)
    try:
        # The search is older than seven days, but deleting it would invalidate
        # the still-retained detail snapshot's foreign key.
        assert (await manager.get_job(search_job["job_id"]))["state"] == "completed"
        assert (await manager.get_job(detail_job["job_id"]))["state"] == "completed"
    finally:
        await manager.close()

    clock.advance(6 * 86_400)
    manager = make_manager()
    await manager.start(start_workers=False)
    try:
        with pytest.raises(JobNotFoundError):
            await manager.get_job(detail_job["job_id"])
        with pytest.raises(JobNotFoundError):
            await manager.get_job(search_job["job_id"])
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_cancel_releases_executor_and_scheduler_remains_usable(tmp_path):
    gate = asyncio.Event()
    started = asyncio.Event()

    async def search(_task):
        return SearchPageResult([], total_results=0, total_pages=1)

    async def detail(task):
        if task.listing_id == "3600000000":
            started.set()
            await gate.wait()
        return DetailItemResult("complete", {"id": task.listing_id})

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
    )
    await manager.start()
    try:
        job = await manager.create_detail_job({"listing_ids": ["3600000000"]})
        await started.wait()
        cancelled = await manager.cancel_job(job["job_id"])
        assert cancelled["state"] == "cancelled"
        assert (await manager.cancel_job(job["job_id"]))["state"] == "cancelled"

        next_job = await manager.create_detail_job({"listing_ids": ["3600000001"]})
        await manager.wait_for_idle()
        assert (await manager.get_job(next_job["job_id"]))["state"] == "completed"
        cancelled_items = await manager.get_results(job["job_id"])
        assert cancelled_items["items"][0]["status"] == "cancelled"
    finally:
        gate.set()
        await manager.close()


@pytest.mark.asyncio
async def test_cancel_disconnect_after_commit_still_drains_executor(
    tmp_path, monkeypatch
):
    executor_started = asyncio.Event()
    executor_cancelled = asyncio.Event()
    allow_executor_cleanup = asyncio.Event()
    commit_started = asyncio.Event()
    allow_commit = asyncio.Event()

    async def search(_task):
        raise AssertionError

    async def detail(_task):
        executor_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            executor_cancelled.set()
            await allow_executor_cleanup.wait()
            raise

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
    )
    await manager.start()
    cancel_task = None
    try:
        job = await manager.create_detail_job({"listing_ids": ["3600000010"]})
        await executor_started.wait()

        database = manager._require_db()
        original_commit = database.commit
        delay_next_commit = True

        async def slow_commit():
            nonlocal delay_next_commit
            if delay_next_commit:
                delay_next_commit = False
                commit_started.set()
                await allow_commit.wait()
            return await original_commit()

        monkeypatch.setattr(database, "commit", slow_commit)
        cancel_task = asyncio.create_task(manager.cancel_job(job["job_id"]))
        await commit_started.wait()
        cancel_task.cancel()
        allow_commit.set()

        # Caller cancellation is remembered, but not delivered until the
        # already committed cancellation has actually stopped browser work.
        await executor_cancelled.wait()
        await asyncio.sleep(0)
        assert not cancel_task.done()
        allow_executor_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await cancel_task

        status = await manager.get_job(job["job_id"])
        assert status["state"] == "cancelled"
        replay = await manager.cancel_job(job["job_id"])
        assert replay["state"] == "cancelled"
        assert replay["idempotent_replay"] is True
    finally:
        allow_commit.set()
        allow_executor_cleanup.set()
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_cancelled_claim_cannot_launch_after_registration_race(tmp_path):
    calls = 0

    async def search(_task):
        raise AssertionError

    async def detail(_task):
        nonlocal calls
        calls += 1
        return DetailItemResult("complete", {"unexpected": True})

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
    )
    await manager.start(start_workers=False)
    try:
        job = await manager.create_detail_job({"listing_ids": ["3600000011"]})
        claimed, _ = await manager._claim_next_work()
        assert claimed is not None

        await manager.cancel_job(job["job_id"])
        await manager._execute_claim(claimed)

        assert calls == 0
        assert (await manager.get_job(job["job_id"]))["state"] == "cancelled"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_heartbeat_sqlite_failure_cancels_executor_and_requeues(
    tmp_path, monkeypatch
):
    heartbeat_tick = asyncio.Event()
    executor_started = asyncio.Event()
    executor_cancelled = asyncio.Event()

    async def controlled_sleep(seconds):
        if seconds == 1.0:
            await heartbeat_tick.wait()
        else:
            await asyncio.sleep(0)

    async def search(_task):
        raise AssertionError

    async def detail(_task):
        executor_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            executor_cancelled.set()
            raise

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3"),
        search_executor=search,
        detail_executor=detail,
        sleep=controlled_sleep,
    )
    await manager.start(start_workers=False)
    try:
        job = await manager.create_detail_job({"listing_ids": ["3690000000"]})
        claimed, _ = await manager._claim_next_work()
        assert claimed is not None

        db = manager._require_db()
        original_execute = db.execute

        async def fail_heartbeat(sql, *args, **kwargs):
            if "SET lease_expires_at" in sql:
                raise aiosqlite.OperationalError("heartbeat write failed")
            return await original_execute(sql, *args, **kwargs)

        monkeypatch.setattr(db, "execute", fail_heartbeat)
        execution = asyncio.create_task(manager._execute_claim(claimed))
        await executor_started.wait()
        heartbeat_tick.set()

        with pytest.raises(aiosqlite.OperationalError, match="heartbeat write failed"):
            await execution

        assert executor_cancelled.is_set()
        status = await manager.get_job(job["job_id"])
        assert status["state"] == "queued"
        assert status["progress"]["pending"] == 1
        assert status["progress"]["running"] == 0
    finally:
        heartbeat_tick.set()
        await manager.close()


@pytest.mark.asyncio
async def test_expired_lease_is_not_reclaimed_while_failed_heartbeat_cancels_executor(
    tmp_path, monkeypatch
):
    clock = FakeClock()
    heartbeat_tick = asyncio.Event()
    executor_started = asyncio.Event()
    cancellation_started = asyncio.Event()
    cancellation_cleanup = asyncio.Event()
    execution: asyncio.Task[None] | None = None

    async def controlled_sleep(seconds):
        if seconds == 1.0:
            await heartbeat_tick.wait()
        else:
            await asyncio.sleep(0)

    async def search(_task):
        raise AssertionError

    async def detail(_task):
        executor_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_started.set()
            await cancellation_cleanup.wait()
            raise

    manager = JobManager(
        settings(tmp_path / "jobs.sqlite3", worker_count=2),
        search_executor=search,
        detail_executor=detail,
        clock=clock,
        sleep=controlled_sleep,
    )
    await manager.start(start_workers=False)
    try:
        await manager.create_detail_job({"listing_ids": ["3690000001"]})
        claimed, _ = await manager._claim_next_work()
        assert claimed is not None

        db = manager._require_db()
        original_execute = db.execute

        async def fail_heartbeat(sql, *args, **kwargs):
            if "SET lease_expires_at" in sql:
                raise aiosqlite.OperationalError("heartbeat write failed")
            return await original_execute(sql, *args, **kwargs)

        monkeypatch.setattr(db, "execute", fail_heartbeat)
        execution = asyncio.create_task(manager._execute_claim(claimed))
        await executor_started.wait()
        heartbeat_tick.set()
        await cancellation_started.wait()

        clock.advance(manager.settings.lease_seconds + 1)
        competing_claim, _ = await manager._claim_next_work()
        assert competing_claim is None

        cancellation_cleanup.set()
        with pytest.raises(aiosqlite.OperationalError, match="heartbeat write failed"):
            await execution

        replacement_claim, _ = await manager._claim_next_work()
        assert replacement_claim is not None
        assert replacement_claim.work_item_id == claimed.work_item_id
        assert replacement_claim.lease_token != claimed.lease_token
    finally:
        heartbeat_tick.set()
        cancellation_cleanup.set()
        if execution is not None and not execution.done():
            await asyncio.gather(execution, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_restart_recovers_leased_work_and_exclusive_lock(tmp_path):
    database = tmp_path / "jobs.sqlite3"
    gate = asyncio.Event()
    started = asyncio.Event()

    async def blocked_detail(_task):
        started.set()
        await gate.wait()
        return DetailItemResult("complete", {})

    async def search(_task):
        return SearchPageResult([], total_results=0, total_pages=1)

    first = JobManager(
        settings(database), search_executor=search, detail_executor=blocked_detail
    )
    await first.start()
    job = await first.create_detail_job({"listing_ids": ["3700000000"]})
    await started.wait()

    competing = JobManager(
        settings(database), search_executor=search, detail_executor=blocked_detail
    )
    with pytest.raises(JobManagerAlreadyRunning):
        await competing.start()
    await first.close()

    async def successful_detail(task):
        return DetailItemResult("complete", {"id": task.listing_id})

    restarted = JobManager(
        settings(database), search_executor=search, detail_executor=successful_detail
    )
    await restarted.start()
    try:
        await restarted.wait_for_idle()
        assert (await restarted.get_job(job["job_id"]))["state"] == "completed"
    finally:
        gate.set()
        await restarted.close()


@pytest.mark.asyncio
async def test_storage_guard_and_status_are_controlled_and_non_sensitive(tmp_path):
    async def search(_task):
        return SearchPageResult([], total_results=0, total_pages=1)

    async def detail(_task):
        raise AssertionError

    database = tmp_path / "secret-name.sqlite3"
    manager = JobManager(
        settings(database, min_free_disk_bytes=1),
        search_executor=search,
        detail_executor=detail,
        disk_usage=lambda _path: SimpleNamespace(free=0),
    )
    await manager.start()
    try:
        with pytest.raises(JobStorageUnavailable):
            await manager.create_search_job({})
        status = await manager.status()
        assert "database_bytes" in status
        assert str(database) not in repr(status)
        assert status["work_items"]["pending"] == 0
    finally:
        await manager.close()


def test_job_settings_environment_is_validated(tmp_path):
    configured = JobSettings.from_env(
        {
            "KLEINANZEIGEN_JOB_DATABASE_PATH": str(tmp_path / "jobs.sqlite3"),
            "KLEINANZEIGEN_JOB_WORKER_COUNT": "2",
            "KLEINANZEIGEN_JOB_MIN_FREE_DISK_BYTES": "1MiB",
        }
    )
    assert configured.worker_count == 2
    assert configured.min_free_disk_bytes == 1024**2
    with pytest.raises(ValueError, match="WORKER_COUNT"):
        JobSettings.from_env({"KLEINANZEIGEN_JOB_WORKER_COUNT": "many"})
