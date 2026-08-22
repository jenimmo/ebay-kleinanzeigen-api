"""Persistent, fair job scheduling for Kleinanzeigen search and detail work.

The manager deliberately schedules one outbound unit at a time per job.  It does
not perform browser work itself; small, typed executor callbacks are injected by
the application layer.  This keeps persistence, fairness and recovery testable
without Playwright or network access.
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import functools
import hashlib
import json
import math
import os
import random
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias

import aiosqlite

from utils.input_validation import normalize_listing_id
from utils.outbound_control import (
    OutboundCircuitOpen,
    UpstreamBlocked,
    UpstreamRateLimited,
)
from utils.url_security import UnsafeOutboundUrlError, validate_kleinanzeigen_url_syntax


_SCHEMA_VERSION = 4
_ENV_PREFIX = "KLEINANZEIGEN_JOB_"
_SEARCH_CHUNK_SIZE = 200
_MAX_RESULT_PAGE_SIZE = 200
_MAX_IDEMPOTENCY_KEY_LENGTH = 128
_MAX_ERROR_LENGTH = 500
_CONTROLLER_CIRCUIT_REASONS = {
    "http_429",
    "http_403",
    "block_page",
    "half_open_probe_failed",
}
_ALL_JOB_STATES = (
    "queued",
    "running",
    "waiting_cooldown",
    "awaiting_continuation",
    "needs_attention",
    "completed",
    "cancelled",
)
_TERMINAL_JOB_STATES = frozenset({"completed", "cancelled"})

JobState: TypeAlias = Literal[
    "queued",
    "running",
    "waiting_cooldown",
    "awaiting_continuation",
    "needs_attention",
    "completed",
    "cancelled",
]


def _validate_int(name: str, value: int, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


def _validate_float(name: str, value: float, minimum: float, maximum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")


def _env_int(
    environ: Mapping[str, str], suffix: str, default: int, *, allow_bytes: bool = False
) -> int:
    name = f"{_ENV_PREFIX}{suffix}"
    raw = environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    multipliers = {"kib": 1024, "mib": 1024**2, "gib": 1024**3}
    if allow_bytes:
        for unit, multiplier in multipliers.items():
            if value.endswith(unit):
                try:
                    return int(value[: -len(unit)].strip()) * multiplier
                except ValueError as exc:
                    raise ValueError(
                        f"{name} must be a byte count, got {raw!r}"
                    ) from exc
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(environ: Mapping[str, str], suffix: str, default: float) -> float:
    name = f"{_ENV_PREFIX}{suffix}"
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {raw!r}")
    return value


@dataclass(frozen=True, slots=True)
class JobSettings:
    """Validated process-local settings for the durable job scheduler."""

    database_path: Path = Path("data/kleinanzeigen_jobs.sqlite3")
    worker_count: int = 1
    lease_seconds: float = 180.0
    heartbeat_seconds: float = 30.0
    retry_budget: int = 1
    poll_interval_seconds: float = 0.25
    min_dispatch_interval_seconds: float = 3.0
    dispatch_jitter_seconds: float = 1.0
    cooldown_initial_seconds: float = 120.0
    cooldown_max_seconds: float = 3_600.0
    page_setup_timeout_seconds: float = 30.0
    extraction_timeout_seconds: float = 30.0
    cleanup_timeout_seconds: float = 30.0
    retention_days: int = 7
    min_free_disk_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        path = Path(self.database_path)
        if not str(path).strip() or "\x00" in str(path):
            raise ValueError("database_path must be a non-empty filesystem path")
        object.__setattr__(self, "database_path", path)
        _validate_int("worker_count", self.worker_count, 1, 8)
        _validate_float("lease_seconds", self.lease_seconds, 30.0, 3_600.0)
        _validate_float("heartbeat_seconds", self.heartbeat_seconds, 1.0, 1_200.0)
        if self.heartbeat_seconds >= self.lease_seconds / 2:
            raise ValueError("heartbeat_seconds must be less than half lease_seconds")
        _validate_int("retry_budget", self.retry_budget, 0, 5)
        _validate_float("poll_interval_seconds", self.poll_interval_seconds, 0.01, 10.0)
        _validate_float(
            "min_dispatch_interval_seconds",
            self.min_dispatch_interval_seconds,
            0.0,
            300.0,
        )
        _validate_float(
            "dispatch_jitter_seconds", self.dispatch_jitter_seconds, 0.0, 60.0
        )
        _validate_float(
            "cooldown_initial_seconds", self.cooldown_initial_seconds, 1.0, 86_400.0
        )
        _validate_float(
            "cooldown_max_seconds", self.cooldown_max_seconds, 1.0, 86_400.0
        )
        if self.cooldown_max_seconds < self.cooldown_initial_seconds:
            raise ValueError(
                "cooldown_max_seconds must be at least cooldown_initial_seconds"
            )
        for name, value in (
            ("page_setup_timeout_seconds", self.page_setup_timeout_seconds),
            ("extraction_timeout_seconds", self.extraction_timeout_seconds),
            ("cleanup_timeout_seconds", self.cleanup_timeout_seconds),
        ):
            _validate_float(name, value, 1.0, 600.0)
        _validate_int("retention_days", self.retention_days, 1, 365)
        _validate_int("min_free_disk_bytes", self.min_free_disk_bytes, 0, 1024**4)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> JobSettings:
        """Load settings from environment and fail immediately on invalid input."""

        source = os.environ if environ is None else environ
        defaults = cls()
        raw_path = source.get(
            f"{_ENV_PREFIX}DATABASE_PATH", str(defaults.database_path)
        ).strip()
        if not raw_path:
            raise ValueError(f"{_ENV_PREFIX}DATABASE_PATH must not be empty")
        return cls(
            database_path=Path(raw_path),
            worker_count=_env_int(source, "WORKER_COUNT", defaults.worker_count),
            lease_seconds=_env_float(source, "LEASE_SECONDS", defaults.lease_seconds),
            heartbeat_seconds=_env_float(
                source, "HEARTBEAT_SECONDS", defaults.heartbeat_seconds
            ),
            retry_budget=_env_int(source, "RETRY_BUDGET", defaults.retry_budget),
            poll_interval_seconds=_env_float(
                source, "POLL_INTERVAL_SECONDS", defaults.poll_interval_seconds
            ),
            min_dispatch_interval_seconds=_env_float(
                source,
                "MIN_DISPATCH_INTERVAL_SECONDS",
                defaults.min_dispatch_interval_seconds,
            ),
            dispatch_jitter_seconds=_env_float(
                source,
                "DISPATCH_JITTER_SECONDS",
                defaults.dispatch_jitter_seconds,
            ),
            cooldown_initial_seconds=_env_float(
                source,
                "COOLDOWN_INITIAL_SECONDS",
                defaults.cooldown_initial_seconds,
            ),
            cooldown_max_seconds=_env_float(
                source, "COOLDOWN_MAX_SECONDS", defaults.cooldown_max_seconds
            ),
            page_setup_timeout_seconds=_env_float(
                source,
                "PAGE_SETUP_TIMEOUT_SECONDS",
                defaults.page_setup_timeout_seconds,
            ),
            extraction_timeout_seconds=_env_float(
                source,
                "EXTRACTION_TIMEOUT_SECONDS",
                defaults.extraction_timeout_seconds,
            ),
            cleanup_timeout_seconds=_env_float(
                source,
                "CLEANUP_TIMEOUT_SECONDS",
                defaults.cleanup_timeout_seconds,
            ),
            retention_days=_env_int(source, "RETENTION_DAYS", defaults.retention_days),
            min_free_disk_bytes=_env_int(
                source,
                "MIN_FREE_DISK_BYTES",
                defaults.min_free_disk_bytes,
                allow_bytes=True,
            ),
        )


@dataclass(frozen=True, slots=True)
class SearchTask:
    job_id: str
    work_item_id: int
    search: Mapping[str, Any]
    page_number: int
    attempt: int
    is_retry: bool
    page_setup_timeout_seconds: float
    extraction_timeout_seconds: float
    cleanup_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class DetailTask:
    job_id: str
    work_item_id: int
    listing_id: str
    attempt: int
    is_retry: bool
    page_setup_timeout_seconds: float
    extraction_timeout_seconds: float
    cleanup_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class SearchPageResult:
    listings: Sequence[Mapping[str, Any]]
    total_results: int | None = None
    total_pages: int | None = None
    page_size: int | None = None
    has_next_page: bool | None = None


@dataclass(frozen=True, slots=True)
class DetailItemResult:
    status: Literal["complete", "not_found"]
    data: Mapping[str, Any] | None = None


class SearchExecutor(Protocol):
    def __call__(self, task: SearchTask) -> Awaitable[SearchPageResult]: ...


class DetailExecutor(Protocol):
    def __call__(self, task: DetailTask) -> Awaitable[DetailItemResult]: ...


class JobManagerError(RuntimeError):
    error_code = "job_manager_error"
    http_status = 500


class JobNotFoundError(JobManagerError):
    error_code = "job_not_found"
    http_status = 404


class JobConflictError(JobManagerError):
    error_code = "job_conflict"
    http_status = 409


class JobValidationError(JobManagerError):
    error_code = "job_validation_error"
    http_status = 422


class InvalidCursorError(JobManagerError):
    error_code = "invalid_cursor"
    http_status = 400


class JobStorageUnavailable(JobManagerError):
    error_code = "job_storage_unavailable"
    http_status = 503


class JobManagerNotStarted(JobManagerError):
    error_code = "job_manager_not_started"
    http_status = 503


class JobManagerAlreadyRunning(JobManagerError):
    error_code = "job_manager_already_running"
    http_status = 503


class RetryableJobError(RuntimeError):
    """Executor failure that may consume the one shared job retry budget."""

    def __init__(self, message: str, *, error_code: str = "transient_error") -> None:
        super().__init__(message)
        self.error_code = error_code


class PermanentJobError(RuntimeError):
    """Executor failure that requires explicit operator/agent attention."""

    def __init__(self, message: str, *, error_code: str = "permanent_error") -> None:
        super().__init__(message)
        self.error_code = error_code


def _translate_storage_errors(method):
    """Expose SQLite failures as the public, non-sensitive 503 domain error."""

    @functools.wraps(method)
    async def wrapped(*args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except JobManagerError:
            raise
        except aiosqlite.Error as exc:
            raise JobStorageUnavailable("Job storage is unavailable") from exc

    return wrapped


@dataclass(frozen=True, slots=True)
class _ClaimedWork:
    work_item_id: int
    job_id: str
    job_kind: Literal["search", "details"]
    request: Mapping[str, Any]
    page_number: int | None
    listing_id: str | None
    attempt: int
    lease_token: str
    is_probe: bool
    circuit_generation_at_claim: int
    block_streak_at_claim: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('search', 'details')),
    state TEXT NOT NULL CHECK (state IN (
        'queued', 'running', 'waiting_cooldown', 'awaiting_continuation',
        'needs_attention', 'completed', 'cancelled'
    )),
    request_json TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    last_scheduled_at REAL NOT NULL DEFAULT 0,
    last_scheduled_seq INTEGER NOT NULL DEFAULT 0,
    total_reported INTEGER,
    total_pages INTEGER,
    next_search_page INTEGER NOT NULL DEFAULT 1,
    search_has_more INTEGER NOT NULL DEFAULT 0,
    approved_limit INTEGER NOT NULL DEFAULT 200,
    continuation_revision INTEGER NOT NULL DEFAULT 0,
    resume_revision INTEGER NOT NULL DEFAULT 0,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    released_count INTEGER NOT NULL DEFAULT 0,
    total_items INTEGER NOT NULL DEFAULT 0,
    source_search_job_id TEXT REFERENCES jobs(id),
    last_error_code TEXT,
    last_error_message TEXT
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    job_kind TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS job_action_receipts (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK (action IN ('continue', 'resume')),
    request_revision INTEGER NOT NULL,
    result_revision INTEGER NOT NULL,
    result_state TEXT NOT NULL,
    approved_limit INTEGER,
    created_at REAL NOT NULL,
    PRIMARY KEY(job_id, action, request_revision)
);

CREATE INDEX IF NOT EXISTS idx_jobs_source_search
ON jobs(source_search_job_id);

CREATE TABLE IF NOT EXISTS work_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('search_page', 'detail')),
    sequence INTEGER NOT NULL,
    page_number INTEGER,
    listing_id TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'leased', 'complete', 'not_found',
        'needs_attention', 'cancelled'
    )),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_expires_at REAL,
    last_error_code TEXT,
    last_error_message TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(job_id, kind, sequence),
    UNIQUE(job_id, page_number),
    UNIQUE(job_id, listing_id)
);

CREATE INDEX IF NOT EXISTS idx_work_runnable
ON work_items(status, next_attempt_at, job_id, sequence);

CREATE INDEX IF NOT EXISTS idx_work_lease
ON work_items(status, lease_expires_at);

CREATE TABLE IF NOT EXISTS detail_result_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id INTEGER NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_detail_result_events_work
ON detail_result_events(work_item_id, id);

CREATE TRIGGER IF NOT EXISTS detail_result_event_after_insert
AFTER INSERT ON work_items
WHEN NEW.kind = 'detail'
BEGIN
    INSERT INTO detail_result_events (work_item_id, created_at)
    VALUES (NEW.id, NEW.updated_at);
END;

CREATE TRIGGER IF NOT EXISTS detail_result_event_after_state_change
AFTER UPDATE OF status, last_error_code, last_error_message ON work_items
WHEN NEW.kind = 'detail' AND (
    NEW.status IS NOT OLD.status
    OR NEW.last_error_code IS NOT OLD.last_error_code
    OR NEW.last_error_message IS NOT OLD.last_error_message
)
BEGIN
    INSERT INTO detail_result_events (work_item_id, created_at)
    VALUES (NEW.id, NEW.updated_at);
END;

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    work_item_id INTEGER REFERENCES work_items(id) ON DELETE SET NULL,
    sequence INTEGER NOT NULL,
    listing_id TEXT NOT NULL,
    data_json TEXT,
    visible INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(job_id, listing_id),
    UNIQUE(job_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_results_page
ON results(job_id, visible, sequence);

CREATE TABLE IF NOT EXISTS runtime_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    circuit_reason TEXT,
    circuit_open_until REAL NOT NULL DEFAULT 0,
    circuit_level INTEGER NOT NULL DEFAULT 0,
    circuit_generation INTEGER NOT NULL DEFAULT 0,
    block_streak INTEGER NOT NULL DEFAULT 0,
    probe_in_flight INTEGER NOT NULL DEFAULT 0,
    next_start_at REAL NOT NULL DEFAULT 0,
    dispatch_sequence INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
"""


class JobManager:
    """Own a durable SQLite queue and a fixed, fair worker pool."""

    def __init__(
        self,
        settings: JobSettings,
        *,
        search_executor: SearchExecutor,
        detail_executor: DetailExecutor,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
        disk_usage: Callable[[str | os.PathLike[str]], Any] = shutil.disk_usage,
    ) -> None:
        self.settings = settings
        self._search_executor = search_executor
        self._detail_executor = detail_executor
        self._clock = clock
        self._sleep = sleep
        self._random_value = random_value
        self._disk_usage = disk_usage

        self._db: aiosqlite.Connection | None = None
        self._db_lock = asyncio.Lock()
        # Serializes durable cancellation with executor registration. Without
        # this gate, a leased claim could start after its job was cancelled but
        # before it appeared in _executions.
        self._execution_gate = asyncio.Lock()
        self._wake = asyncio.Event()
        self._workers: list[asyncio.Task[None]] = []
        self._executions: dict[int, asyncio.Task[Any]] = {}
        self._started = False
        self._closing = False
        self._lock_file: Any = None
        self._next_prune_at = 0.0

    async def start(self, *, start_workers: bool = True) -> None:
        """Open/migrate storage and optionally start the worker pool.

        Production startup can pass ``start_workers=False``, restore the
        OutboundController from :meth:`load_controller_snapshot`, and then call
        :meth:`start_workers`.  This prevents work from racing controller restore.
        """

        if self._started:
            return
        path = self.settings.database_path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._acquire_process_lock()
        try:
            self._db = await aiosqlite.connect(path, timeout=30.0)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA foreign_keys = ON")
            await self._db.execute("PRAGMA journal_mode = WAL")
            # At this deliberately low write rate, FULL durability is worth the
            # small fsync cost: a returned 202 must survive a host/power crash,
            # not merely a clean process restart.
            await self._db.execute("PRAGMA synchronous = FULL")
            await self._db.execute("PRAGMA busy_timeout = 30000")
            version_cursor = await self._db.execute("PRAGMA user_version")
            version_row = await version_cursor.fetchone()
            version = int(version_row[0])
            if version > _SCHEMA_VERSION:
                raise JobStorageUnavailable(
                    f"Job database schema {version} is newer than supported "
                    f"schema {_SCHEMA_VERSION}"
                )
            await self._db.executescript(_SCHEMA)
            columns_cursor = await self._db.execute("PRAGMA table_info(jobs)")
            job_columns = {str(row[1]) for row in await columns_cursor.fetchall()}
            if "continuation_revision" not in job_columns:
                await self._db.execute(
                    "ALTER TABLE jobs ADD COLUMN continuation_revision "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "resume_revision" not in job_columns:
                await self._db.execute(
                    "ALTER TABLE jobs ADD COLUMN resume_revision "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            await self._db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            now = self._clock()
            await self._db.execute(
                """
                INSERT OR IGNORE INTO runtime_state (
                    singleton, updated_at
                ) VALUES (1, ?)
                """,
                (now,),
            )
            # Version 2 introduces a monotonic change cursor for detail rows.
            # Backfill one snapshot event for work created by schema version 1;
            # subsequent externally visible state changes are trigger-backed.
            await self._db.execute(
                """
                INSERT INTO detail_result_events (work_item_id, created_at)
                SELECT w.id, w.updated_at
                FROM work_items AS w
                WHERE w.kind = 'detail'
                  AND NOT EXISTS (
                      SELECT 1 FROM detail_result_events AS event
                      WHERE event.work_item_id = w.id
                  )
                """
            )
            await self._recover_interrupted_work(now)
            await self._prune_expired_jobs(now)
            self._next_prune_at = now + 3_600.0
            await self._db.commit()
        except BaseException as exc:
            if self._db is not None:
                await self._db.close()
                self._db = None
            self._release_process_lock()
            if isinstance(exc, aiosqlite.Error):
                raise JobStorageUnavailable("Could not initialize job storage") from exc
            raise

        self._started = True
        self._closing = False
        if start_workers:
            await self.start_workers()

    async def start_workers(self) -> None:
        """Start workers after external outbound runtime restoration is complete."""

        self._require_started()
        if self._workers:
            return
        self._workers = [
            asyncio.create_task(
                self._worker_loop(index), name=f"kleinanzeigen-job-worker-{index}"
            )
            for index in range(self.settings.worker_count)
        ]
        self._wake.set()

    async def close(self) -> None:
        """Stop workers safely while leaving claimed work resumable on restart."""

        if not self._started:
            self._release_process_lock()
            return
        self._closing = True
        for execution in tuple(self._executions.values()):
            execution.cancel()
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._executions.clear()

        db = self._require_db()
        now = self._clock()
        try:
            async with self._db_lock:
                try:
                    await db.execute(
                        """
                        UPDATE work_items
                        SET status = 'pending', attempts = MAX(0, attempts - 1),
                            lease_token = NULL,
                            lease_expires_at = NULL, updated_at = ?
                        WHERE status = 'leased'
                        """,
                        (now,),
                    )
                    await db.execute(
                        """
                        UPDATE jobs
                        SET state = 'queued', updated_at = ?
                        WHERE state = 'running'
                        """,
                        (now,),
                    )
                    await db.execute(
                        "UPDATE runtime_state SET probe_in_flight = 0, updated_at = ? "
                        "WHERE singleton = 1",
                        (now,),
                    )
                    await self._finish_db_operation(db.commit())
                except BaseException:
                    await self._finish_db_operation(db.rollback())
                    raise
        finally:
            try:
                await db.close()
            finally:
                self._db = None
                self._started = False
                self._closing = False
                self._release_process_lock()

    @_translate_storage_errors
    async def create_search_job(
        self,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Persist a search job and return immediately without browser work."""

        self._require_started()
        search = self._normalize_search_payload(payload)
        return await self._create_job(
            kind="search",
            request={"search": search},
            idempotency_key=idempotency_key,
        )

    @_translate_storage_errors
    async def create_detail_job(
        self,
        payload: Mapping[str, Any] | None = None,
        *,
        source_search_job_id: str | None = None,
        listing_ids: Sequence[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Persist a frozen detail-ID snapshot without creating coroutines."""

        self._require_started()
        raw = dict(payload or {})
        source = source_search_job_id or raw.get("source_search_job_id")
        ids = listing_ids
        if ids is None:
            ids = raw.get("listing_ids", raw.get("ids"))
        if bool(source) == bool(ids):
            raise JobValidationError(
                "Exactly one of source_search_job_id or listing_ids is required"
            )

        normalized_source = str(source).strip() if source else None
        explicit_ids: list[str] | None = None
        if ids is not None:
            if isinstance(ids, (str, bytes)) or not isinstance(ids, Sequence):
                raise JobValidationError("listing_ids must be an array")
            explicit_ids = self._normalize_listing_ids(ids)
            if not explicit_ids:
                raise JobValidationError("listing_ids must not be empty")

        request: dict[str, Any]
        if normalized_source:
            request = {"source_search_job_id": normalized_source}
        else:
            request = {"listing_ids": explicit_ids}
        return await self._create_job(
            kind="details", request=request, idempotency_key=idempotency_key
        )

    @_translate_storage_errors
    async def get_job(self, job_id: str) -> dict[str, Any]:
        """Return status, progress, ETA, continuation and cooldown metadata."""

        self._require_started()
        normalized = self._normalize_job_id(job_id)
        db = self._require_db()
        async with self._db_lock:
            row = await self._fetch_job_locked(db, normalized)
            counts_cursor = await db.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM work_items WHERE job_id = ? GROUP BY status
                """,
                (normalized,),
            )
            counts = {
                item["status"]: int(item["count"])
                for item in await counts_cursor.fetchall()
            }
            runtime = await self._fetch_runtime_locked(db)
            queue_cursor = await db.execute(
                """
                SELECT COUNT(*)
                FROM jobs
                WHERE state IN ('queued', 'running')
                  AND last_scheduled_seq < ?
                """,
                (row["last_scheduled_seq"],),
            )
            queue_row = await queue_cursor.fetchone()
        return self._job_dto(row, counts, runtime, int(queue_row[0]))

    @_translate_storage_errors
    async def get_results(
        self,
        job_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Read a stable SQLite result/change page without invoking an executor.

        Search results are immutable and therefore use their result sequence.
        Detail rows may move from pending to a final state, so their cursor is a
        monotonic change-event ID. A listing can intentionally reappear after
        its state changes; clients apply the newest item by listing_id.
        """

        self._require_started()
        normalized = self._normalize_job_id(job_id)
        _validate_int("limit", limit, 1, _MAX_RESULT_PAGE_SIZE)
        db = self._require_db()
        next_cursor = cursor
        async with self._db_lock:
            job = await self._fetch_job_locked(db, normalized)
            if job["kind"] == "search":
                after_sequence = self._decode_cursor(
                    cursor,
                    normalized,
                    expected_version=1,
                )
                result_cursor = await db.execute(
                    """
                    SELECT sequence, listing_id, data_json
                    FROM results
                    WHERE job_id = ? AND visible = 1 AND sequence > ?
                    ORDER BY sequence ASC LIMIT ?
                    """,
                    (normalized, after_sequence, limit + 1),
                )
                rows = await result_cursor.fetchall()
                page_rows = rows[:limit]
                items = [
                    {
                        "sequence": int(item["sequence"]),
                        "listing_id": item["listing_id"],
                        "status": "complete",
                        "data": json.loads(item["data_json"]),
                    }
                    for item in page_rows
                ]
                if items and (
                    len(rows) > limit or job["state"] not in _TERMINAL_JOB_STATES
                ):
                    next_cursor = self._encode_cursor(
                        normalized,
                        items[-1]["sequence"],
                        version=1,
                    )
                else:
                    next_cursor = None
            else:
                after_event = self._decode_cursor(
                    cursor,
                    normalized,
                    expected_version=2,
                )
                result_cursor = await db.execute(
                    """
                    SELECT event.id AS event_id, w.sequence, w.listing_id, w.status,
                           w.last_error_code, w.last_error_message, r.data_json
                    FROM detail_result_events AS event
                    JOIN work_items AS w ON w.id = event.work_item_id
                    LEFT JOIN results AS r ON r.work_item_id = w.id
                    WHERE w.job_id = ? AND event.id > ?
                      AND NOT EXISTS (
                          SELECT 1
                          FROM detail_result_events AS newer
                          WHERE newer.work_item_id = event.work_item_id
                            AND newer.id > event.id
                      )
                    ORDER BY event.id ASC LIMIT ?
                    """,
                    (normalized, after_event, limit + 1),
                )
                rows = await result_cursor.fetchall()
                page_rows = rows[:limit]
                items = []
                for item in page_rows:
                    dto: dict[str, Any] = {
                        "sequence": int(item["sequence"]),
                        "listing_id": item["listing_id"],
                        "status": (
                            "pending" if item["status"] == "leased" else item["status"]
                        ),
                        "data": (
                            json.loads(item["data_json"])
                            if item["data_json"] is not None
                            else None
                        ),
                    }
                    if item["last_error_code"]:
                        dto["error"] = {
                            "code": item["last_error_code"],
                            "message": item["last_error_message"],
                        }
                    items.append(dto)
                if page_rows:
                    next_cursor = self._encode_cursor(
                        normalized,
                        int(page_rows[-1]["event_id"]),
                        version=2,
                    )

        return {
            "job_id": normalized,
            "type": job["kind"],
            "items": items,
            "next_cursor": next_cursor,
            "has_more": len(rows) > limit,
        }

    @_translate_storage_errors
    async def continue_job(
        self,
        job_id: str,
        *,
        continuation_token: str,
        after_cursor: str | None = None,
    ) -> dict[str, Any]:
        """Idempotently release the tranche authorized by token and cursor."""

        self._require_started()
        normalized = self._normalize_job_id(job_id)
        requested_revision = self._decode_continuation_token(
            continuation_token,
            normalized,
        )
        after_sequence = (
            self._decode_cursor(after_cursor, normalized, expected_version=1)
            if after_cursor is not None
            else None
        )
        now = self._clock()
        replay = False
        async with self._transaction() as db:
            job = await self._fetch_job_locked(db, normalized)
            if job["kind"] != "search":
                raise JobConflictError("Only search jobs can be continued")

            current_revision = int(job["continuation_revision"])
            state: JobState = job["state"]
            approved_limit = int(job["approved_limit"])
            receipt_cursor = await db.execute(
                """
                SELECT result_revision, result_state, approved_limit
                FROM job_action_receipts
                WHERE job_id = ? AND action = 'continue'
                  AND request_revision = ?
                """,
                (normalized, requested_revision),
            )
            receipt = await receipt_cursor.fetchone()
            if receipt is not None:
                receipt_approved_limit = int(receipt["approved_limit"])
                self._validate_continuation_boundary(
                    after_sequence,
                    receipt_approved_limit - _SEARCH_CHUNK_SIZE,
                )
                # Receipts preserve the exact durable ACK across later
                # continuations and process restarts.
                replay = True
                current_revision = int(receipt["result_revision"])
                state = receipt["result_state"]
                approved_limit = receipt_approved_limit
            elif requested_revision != current_revision:
                raise JobConflictError("Continuation token is stale")
            else:
                if state != "awaiting_continuation":
                    raise JobConflictError("Job is not awaiting continuation")
                self._validate_continuation_boundary(
                    after_sequence,
                    approved_limit,
                )

                approved_limit += _SEARCH_CHUNK_SIZE
                await db.execute(
                    "UPDATE results SET visible = 1, updated_at = ? "
                    "WHERE job_id = ? AND sequence <= ?",
                    (now, normalized, approved_limit),
                )
                count_cursor = await db.execute(
                    "SELECT COUNT(*) FROM results WHERE job_id = ? AND visible = 1",
                    (normalized,),
                )
                released = int((await count_cursor.fetchone())[0])
                hidden = int(job["discovered_count"]) - released
                has_more = bool(job["search_has_more"])
                runtime = await self._fetch_runtime_locked(db)
                if hidden > 0:
                    state = "awaiting_continuation"
                elif has_more:
                    state = (
                        "waiting_cooldown"
                        if runtime["circuit_reason"] is not None
                        else "queued"
                    )
                    await self._insert_search_page_locked(
                        db, normalized, int(job["next_search_page"]), now
                    )
                else:
                    state = "completed"

                current_revision += 1
                await db.execute(
                    """
                    UPDATE jobs
                    SET approved_limit = ?, continuation_revision = ?,
                        released_count = ?, state = ?,
                        completed_at = CASE
                            WHEN ? = 'completed' THEN ? ELSE NULL
                        END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        approved_limit,
                        current_revision,
                        released,
                        state,
                        state,
                        now,
                        now,
                        normalized,
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO job_action_receipts (
                        job_id, action, request_revision, result_revision,
                        result_state, approved_limit, created_at
                    ) VALUES (?, 'continue', ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized,
                        requested_revision,
                        current_revision,
                        state,
                        approved_limit,
                        now,
                    ),
                )

        if not replay:
            self._wake.set()
        return self._continuation_ack(
            job_id=normalized,
            state=state,
            approved_limit=approved_limit,
            revision=current_revision,
            replay=replay,
        )

    @_translate_storage_errors
    async def resume_job(
        self,
        job_id: str,
        *,
        resume_token: str,
    ) -> dict[str, Any]:
        """Idempotently requeue the attention cycle authorized by a token."""

        self._require_started()
        normalized = self._normalize_job_id(job_id)
        requested_revision = self._decode_resume_token(resume_token, normalized)
        now = self._clock()
        replay = False
        async with self._transaction() as db:
            job = await self._fetch_job_locked(db, normalized)
            job_kind = job["kind"]
            current_revision = int(job["resume_revision"])
            resumed_state: JobState = job["state"]
            receipt_cursor = await db.execute(
                """
                SELECT result_revision, result_state
                FROM job_action_receipts
                WHERE job_id = ? AND action = 'resume'
                  AND request_revision = ?
                """,
                (normalized, requested_revision),
            )
            receipt = await receipt_cursor.fetchone()
            if receipt is not None:
                # The exact old action remains replayable after later failures,
                # resumes, and restarts, without authorizing fresh work.
                replay = True
                current_revision = int(receipt["result_revision"])
                resumed_state = receipt["result_state"]
            elif requested_revision != current_revision:
                raise JobConflictError("Resume token is stale")
            else:
                if resumed_state != "needs_attention":
                    raise JobConflictError("Job does not need attention")
                cursor = await db.execute(
                    """
                    UPDATE work_items
                    SET status = 'pending', attempts = 0, next_attempt_at = 0,
                        lease_token = NULL, lease_expires_at = NULL,
                        last_error_code = NULL, last_error_message = NULL,
                        updated_at = ?
                    WHERE job_id = ? AND status = 'needs_attention'
                    """,
                    (now, normalized),
                )
                if cursor.rowcount == 0:
                    raise JobConflictError("Job has no resumable work")
                runtime = await self._fetch_runtime_locked(db)
                resumed_state = (
                    "waiting_cooldown"
                    if runtime["circuit_reason"] is not None
                    else "queued"
                )
                current_revision += 1
                await db.execute(
                    """
                    UPDATE jobs
                    SET state = ?, resume_revision = ?, completed_at = NULL,
                        last_error_code = NULL, last_error_message = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (resumed_state, current_revision, now, normalized),
                )
                await db.execute(
                    """
                    INSERT INTO job_action_receipts (
                        job_id, action, request_revision, result_revision,
                        result_state, approved_limit, created_at
                    ) VALUES (?, 'resume', ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized,
                        requested_revision,
                        current_revision,
                        resumed_state,
                        None,
                        now,
                    ),
                )

        if not replay:
            self._wake.set()
        return self._resume_ack(
            job_id=normalized,
            kind=job_kind,
            state=resumed_state,
            revision=current_revision,
            replay=replay,
        )

    @_translate_storage_errors
    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        """Durably cancel and drain work despite caller/HTTP cancellation."""

        self._require_started()
        normalized = self._normalize_job_id(job_id)
        cancellation: asyncio.CancelledError | None = None
        operation = asyncio.create_task(self._cancel_job_durably(normalized))
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError as exc:
                # A disconnected HTTP request must not strand browser work
                # after the cancellation transaction committed. The durable
                # operation owns its own task and is always drained first.
                cancellation = cancellation or exc
        result = operation.result()
        if cancellation is not None:
            raise cancellation
        return result

    async def _cancel_job_durably(self, job_id: str) -> dict[str, Any]:
        """Commit cancellation before stopping every registered executor."""

        now = self._clock()
        executions: list[asyncio.Task[Any]] = []
        cancellation: asyncio.CancelledError | None = None
        replay = False
        job_kind: Literal["search", "details"]
        async with self._execution_gate:
            async with self._transaction() as db:
                job = await self._fetch_job_locked(db, job_id)
                job_kind = job["kind"]
                if job["state"] == "completed":
                    raise JobConflictError("Completed jobs cannot be cancelled")
                replay = job["state"] == "cancelled"
                work_cursor = await db.execute(
                    "SELECT id FROM work_items WHERE job_id = ?",
                    (job_id,),
                )
                work_ids = [int(row[0]) for row in await work_cursor.fetchall()]
                if not replay:
                    await db.execute(
                        """
                        UPDATE work_items
                        SET status = 'cancelled', lease_token = NULL,
                            lease_expires_at = NULL, updated_at = ?
                        WHERE job_id = ?
                          AND status IN ('pending', 'leased', 'needs_attention')
                        """,
                        (now, job_id),
                    )
                    await db.execute(
                        """
                        UPDATE jobs
                        SET state = 'cancelled', completed_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, now, job_id),
                    )

            # No executor can register while the gate is held. After COMMIT,
            # stale claim registration will revalidate the cancelled row and
            # refuse to launch, so this snapshot is complete.
            executions = [
                execution
                for work_id in work_ids
                if (execution := self._executions.get(work_id)) is not None
            ]

        for execution in executions:
            try:
                await self._cancel_execution_and_wait(execution)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
        self._wake.set()

        if cancellation is not None:
            raise cancellation
        return self._creation_ack(
            job_id=job_id,
            kind=job_kind,
            state="cancelled",
            replay=replay,
        )

    @_translate_storage_errors
    async def load_runtime_state(self) -> dict[str, Any]:
        """Load restart-safe outbound timing and circuit state."""

        self._require_started()
        db = self._require_db()
        async with self._db_lock:
            runtime = await self._fetch_runtime_locked(db)
        return self._runtime_dto(runtime)

    @_translate_storage_errors
    async def load_controller_snapshot(self) -> dict[str, Any]:
        """Return the exact restart snapshot shape used by OutboundController."""

        self._require_started()
        db = self._require_db()
        async with self._db_lock:
            runtime = await self._fetch_runtime_locked(db)
        return self._controller_snapshot(runtime)

    @_translate_storage_errors
    async def save_runtime_state(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Merge a controller snapshot into persistent outbound runtime state.

        ``next_start_delay_seconds`` and ``cooldown_remaining_seconds`` are
        converted to wall-clock deadlines, making them meaningful after a process
        restart.  Absolute epoch fields are accepted for restore/test tooling.
        """

        self._require_started()
        if not isinstance(snapshot, Mapping):
            raise JobValidationError("runtime snapshot must be an object")
        now = self._clock()
        circuit = snapshot.get("circuit")
        circuit_mapping = circuit if isinstance(circuit, Mapping) else {}

        next_start_at = self._deadline_from_snapshot(
            snapshot,
            absolute_key="next_start_at",
            remaining_key="next_start_delay_seconds",
            now=now,
        )
        circuit_open_until = self._deadline_from_snapshot(
            {**snapshot, **circuit_mapping},
            absolute_key="circuit_open_until",
            remaining_key="cooldown_remaining_seconds",
            now=now,
        )
        circuit_state = circuit_mapping.get("state", snapshot.get("circuit_state"))
        circuit_reason_value = circuit_mapping.get(
            "reason", snapshot.get("circuit_reason")
        )
        circuit_reason = (
            self._safe_error_text(circuit_reason_value)
            if circuit_reason_value is not None
            else None
        )
        if (
            circuit_reason is not None
            and circuit_reason not in _CONTROLLER_CIRCUIT_REASONS
        ):
            raise JobValidationError("runtime snapshot has an invalid circuit_reason")

        async with self._transaction() as db:
            runtime = await self._fetch_runtime_locked(db)
            updates: list[str] = ["updated_at = ?"]
            values: list[Any] = [now]
            if next_start_at is not None:
                updates.append("next_start_at = ?")
                values.append(max(float(runtime["next_start_at"]), next_start_at))

            explicit_controller_reason = "circuit_reason" in snapshot
            if circuit_state == "closed" or (
                explicit_controller_reason and snapshot.get("circuit_reason") is None
            ):
                updates.extend(
                    (
                        "circuit_reason = NULL",
                        "circuit_open_until = 0",
                        "circuit_level = 0",
                        "block_streak = 0",
                        "probe_in_flight = 0",
                    )
                )
                await db.execute(
                    """
                    UPDATE jobs SET state = 'queued', updated_at = ?
                    WHERE state = 'waiting_cooldown'
                    """,
                    (now,),
                )

            elif circuit_reason is not None or circuit_open_until is not None:
                updates.append("circuit_reason = ?")
                values.append(circuit_reason or runtime["circuit_reason"] or "blocked")
                if circuit_open_until is not None:
                    updates.append("circuit_open_until = ?")
                    values.append(
                        max(float(runtime["circuit_open_until"]), circuit_open_until)
                    )
                # A controller snapshot emitted when a half-open navigation
                # actually starts has an expired/empty open-until deadline. Do
                # not clear the scheduler's probe reservation in that case: a
                # second worker could otherwise claim a competing probe and
                # cancel the valid one. A future deadline represents a fresh
                # trip and safely resets any stale reservation.
                if circuit_open_until is not None and circuit_open_until > now:
                    updates.append("probe_in_flight = 0")
                await db.execute(
                    """
                    UPDATE jobs SET state = 'waiting_cooldown', updated_at = ?
                    WHERE state IN ('queued', 'running')
                    """,
                    (now,),
                )

            if "circuit_generation" in snapshot:
                generation = self._validated_runtime_integer(
                    snapshot["circuit_generation"], "circuit_generation"
                )
                updates.append("circuit_generation = ?")
                values.append(generation)
            if "block_streak" in snapshot:
                block_streak = self._validated_runtime_integer(
                    snapshot["block_streak"], "block_streak"
                )
                updates.extend(("block_streak = ?", "circuit_level = ?"))
                values.extend((block_streak, block_streak))

            values.append(1)
            await db.execute(
                f"UPDATE runtime_state SET {', '.join(updates)} WHERE singleton = ?",
                values,
            )
        self._wake.set()
        return await self.load_runtime_state()

    @_translate_storage_errors
    async def status(self) -> dict[str, Any]:
        """Return non-sensitive persistent queue metrics for health endpoints."""

        self._require_started()
        db = self._require_db()
        now = self._clock()
        async with self._db_lock:
            job_cursor = await db.execute(
                "SELECT state, COUNT(*) AS count FROM jobs GROUP BY state"
            )
            raw_job_counts = {
                str(row["state"]): int(row["count"])
                for row in await job_cursor.fetchall()
            }
            work_cursor = await db.execute(
                "SELECT status, COUNT(*) AS count FROM work_items GROUP BY status"
            )
            work_counts = {
                str(row["status"]): int(row["count"])
                for row in await work_cursor.fetchall()
            }
            oldest_cursor = await db.execute(
                """
                SELECT MIN(created_at) FROM jobs
                WHERE state IN ('queued', 'running', 'waiting_cooldown')
                """
            )
            oldest = (await oldest_cursor.fetchone())[0]
            runtime = await self._fetch_runtime_locked(db)
        return {
            "workers": {
                "configured": self.settings.worker_count,
                "running": sum(not task.done() for task in self._workers),
                "executing": len(self._executions),
            },
            "jobs": {state: raw_job_counts.get(state, 0) for state in _ALL_JOB_STATES},
            "work_items": {
                "pending": work_counts.get("pending", 0),
                "leased": work_counts.get("leased", 0),
                "complete": work_counts.get("complete", 0),
                "not_found": work_counts.get("not_found", 0),
                "needs_attention": work_counts.get("needs_attention", 0),
                "cancelled": work_counts.get("cancelled", 0),
            },
            "oldest_active_job_age_seconds": (
                round(max(0.0, now - float(oldest)), 3) if oldest is not None else None
            ),
            "database_bytes": self._database_size_bytes(),
            "runtime": self._runtime_dto(runtime),
        }

    async def status_snapshot(self) -> dict[str, Any]:
        """Compatibility alias for callers that prefer an explicit snapshot name."""

        return await self.status()

    async def wait_for_idle(self, timeout: float = 10.0) -> None:
        """Testing/operations helper: wait until no immediately runnable work remains."""

        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            db = self._require_db()
            async with self._db_lock:
                cursor = await db.execute(
                    """
                    SELECT COUNT(*) FROM work_items AS w
                    JOIN jobs AS j ON j.id = w.job_id
                    WHERE w.status IN ('pending', 'leased')
                      AND j.state IN ('queued', 'running')
                    """
                )
                count = int((await cursor.fetchone())[0])
            if count == 0:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    "Timed out waiting for the job manager to become idle"
                )
            await asyncio.sleep(min(0.01, self.settings.poll_interval_seconds))

    async def _create_job(
        self,
        *,
        kind: Literal["search", "details"],
        request: Mapping[str, Any],
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        normalized_key = self._normalize_idempotency_key(idempotency_key)
        request_json = self._json_dumps(request)
        request_hash = hashlib.sha256(
            f"{kind}\0{request_json}".encode("utf-8")
        ).hexdigest()
        now = self._clock()
        job_id = str(uuid.uuid4())
        replay = False
        acknowledged_state: JobState = "queued"

        if normalized_key is not None:
            db = self._require_db()
            try:
                async with self._db_lock:
                    key_cursor = await db.execute(
                        """
                        SELECT key_record.job_id, key_record.job_kind,
                               key_record.request_hash, job.state
                        FROM idempotency_keys AS key_record
                        JOIN jobs AS job ON job.id = key_record.job_id
                        WHERE key_record.key = ?
                        """,
                        (normalized_key,),
                    )
                    existing = await key_cursor.fetchone()
            except aiosqlite.Error as exc:
                raise JobStorageUnavailable("Could not read job storage") from exc
            if existing is not None:
                if (
                    existing["job_kind"] != kind
                    or existing["request_hash"] != request_hash
                ):
                    raise JobConflictError(
                        "Idempotency-Key was already used for another request"
                    )
                return self._creation_ack(
                    job_id=str(existing["job_id"]),
                    kind=kind,
                    state=existing["state"],
                    replay=True,
                )

        self._assert_storage_capacity()

        try:
            async with self._transaction() as db:
                if normalized_key is not None:
                    key_cursor = await db.execute(
                        """
                            SELECT key_record.job_id, key_record.job_kind,
                                   key_record.request_hash, job.state
                            FROM idempotency_keys AS key_record
                            JOIN jobs AS job ON job.id = key_record.job_id
                            WHERE key_record.key = ?
                        """,
                        (normalized_key,),
                    )
                    existing = await key_cursor.fetchone()
                    if existing is not None:
                        if (
                            existing["job_kind"] != kind
                            or existing["request_hash"] != request_hash
                        ):
                            raise JobConflictError(
                                "Idempotency-Key was already used for another request"
                            )
                        job_id = existing["job_id"]
                        acknowledged_state = existing["state"]
                        replay = True

                if not replay:
                    runtime = await self._fetch_runtime_locked(db)
                    initial_state = (
                        "waiting_cooldown"
                        if runtime["circuit_reason"] is not None
                        else "queued"
                    )
                    acknowledged_state = initial_state
                    source_job_id = request.get("source_search_job_id")
                    detail_ids: list[str] = []
                    if kind == "details":
                        if source_job_id:
                            source = await self._fetch_job_locked(
                                db, str(source_job_id)
                            )
                            if source["kind"] != "search":
                                raise JobValidationError(
                                    "source_search_job_id must reference a search job"
                                )
                            ids_cursor = await db.execute(
                                """
                                SELECT listing_id FROM results
                                WHERE job_id = ? AND visible = 1
                                ORDER BY sequence ASC
                                """,
                                (source_job_id,),
                            )
                            detail_ids = [
                                str(row["listing_id"])
                                for row in await ids_cursor.fetchall()
                            ]
                            if not detail_ids:
                                raise JobValidationError(
                                    "Source search job has no released results"
                                )
                        else:
                            detail_ids = list(request["listing_ids"])

                    await db.execute(
                        """
                        INSERT INTO jobs (
                            id, kind, state, request_json, request_hash,
                            created_at, updated_at, approved_limit, total_items,
                            source_search_job_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            kind,
                            initial_state,
                            request_json,
                            request_hash,
                            now,
                            now,
                            _SEARCH_CHUNK_SIZE,
                            len(detail_ids),
                            source_job_id,
                        ),
                    )
                    if kind == "search":
                        await self._insert_search_page_locked(db, job_id, 1, now)
                    else:
                        await db.executemany(
                            """
                            INSERT INTO work_items (
                                job_id, kind, sequence, listing_id, status,
                                created_at, updated_at
                            ) VALUES (?, 'detail', ?, ?, 'pending', ?, ?)
                            """,
                            [
                                (job_id, index, listing_id, now, now)
                                for index, listing_id in enumerate(detail_ids, start=1)
                            ],
                        )
                    if normalized_key is not None:
                        await db.execute(
                            """
                            INSERT INTO idempotency_keys (
                                key, job_kind, request_hash, job_id, created_at
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                normalized_key,
                                kind,
                                request_hash,
                                job_id,
                                now,
                            ),
                        )
        except JobManagerError:
            raise
        except aiosqlite.Error as exc:
            raise JobStorageUnavailable("Could not persist the job") from exc

        if not replay:
            self._wake.set()
        # Everything needed for the acceptance response was captured inside the
        # committing transaction. Keeping the post-commit path await-free avoids
        # reporting a failure after the durable job has already been accepted.
        return self._creation_ack(
            job_id=str(job_id),
            kind=kind,
            state=acknowledged_state,
            replay=replay,
        )

    @staticmethod
    def _creation_ack(
        *,
        job_id: str,
        kind: Literal["search", "details"],
        state: JobState,
        replay: bool,
    ) -> dict[str, Any]:
        """Build the non-fallible response returned after durable acceptance."""

        return {
            "job_id": job_id,
            "type": kind,
            "state": state,
            "idempotent_replay": replay,
        }

    def _continuation_ack(
        self,
        *,
        job_id: str,
        state: JobState,
        approved_limit: int,
        revision: int,
        replay: bool,
    ) -> dict[str, Any]:
        """Build an await-free continuation ACK and its next atomic token."""

        required = state == "awaiting_continuation"
        return {
            "job_id": job_id,
            "type": "search",
            "state": state,
            "idempotent_replay": replay,
            "continuation": {
                "required": required,
                "chunk_size": _SEARCH_CHUNK_SIZE,
                "approved_limit": approved_limit,
                "revision": revision,
                "token": (
                    self._encode_continuation_token(job_id, revision)
                    if required
                    else None
                ),
            },
        }

    def _resume_ack(
        self,
        *,
        job_id: str,
        kind: Literal["search", "details"],
        state: JobState,
        revision: int,
        replay: bool,
    ) -> dict[str, Any]:
        """Build an await-free resume ACK and expose the next cycle token."""

        required = state == "needs_attention"
        return {
            **self._creation_ack(
                job_id=job_id,
                kind=kind,
                state=state,
                replay=replay,
            ),
            "resume": {
                "required": required,
                "revision": revision,
                "token": (
                    self._encode_resume_token(job_id, revision) if required else None
                ),
            },
        }

    async def _insert_search_page_locked(
        self,
        db: aiosqlite.Connection,
        job_id: str,
        page_number: int,
        now: float,
    ) -> None:
        await db.execute(
            """
            INSERT OR IGNORE INTO work_items (
                job_id, kind, sequence, page_number, status,
                created_at, updated_at
            ) VALUES (?, 'search_page', ?, ?, 'pending', ?, ?)
            """,
            (job_id, page_number, page_number, now, now),
        )

    def _normalize_search_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise JobValidationError("Search request must be an object")
        raw = payload.get("search", payload)
        if not isinstance(raw, Mapping):
            raise JobValidationError("search must be an object")
        search = {str(key): value for key, value in raw.items() if value is not None}
        url = search.get("url")
        structured_keys = {
            "query",
            "location",
            "radius",
            "min_price",
            "max_price",
        }
        has_structured_filters = any(key in search for key in structured_keys)
        if url is not None:
            if has_structured_filters:
                raise JobValidationError(
                    "Use either a Kleinanzeigen URL or structured filters"
                )
            if not isinstance(url, str):
                raise JobValidationError("url must be a string")
            try:
                search["url"] = validate_kleinanzeigen_url_syntax(url)
            except UnsafeOutboundUrlError as exc:
                raise JobValidationError("URL is not allowed") from exc
        self._json_dumps(search)
        return search

    @staticmethod
    def _normalize_listing_ids(ids: Sequence[Any]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_id in ids:
            try:
                listing_id = normalize_listing_id(raw_id)
            except ValueError as exc:
                raise JobValidationError(str(exc)) from exc
            if listing_id not in seen:
                seen.add(listing_id)
                normalized.append(listing_id)
        return normalized

    @staticmethod
    def _normalize_job_id(job_id: str) -> str:
        if not isinstance(job_id, str):
            raise JobNotFoundError("Job not found")
        try:
            return str(uuid.UUID(job_id))
        except (ValueError, AttributeError) as exc:
            raise JobNotFoundError("Job not found") from exc

    @staticmethod
    def _normalize_idempotency_key(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise JobValidationError("Idempotency-Key must be a string")
        key = value.strip()
        if not key or len(key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
            raise JobValidationError(
                f"Idempotency-Key must contain 1 to {_MAX_IDEMPOTENCY_KEY_LENGTH} characters"
            )
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in key):
            raise JobValidationError("Idempotency-Key contains control characters")
        return key

    @staticmethod
    def _json_dumps(value: Any) -> str:
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise JobValidationError("Request data must be valid JSON") from exc

    def _assert_storage_capacity(self) -> None:
        try:
            free = int(self._disk_usage(self.settings.database_path.parent).free)
        except OSError as exc:
            raise JobStorageUnavailable("Could not inspect job storage") from exc
        if free < self.settings.min_free_disk_bytes:
            raise JobStorageUnavailable(
                "Not enough free disk space to accept another job"
            )

    def _database_size_bytes(self) -> int:
        base = str(self.settings.database_path)
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(f"{base}{suffix}")
            except OSError:
                pass
        return total

    def _job_dto(
        self,
        row: aiosqlite.Row,
        counts: Mapping[str, int],
        runtime: aiosqlite.Row,
        queue_position: int,
    ) -> dict[str, Any]:
        now = self._clock()
        kind = str(row["kind"])
        complete = int(counts.get("complete", 0))
        not_found = int(counts.get("not_found", 0))
        pending = int(counts.get("pending", 0))
        running = int(counts.get("leased", 0))
        needs_attention = int(counts.get("needs_attention", 0))
        cancelled = int(counts.get("cancelled", 0))
        if kind == "search":
            processed_items = int(row["released_count"])
            total_items = row["total_reported"]
        else:
            processed_items = complete + not_found
            total_items = int(row["total_items"])

        cooldown_remaining = max(0.0, float(runtime["circuit_open_until"]) - now)
        outstanding_units = pending + running
        if kind == "search" and row["total_pages"] is not None:
            outstanding_units += max(
                0, int(row["total_pages"]) - int(row["next_search_page"]) + 1
            )
        estimate = cooldown_remaining + outstanding_units * (
            self.settings.min_dispatch_interval_seconds
            + self.settings.dispatch_jitter_seconds / 2
        )
        continuation_required = row["state"] == "awaiting_continuation"
        open_errors = needs_attention
        dto: dict[str, Any] = {
            "job_id": row["id"],
            "type": kind,
            "state": row["state"],
            "created_at": self._iso_datetime(float(row["created_at"])),
            "updated_at": self._iso_datetime(float(row["updated_at"])),
            "completed_at": (
                self._iso_datetime(float(row["completed_at"]))
                if row["completed_at"] is not None
                else None
            ),
            "progress": {
                "total_items": total_items,
                "processed_items": processed_items,
                "pending": pending,
                "running": running,
                "complete": complete,
                "not_found": not_found,
                "needs_attention": needs_attention,
                "cancelled": cancelled,
            },
            "results": {
                "discovered": int(row["discovered_count"]),
                "available": (
                    int(row["released_count"])
                    if kind == "search"
                    else int(row["total_items"])
                ),
            },
            "continuation": {
                "required": continuation_required,
                "chunk_size": _SEARCH_CHUNK_SIZE,
                "approved_limit": int(row["approved_limit"]),
                "revision": int(row["continuation_revision"]),
                "token": (
                    self._encode_continuation_token(
                        str(row["id"]),
                        int(row["continuation_revision"]),
                    )
                    if continuation_required
                    else None
                ),
            },
            "resume": {
                "required": row["state"] == "needs_attention",
                "revision": int(row["resume_revision"]),
                "token": (
                    self._encode_resume_token(
                        str(row["id"]),
                        int(row["resume_revision"]),
                    )
                    if row["state"] == "needs_attention"
                    else None
                ),
            },
            "queue": {
                "jobs_ahead": max(0, queue_position),
                "estimated_remaining_seconds": round(estimate, 3),
            },
            "cooldown": {
                "reason": runtime["circuit_reason"],
                "until": (
                    self._iso_datetime(float(runtime["circuit_open_until"]))
                    if runtime["circuit_reason"] is not None
                    else None
                ),
                "remaining_seconds": round(cooldown_remaining, 3),
            },
            "errors": {
                "open": open_errors,
                "last": (
                    {
                        "code": row["last_error_code"],
                        "message": row["last_error_message"],
                    }
                    if row["last_error_code"]
                    else None
                ),
            },
        }
        if kind == "search":
            dto["results"]["reported_total"] = row["total_reported"]
            dto["results"]["reported_pages"] = row["total_pages"]
        if row["source_search_job_id"]:
            dto["source_search_job_id"] = row["source_search_job_id"]
        return dto

    def _runtime_dto(self, runtime: aiosqlite.Row) -> dict[str, Any]:
        now = self._clock()
        open_until = float(runtime["circuit_open_until"])
        next_start_at = float(runtime["next_start_at"])
        reason = runtime["circuit_reason"]
        remaining = max(0.0, open_until - now)
        if reason is None:
            state = "closed"
        elif remaining > 0:
            state = "open"
        else:
            state = "half_open"
        return {
            "next_start_at": next_start_at,
            "next_start_delay_seconds": round(max(0.0, next_start_at - now), 3),
            "circuit_open_until": open_until,
            "circuit": {
                "state": state,
                "reason": reason,
                "cooldown_remaining_seconds": round(remaining, 3),
                "level": int(runtime["circuit_level"]),
                "probe_in_flight": bool(runtime["probe_in_flight"]),
            },
            "controller_snapshot": self._controller_snapshot(runtime),
        }

    def _controller_snapshot(self, runtime: aiosqlite.Row) -> dict[str, Any]:
        now = self._clock()
        next_start_at = float(runtime["next_start_at"])
        circuit_open_until = float(runtime["circuit_open_until"])
        return {
            "version": 1,
            "captured_at": self._iso_datetime(now),
            "next_start_at": (
                self._iso_datetime(next_start_at) if next_start_at > 0 else None
            ),
            "circuit_reason": runtime["circuit_reason"],
            "circuit_open_until": (
                self._iso_datetime(circuit_open_until)
                if circuit_open_until > 0
                else None
            ),
            "circuit_generation": int(runtime["circuit_generation"]),
            "block_streak": int(runtime["block_streak"]),
        }

    @staticmethod
    def _deadline_from_snapshot(
        snapshot: Mapping[str, Any],
        *,
        absolute_key: str,
        remaining_key: str,
        now: float,
    ) -> float | None:
        if absolute_key in snapshot:
            value = snapshot[absolute_key]
            if value is None:
                return 0.0
            try:
                if isinstance(value, str):
                    normalized = value.strip().replace("Z", "+00:00")
                    parsed = datetime.fromisoformat(normalized)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    deadline = parsed.timestamp()
                else:
                    deadline = float(value)
            except (TypeError, ValueError) as exc:
                raise JobValidationError(
                    f"{absolute_key} must be an epoch number or ISO timestamp"
                ) from exc
            if not math.isfinite(deadline) or deadline < 0:
                raise JobValidationError(
                    f"{absolute_key} must be a finite non-negative number"
                )
            return deadline
        if remaining_key in snapshot:
            value = snapshot[remaining_key]
            try:
                remaining = float(value)
            except (TypeError, ValueError) as exc:
                raise JobValidationError(f"{remaining_key} must be a number") from exc
            if not math.isfinite(remaining) or remaining < 0:
                raise JobValidationError(
                    f"{remaining_key} must be a finite non-negative number"
                )
            return now + remaining
        return None

    @staticmethod
    def _validated_runtime_integer(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise JobValidationError(f"{name} must be a non-negative integer")
        return value

    @staticmethod
    def _iso_datetime(timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()

    @staticmethod
    def _safe_error_text(value: Any) -> str:
        text = str(value)
        text = " ".join(text.replace("\x00", "").split())
        return text[:_MAX_ERROR_LENGTH]

    @staticmethod
    def _encode_cursor(job_id: str, position: int, *, version: int) -> str:
        position_key = "s" if version == 1 else "e"
        payload = json.dumps(
            {"v": version, "j": job_id, position_key: position},
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(
        cursor: str | None,
        job_id: str,
        *,
        expected_version: int,
    ) -> int:
        if cursor is None:
            return 0
        if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
            raise InvalidCursorError("Invalid result cursor")
        try:
            padding = "=" * (-len(cursor) % 4)
            raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
            payload = json.loads(raw)
            position_key = "s" if expected_version == 1 else "e"
            position = payload.get(position_key) if isinstance(payload, dict) else None
            if (
                not isinstance(payload, dict)
                or payload.get("v") != expected_version
                or payload.get("j") != job_id
                or isinstance(position, bool)
                or not isinstance(position, int)
                or position < 0
            ):
                raise ValueError
            return position
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise InvalidCursorError("Invalid result cursor") from exc

    @staticmethod
    def _validate_continuation_boundary(
        after_sequence: int | None,
        expected_sequence: int,
    ) -> None:
        """Require an optional search cursor to anchor the token's tranche."""

        if after_sequence is not None and after_sequence != expected_sequence:
            raise InvalidCursorError(
                "Result cursor does not match the continuation boundary"
            )

    @staticmethod
    def _encode_continuation_token(job_id: str, revision: int) -> str:
        return JobManager._encode_action_token(job_id, "continue", revision)

    @staticmethod
    def _encode_resume_token(job_id: str, revision: int) -> str:
        return JobManager._encode_action_token(job_id, "resume", revision)

    @staticmethod
    def _encode_action_token(job_id: str, action: str, revision: int) -> str:
        payload = json.dumps(
            {"v": 1, "j": job_id, "a": action, "r": revision},
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_continuation_token(token: str, job_id: str) -> int:
        return JobManager._decode_action_token(
            token,
            job_id,
            action="continue",
            label="continuation",
        )

    @staticmethod
    def _decode_resume_token(token: str, job_id: str) -> int:
        return JobManager._decode_action_token(
            token,
            job_id,
            action="resume",
            label="resume",
        )

    @staticmethod
    def _decode_action_token(
        token: str,
        job_id: str,
        *,
        action: str,
        label: str,
    ) -> int:
        if not isinstance(token, str) or not token or len(token) > 512:
            raise JobValidationError(f"Invalid {label} token")
        try:
            padding = "=" * (-len(token) % 4)
            raw = base64.b64decode(token + padding, altchars=b"-_", validate=True)
            payload = json.loads(raw)
            revision = payload.get("r") if isinstance(payload, dict) else None
            if (
                not isinstance(payload, dict)
                or payload.get("v") != 1
                or payload.get("j") != job_id
                or payload.get("a") != action
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 0
            ):
                raise ValueError
            return revision
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise JobValidationError(f"Invalid {label} token") from exc

    async def _worker_loop(self, worker_index: int) -> None:
        del worker_index
        while True:
            self._wake.clear()
            try:
                claimed, _delay = await self._claim_next_work()
                if claimed is None:
                    # Recheck at the configured cadence so injected/fake clocks
                    # can advance deterministically without real-time sleeps.
                    wait_seconds = self.settings.poll_interval_seconds
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=wait_seconds)
                    except TimeoutError:
                        pass
                    continue
                await self._execute_claim(claimed)
            except asyncio.CancelledError:
                raise
            except aiosqlite.Error:
                # A transient SQLite busy/I/O condition must not kill the fixed
                # worker pool. Public create calls still surface storage failures.
                await self._sleep(self.settings.poll_interval_seconds)
            except Exception:
                # Defensive worker containment. Item-level executor errors are
                # handled by _execute_claim and should never arrive here.
                await self._sleep(self.settings.poll_interval_seconds)

    async def _claim_next_work(self) -> tuple[_ClaimedWork | None, float]:
        now = self._clock()
        lease_token = uuid.uuid4().hex
        async with self._transaction() as db:
            if now >= self._next_prune_at:
                await self._prune_expired_jobs(now)
                self._next_prune_at = now + 3_600.0
            await self._recover_expired_leases_locked(db, now)
            runtime = await self._fetch_runtime_locked(db)
            circuit_reason = runtime["circuit_reason"]
            circuit_open_until = float(runtime["circuit_open_until"])
            is_probe = False
            if circuit_reason is not None:
                if circuit_open_until > now:
                    return None, circuit_open_until - now
                if bool(runtime["probe_in_flight"]):
                    return None, self.settings.poll_interval_seconds
                await db.execute(
                    """
                    UPDATE jobs SET state = 'queued', updated_at = ?
                    WHERE state = 'waiting_cooldown'
                    """,
                    (now,),
                )
                is_probe = True

            next_start_at = float(runtime["next_start_at"])
            if next_start_at > now:
                return None, next_start_at - now

            candidate_cursor = await db.execute(
                """
                SELECT w.id AS work_item_id, w.job_id, w.page_number,
                       w.listing_id, w.attempts, j.kind AS job_kind,
                       j.request_json
                FROM work_items AS w
                JOIN jobs AS j ON j.id = w.job_id
                WHERE w.status = 'pending'
                  AND w.next_attempt_at <= ?
                  AND j.state IN ('queued', 'running')
                  AND NOT EXISTS (
                      SELECT 1 FROM work_items AS active
                      WHERE active.job_id = w.job_id
                        AND active.status = 'leased'
                  )
                ORDER BY j.last_scheduled_seq ASC,
                         j.created_at ASC,
                         w.sequence ASC
                LIMIT 1
                """,
                (now,),
            )
            candidate = await candidate_cursor.fetchone()
            if candidate is None:
                future_cursor = await db.execute(
                    """
                    SELECT MIN(w.next_attempt_at)
                    FROM work_items AS w
                    JOIN jobs AS j ON j.id = w.job_id
                    WHERE w.status = 'pending'
                      AND j.state IN ('queued', 'running')
                    """
                )
                future_row = await future_cursor.fetchone()
                if future_row[0] is None:
                    return None, self.settings.poll_interval_seconds
                return None, max(
                    self.settings.poll_interval_seconds,
                    float(future_row[0]) - now,
                )

            dispatch_sequence = int(runtime["dispatch_sequence"]) + 1
            jitter = min(1.0, max(0.0, float(self._random_value())))
            next_start = (
                now
                + self.settings.min_dispatch_interval_seconds
                + jitter * self.settings.dispatch_jitter_seconds
            )
            if is_probe:
                await db.execute(
                    """
                    UPDATE runtime_state
                    SET probe_in_flight = 1, next_start_at = ?,
                        dispatch_sequence = ?, updated_at = ?
                    WHERE singleton = 1
                    """,
                    (next_start, dispatch_sequence, now),
                )
            else:
                await db.execute(
                    """
                    UPDATE runtime_state
                    SET next_start_at = ?, dispatch_sequence = ?, updated_at = ?
                    WHERE singleton = 1
                    """,
                    (next_start, dispatch_sequence, now),
                )
            await db.execute(
                """
                UPDATE work_items
                SET status = 'leased', attempts = attempts + 1,
                    lease_token = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    lease_token,
                    now + self.settings.lease_seconds,
                    now,
                    candidate["work_item_id"],
                ),
            )
            await db.execute(
                """
                UPDATE jobs
                SET state = 'running', last_scheduled_at = ?,
                    last_scheduled_seq = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, dispatch_sequence, now, candidate["job_id"]),
            )
            request = json.loads(candidate["request_json"])
            return (
                _ClaimedWork(
                    work_item_id=int(candidate["work_item_id"]),
                    job_id=str(candidate["job_id"]),
                    job_kind=candidate["job_kind"],
                    request=request,
                    page_number=(
                        int(candidate["page_number"])
                        if candidate["page_number"] is not None
                        else None
                    ),
                    listing_id=candidate["listing_id"],
                    attempt=int(candidate["attempts"]),
                    lease_token=lease_token,
                    is_probe=is_probe,
                    circuit_generation_at_claim=int(runtime["circuit_generation"]),
                    block_streak_at_claim=int(runtime["block_streak"]),
                ),
                0.0,
            )

    async def _execute_claim(self, claimed: _ClaimedWork) -> None:
        heartbeat: asyncio.Task[None] | None = None
        async with self._execution_gate:
            # Cancellation and claim registration share this gate and lock
            # order. Revalidate after the claim transaction so a cancellation
            # committed in between cannot launch one last navigation.
            async with self._transaction() as db:
                if not await self._claim_is_current_locked(db, claimed):
                    await self._abandon_stale_probe_locked(
                        db,
                        claimed,
                        self._clock(),
                    )
                    self._wake.set()
                    return
                job = await self._fetch_job_locked(db, claimed.job_id)
                if job["state"] == "cancelled":
                    await self._abandon_stale_probe_locked(
                        db,
                        claimed,
                        self._clock(),
                    )
                    self._wake.set()
                    return

            if claimed.job_kind == "search":
                task = SearchTask(
                    job_id=claimed.job_id,
                    work_item_id=claimed.work_item_id,
                    search=claimed.request["search"],
                    page_number=int(claimed.page_number),
                    attempt=claimed.attempt,
                    is_retry=claimed.attempt > 0,
                    page_setup_timeout_seconds=(
                        self.settings.page_setup_timeout_seconds
                    ),
                    extraction_timeout_seconds=(
                        self.settings.extraction_timeout_seconds
                    ),
                    cleanup_timeout_seconds=self.settings.cleanup_timeout_seconds,
                )
                execution = asyncio.create_task(self._search_executor(task))
            else:
                task = DetailTask(
                    job_id=claimed.job_id,
                    work_item_id=claimed.work_item_id,
                    listing_id=str(claimed.listing_id),
                    attempt=claimed.attempt,
                    is_retry=claimed.attempt > 0,
                    page_setup_timeout_seconds=(
                        self.settings.page_setup_timeout_seconds
                    ),
                    extraction_timeout_seconds=(
                        self.settings.extraction_timeout_seconds
                    ),
                    cleanup_timeout_seconds=self.settings.cleanup_timeout_seconds,
                )
                execution = asyncio.create_task(self._detail_executor(task))
            self._executions[claimed.work_item_id] = execution
        heartbeat = asyncio.create_task(self._heartbeat_lease(claimed))
        try:
            try:
                completed, _pending = await asyncio.wait(
                    (execution, heartbeat),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                await self._cancel_execution_and_wait(execution)
                raise

            if heartbeat in completed and execution not in completed:
                if heartbeat.cancelled():
                    heartbeat_error: BaseException = RuntimeError(
                        "Lease heartbeat stopped unexpectedly"
                    )
                else:
                    heartbeat_error = heartbeat.exception() or RuntimeError(
                        "Lease heartbeat stopped unexpectedly"
                    )

                # Continuing outbound work without a renewable lease can let a
                # second worker recover and execute the same item concurrently.
                # Stop the executor before making the item claimable again.
                await self._cancel_execution_and_wait(execution)
                try:
                    await self._release_cancelled_execution(claimed)
                except BaseException as release_error:
                    raise release_error from heartbeat_error
                raise heartbeat_error

            try:
                result = await execution
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if (
                    execution.cancelled()
                    and current is not None
                    and not current.cancelling()
                ):
                    await self._release_cancelled_execution(claimed)
                    return
                raise
            except BaseException as exc:
                await self._handle_execution_failure(claimed, exc)
                return

            try:
                if claimed.job_kind == "search":
                    await self._complete_search_work(
                        claimed, self._coerce_search_result(result)
                    )
                else:
                    await self._complete_detail_work(
                        claimed, self._coerce_detail_result(result)
                    )
            except asyncio.CancelledError:
                # Shutdown/caller cancellation during result persistence must
                # terminate the worker. Treating it as an executor failure
                # swallows close()'s cancellation and can hang shutdown forever;
                # close() will atomically restore the still-leased item.
                raise
            except BaseException as exc:
                await self._handle_execution_failure(claimed, exc)
        finally:
            # A failed heartbeat can requeue this item before the old claim's
            # finally block runs. Another worker may already have installed a
            # replacement executor under the same work-item ID; never remove
            # that newer task from cancellation/lease tracking.
            if self._executions.get(claimed.work_item_id) is execution:
                self._executions.pop(claimed.work_item_id, None)
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            self._wake.set()

    async def _heartbeat_lease(self, claimed: _ClaimedWork) -> None:
        while True:
            await self._sleep(self.settings.heartbeat_seconds)
            now = self._clock()
            async with self._transaction() as db:
                cursor = await db.execute(
                    """
                    UPDATE work_items
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'leased' AND lease_token = ?
                    """,
                    (
                        now + self.settings.lease_seconds,
                        now,
                        claimed.work_item_id,
                        claimed.lease_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Lease heartbeat lost ownership")

    @staticmethod
    async def _cancel_execution_and_wait(execution: asyncio.Task[Any]) -> None:
        """Stop an executor fully, even if this worker is cancelled repeatedly."""

        execution.cancel()
        cancellation: asyncio.CancelledError | None = None
        while not execution.done():
            try:
                await asyncio.shield(execution)
            except asyncio.CancelledError as exc:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    cancellation = cancellation or exc
            except BaseException:
                # Executor failures are immaterial while abandoning its lease;
                # only complete termination is required here.
                pass
        if cancellation is not None:
            raise cancellation

    async def _release_cancelled_execution(self, claimed: _ClaimedWork) -> None:
        now = self._clock()
        async with self._transaction() as db:
            job = await self._fetch_job_locked(db, claimed.job_id)
            if job["state"] == "cancelled":
                await self._abandon_stale_probe_locked(db, claimed, now)
                return

            cursor = await db.execute(
                """
                UPDATE work_items
                SET status = 'pending', attempts = MAX(0, attempts - 1),
                    lease_token = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'leased' AND lease_token = ?
                """,
                (now, claimed.work_item_id, claimed.lease_token),
            )
            if cursor.rowcount:
                if claimed.is_probe:
                    # A heartbeat/storage failure abandoned the half-open
                    # executor before it could resolve the circuit. Release the
                    # persisted reservation atomically with requeueing; leaving
                    # probe_in_flight=1 would stop every job until restart.
                    await self._abandon_stale_probe_locked(db, claimed, now)
                await db.execute(
                    "UPDATE jobs SET state = 'queued', updated_at = ? WHERE id = ?",
                    (now, claimed.job_id),
                )

    @staticmethod
    def _coerce_search_result(value: Any) -> SearchPageResult:
        if isinstance(value, SearchPageResult):
            result = value
        elif isinstance(value, Mapping):
            result = SearchPageResult(
                listings=value.get("listings", value.get("results", ())),
                total_results=value.get("total_results"),
                total_pages=value.get("total_pages"),
                page_size=value.get("page_size"),
                has_next_page=value.get("has_next_page"),
            )
        else:
            raise PermanentJobError(
                "Search executor returned an unsupported result",
                error_code="invalid_executor_result",
            )
        if isinstance(result.listings, (str, bytes)) or not isinstance(
            result.listings, Sequence
        ):
            raise PermanentJobError(
                "Search executor did not return a listing array",
                error_code="invalid_executor_result",
            )
        for name, number, minimum in (
            ("total_results", result.total_results, 0),
            ("total_pages", result.total_pages, 0),
            ("page_size", result.page_size, 1),
        ):
            if number is not None and (
                isinstance(number, bool)
                or not isinstance(number, int)
                or number < minimum
            ):
                raise PermanentJobError(
                    f"Search executor returned invalid {name}",
                    error_code="invalid_executor_result",
                )
        if result.has_next_page is not None and not isinstance(
            result.has_next_page, bool
        ):
            raise PermanentJobError(
                "Search executor returned invalid has_next_page",
                error_code="invalid_executor_result",
            )
        return result

    @staticmethod
    def _coerce_detail_result(value: Any) -> DetailItemResult:
        if isinstance(value, DetailItemResult):
            result = value
        elif isinstance(value, Mapping):
            status = value.get("status")
            if status is None:
                if value.get("not_found"):
                    status = "not_found"
                elif value.get("success"):
                    status = "complete"
            data = value.get("data", value if status == "complete" else None)
            result = DetailItemResult(status=status, data=data)
        else:
            raise PermanentJobError(
                "Detail executor returned an unsupported result",
                error_code="invalid_executor_result",
            )
        if result.status not in {"complete", "not_found"}:
            raise PermanentJobError(
                "Detail executor returned invalid status",
                error_code="invalid_executor_result",
            )
        if result.data is not None and not isinstance(result.data, Mapping):
            raise PermanentJobError(
                "Detail executor returned invalid data",
                error_code="invalid_executor_result",
            )
        return result

    async def _complete_search_work(
        self, claimed: _ClaimedWork, result: SearchPageResult
    ) -> None:
        now = self._clock()
        async with self._transaction() as db:
            if not await self._claim_is_current_locked(db, claimed):
                await self._abandon_stale_probe_locked(db, claimed, now)
                return
            job = await self._fetch_job_locked(db, claimed.job_id)
            if job["state"] == "cancelled":
                await self._abandon_stale_probe_locked(db, claimed, now)
                return

            page_number = int(claimed.page_number)
            old_total = job["total_reported"]
            old_pages = job["total_pages"]
            if (
                page_number > 1
                and old_total is not None
                and result.total_results is not None
            ):
                expected_total = int(old_total)
                tolerance = max(25, math.ceil(expected_total * 0.1))
                if abs(result.total_results - expected_total) > tolerance:
                    raise PermanentJobError(
                        "Search result metadata changed implausibly between pages",
                        error_code="search_pagination_mismatch",
                    )

            existing_cursor = await db.execute(
                "SELECT listing_id FROM results WHERE job_id = ?",
                (claimed.job_id,),
            )
            seen = {str(row["listing_id"]) for row in await existing_cursor.fetchall()}
            sequence_cursor = await db.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM results WHERE job_id = ?",
                (claimed.job_id,),
            )
            next_sequence = int((await sequence_cursor.fetchone())[0])
            approved_limit = int(job["approved_limit"])

            for listing in result.listings:
                if not isinstance(listing, Mapping):
                    raise PermanentJobError(
                        "Search result contains a non-object listing",
                        error_code="listing_extraction_error",
                    )
                raw_listing_id = listing.get(
                    "adid", listing.get("id", listing.get("listing_id"))
                )
                try:
                    listing_id = normalize_listing_id(str(raw_listing_id))
                except (TypeError, ValueError) as exc:
                    raise PermanentJobError(
                        "Search result contains a listing without a canonical ID",
                        error_code="listing_extraction_error",
                    ) from exc
                try:
                    data_json = self._json_dumps(dict(listing))
                except JobValidationError as exc:
                    raise PermanentJobError(
                        "Search result contains non-JSON data",
                        error_code="listing_extraction_error",
                    ) from exc
                if listing_id in seen:
                    continue
                seen.add(listing_id)
                next_sequence += 1
                await db.execute(
                    """
                    INSERT INTO results (
                        job_id, work_item_id, sequence, listing_id, data_json,
                        visible, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        claimed.job_id,
                        claimed.work_item_id,
                        next_sequence,
                        listing_id,
                        data_json,
                        int(next_sequence <= approved_limit),
                        now,
                        now,
                    ),
                )

            discovered = len(seen)
            released_cursor = await db.execute(
                "SELECT COUNT(*) FROM results WHERE job_id = ? AND visible = 1",
                (claimed.job_id,),
            )
            released = int((await released_cursor.fetchone())[0])
            # Page one defines the immutable search snapshot metadata. Counts
            # naturally fluctuate while later pages are fetched, so compatible
            # later values are tolerated but never replace the first-page view.
            reported_total = result.total_results if page_number == 1 else old_total
            total_pages = result.total_pages if page_number == 1 else old_pages
            if total_pages is not None:
                has_more = page_number < int(total_pages)
                if result.has_next_page is False:
                    has_more = False
            elif result.has_next_page is not None:
                has_more = result.has_next_page
            elif reported_total is not None:
                has_more = discovered < int(reported_total)
            elif result.page_size is not None:
                has_more = len(result.listings) >= result.page_size
            else:
                has_more = False

            await db.execute(
                """
                UPDATE work_items
                SET status = 'complete', lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = NULL,
                    last_error_message = NULL, updated_at = ?
                WHERE id = ? AND status = 'leased' AND lease_token = ?
                """,
                (now, claimed.work_item_id, claimed.lease_token),
            )
            next_page = page_number + 1
            hidden = discovered - released
            if hidden > 0 or (discovered >= approved_limit and has_more):
                state: JobState = "awaiting_continuation"
            elif has_more:
                state = "queued"
                await self._insert_search_page_locked(
                    db, claimed.job_id, next_page, now
                )
            else:
                state = "completed"

            if claimed.is_probe:
                await self._clear_circuit_locked(db, now)
            await db.execute(
                """
                UPDATE jobs
                SET state = ?, total_reported = ?, total_pages = ?,
                    next_search_page = ?, search_has_more = ?,
                    discovered_count = ?, released_count = ?, total_items = ?,
                    completed_at = CASE WHEN ? = 'completed' THEN ? ELSE NULL END,
                    last_error_code = NULL, last_error_message = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    state,
                    reported_total,
                    total_pages,
                    next_page,
                    int(has_more),
                    discovered,
                    released,
                    reported_total or discovered,
                    state,
                    now,
                    now,
                    claimed.job_id,
                ),
            )

    async def _complete_detail_work(
        self, claimed: _ClaimedWork, result: DetailItemResult
    ) -> None:
        now = self._clock()
        data_json: str | None = None
        if result.data is not None:
            try:
                data_json = self._json_dumps(dict(result.data))
            except JobValidationError as exc:
                raise PermanentJobError(
                    "Detail result contains non-JSON data",
                    error_code="invalid_executor_result",
                ) from exc

        async with self._transaction() as db:
            if not await self._claim_is_current_locked(db, claimed):
                await self._abandon_stale_probe_locked(db, claimed, now)
                return
            job = await self._fetch_job_locked(db, claimed.job_id)
            if job["state"] == "cancelled":
                await self._abandon_stale_probe_locked(db, claimed, now)
                return
            item_cursor = await db.execute(
                "SELECT sequence FROM work_items WHERE id = ?",
                (claimed.work_item_id,),
            )
            item = await item_cursor.fetchone()
            await db.execute(
                """
                INSERT INTO results (
                    job_id, work_item_id, sequence, listing_id, data_json,
                    visible, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(job_id, listing_id) DO UPDATE SET
                    work_item_id = excluded.work_item_id,
                    data_json = excluded.data_json,
                    updated_at = excluded.updated_at
                """,
                (
                    claimed.job_id,
                    claimed.work_item_id,
                    int(item["sequence"]),
                    claimed.listing_id,
                    data_json,
                    now,
                    now,
                ),
            )
            await db.execute(
                """
                UPDATE work_items
                SET status = ?, lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = NULL, last_error_message = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'leased' AND lease_token = ?
                """,
                (
                    result.status,
                    now,
                    claimed.work_item_id,
                    claimed.lease_token,
                ),
            )
            if claimed.is_probe:
                await self._clear_circuit_locked(db, now)
            state = await self._next_detail_job_state_locked(db, claimed.job_id)
            await db.execute(
                """
                UPDATE jobs
                SET state = ?,
                    completed_at = CASE WHEN ? = 'completed' THEN ? ELSE NULL END,
                    last_error_code = NULL, last_error_message = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (state, state, now, now, claimed.job_id),
            )

    async def _handle_execution_failure(
        self, claimed: _ClaimedWork, exc: BaseException
    ) -> None:
        if isinstance(exc, (UpstreamRateLimited, UpstreamBlocked, OutboundCircuitOpen)):
            await self._trip_circuit(claimed, exc)
            return

        if isinstance(exc, PermanentJobError):
            if claimed.is_probe:
                await self._mark_probe_recovered(claimed)
            await self._mark_needs_attention(claimed, exc)
            return

        if claimed.is_probe and await self._probe_is_still_open():
            await self._reopen_failed_probe(claimed, exc)
            return

        if claimed.attempt < self.settings.retry_budget:
            await self._schedule_retry(claimed, exc)
        else:
            await self._mark_needs_attention(claimed, exc)

    async def _schedule_retry(self, claimed: _ClaimedWork, exc: BaseException) -> None:
        now = self._clock()
        jitter = min(1.0, max(0.0, float(self._random_value())))
        delay = (
            min(2**claimed.attempt, 5.0)
            + jitter * self.settings.dispatch_jitter_seconds
        )
        code, message = self._exception_details(exc)
        async with self._transaction() as db:
            if not await self._claim_is_current_locked(db, claimed):
                return
            await db.execute(
                """
                UPDATE work_items
                SET status = 'pending', next_attempt_at = ?,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = ?, last_error_message = ?, updated_at = ?
                WHERE id = ? AND status = 'leased' AND lease_token = ?
                """,
                (
                    now + delay,
                    code,
                    message,
                    now,
                    claimed.work_item_id,
                    claimed.lease_token,
                ),
            )
            await db.execute(
                """
                UPDATE jobs
                SET state = 'queued', last_error_code = ?,
                    last_error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (code, message, now, claimed.job_id),
            )

    async def _mark_needs_attention(
        self, claimed: _ClaimedWork, exc: BaseException
    ) -> None:
        now = self._clock()
        code, message = self._exception_details(exc)
        async with self._transaction() as db:
            if not await self._claim_is_current_locked(db, claimed):
                return
            await db.execute(
                """
                UPDATE work_items
                SET status = 'needs_attention', lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = ?,
                    last_error_message = ?, updated_at = ?
                WHERE id = ? AND status = 'leased' AND lease_token = ?
                """,
                (
                    code,
                    message,
                    now,
                    claimed.work_item_id,
                    claimed.lease_token,
                ),
            )
            await db.execute(
                """
                UPDATE jobs
                SET state = 'needs_attention', last_error_code = ?,
                    last_error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (code, message, now, claimed.job_id),
            )

    async def _trip_circuit(
        self,
        claimed: _ClaimedWork,
        exc: UpstreamRateLimited | UpstreamBlocked | OutboundCircuitOpen,
    ) -> None:
        now = self._clock()
        code, message = self._exception_details(exc)
        sibling_ids: list[int] = []
        async with self._transaction() as db:
            runtime = await self._fetch_runtime_locked(db)
            old_level = int(runtime["circuit_level"])
            controller_persisted_trip = (
                int(runtime["circuit_generation"]) > claimed.circuit_generation_at_claim
                or int(runtime["block_streak"]) > claimed.block_streak_at_claim
            )
            if controller_persisted_trip:
                level = max(1, int(runtime["block_streak"]), old_level)
            elif isinstance(exc, OutboundCircuitOpen) and not isinstance(
                exc, (UpstreamRateLimited, UpstreamBlocked)
            ):
                level = max(1, old_level)
            else:
                level = min(30, old_level + 1)
            duration = min(
                self.settings.cooldown_initial_seconds * 2 ** (level - 1),
                self.settings.cooldown_max_seconds,
            )
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None:
                try:
                    parsed_retry_after = float(retry_after)
                except (TypeError, ValueError):
                    parsed_retry_after = 0.0
                if math.isfinite(parsed_retry_after):
                    duration = max(duration, min(parsed_retry_after, 86_400.0))
            open_until = max(float(runtime["circuit_open_until"]), now + duration)
            leased_cursor = await db.execute(
                "SELECT id FROM work_items WHERE status = 'leased' AND id != ?",
                (claimed.work_item_id,),
            )
            sibling_ids = [int(row[0]) for row in await leased_cursor.fetchall()]
            await db.execute(
                """
                UPDATE work_items
                SET status = 'pending', attempts = MAX(0, attempts - 1),
                    lease_token = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE status = 'leased'
                """,
                (now,),
            )
            await db.execute(
                """
                UPDATE jobs SET state = 'waiting_cooldown', updated_at = ?
                WHERE state IN ('queued', 'running', 'waiting_cooldown')
                """,
                (now,),
            )
            await db.execute(
                """
                UPDATE jobs SET last_error_code = ?, last_error_message = ?,
                    updated_at = ? WHERE id = ?
                """,
                (code, message, now, claimed.job_id),
            )
            await db.execute(
                """
                UPDATE runtime_state
                SET circuit_reason = ?, circuit_open_until = ?,
                    circuit_level = ?, block_streak = ?,
                    circuit_generation = ?, probe_in_flight = 0, updated_at = ?
                WHERE singleton = 1
                """,
                (
                    self._controller_circuit_reason(exc, runtime["circuit_reason"]),
                    open_until,
                    level,
                    level,
                    (
                        int(runtime["circuit_generation"])
                        if controller_persisted_trip
                        else int(runtime["circuit_generation"]) + 1
                    ),
                    now,
                ),
            )
        for work_id in sibling_ids:
            execution = self._executions.get(work_id)
            if execution is not None:
                execution.cancel()
        self._wake.set()

    async def _reopen_failed_probe(
        self, claimed: _ClaimedWork, exc: BaseException
    ) -> None:
        now = self._clock()
        code, message = self._exception_details(exc)
        async with self._transaction() as db:
            if not await self._claim_is_current_locked(db, claimed):
                return
            runtime = await self._fetch_runtime_locked(db)
            level = max(1, int(runtime["circuit_level"]))
            duration = min(
                self.settings.cooldown_initial_seconds * 2 ** (level - 1),
                self.settings.cooldown_max_seconds,
            )
            await db.execute(
                """
                UPDATE work_items
                SET status = 'pending', lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = ?,
                    last_error_message = ?, updated_at = ?
                WHERE id = ? AND status = 'leased' AND lease_token = ?
                """,
                (
                    code,
                    message,
                    now,
                    claimed.work_item_id,
                    claimed.lease_token,
                ),
            )
            await db.execute(
                """
                UPDATE jobs SET state = 'waiting_cooldown', updated_at = ?
                WHERE state IN ('queued', 'running', 'waiting_cooldown')
                """,
                (now,),
            )
            await db.execute(
                """
                UPDATE runtime_state
                SET circuit_open_until = ?, probe_in_flight = 0, updated_at = ?
                WHERE singleton = 1
                """,
                (now + duration, now),
            )
        self._wake.set()

    async def _mark_probe_recovered(self, claimed: _ClaimedWork) -> None:
        now = self._clock()
        async with self._transaction() as db:
            if await self._claim_is_current_locked(db, claimed):
                await self._clear_circuit_locked(db, now)

    async def _probe_is_still_open(self) -> bool:
        db = self._require_db()
        async with self._db_lock:
            runtime = await self._fetch_runtime_locked(db)
        return runtime["circuit_reason"] is not None

    async def _next_detail_job_state_locked(
        self, db: aiosqlite.Connection, job_id: str
    ) -> JobState:
        cursor = await db.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'needs_attention' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status IN ('pending', 'leased') THEN 1 ELSE 0 END)
            FROM work_items WHERE job_id = ?
            """,
            (job_id,),
        )
        row = await cursor.fetchone()
        if int(row[0] or 0) > 0:
            return "needs_attention"
        if int(row[1] or 0) > 0:
            return "queued"
        return "completed"

    async def _claim_is_current_locked(
        self, db: aiosqlite.Connection, claimed: _ClaimedWork
    ) -> bool:
        cursor = await db.execute(
            "SELECT status, lease_token FROM work_items WHERE id = ?",
            (claimed.work_item_id,),
        )
        row = await cursor.fetchone()
        return bool(
            row is not None
            and row["status"] == "leased"
            and row["lease_token"] == claimed.lease_token
        )

    async def _clear_circuit_locked(self, db: aiosqlite.Connection, now: float) -> None:
        await db.execute(
            """
            UPDATE runtime_state
            SET circuit_reason = NULL, circuit_open_until = 0,
                circuit_level = 0, block_streak = 0,
                probe_in_flight = 0, updated_at = ?
            WHERE singleton = 1
            """,
            (now,),
        )
        await db.execute(
            """
            UPDATE jobs SET state = 'queued', updated_at = ?
            WHERE state = 'waiting_cooldown'
            """,
            (now,),
        )

    async def _abandon_stale_probe_locked(
        self,
        db: aiosqlite.Connection,
        claimed: _ClaimedWork,
        now: float,
    ) -> None:
        if not claimed.is_probe:
            return
        await db.execute(
            """
            UPDATE runtime_state
            SET probe_in_flight = 0,
                circuit_open_until = MIN(circuit_open_until, ?),
                updated_at = ?
            WHERE singleton = 1
            """,
            (now, now),
        )

    def _exception_details(self, exc: BaseException) -> tuple[str, str]:
        code_value = getattr(exc, "error_code", None)
        code = (
            self._safe_error_text(code_value)
            if code_value is not None
            else "executor_error"
        )
        message = self._safe_error_text(exc) or "Executor failed"
        return code[:80], message

    @staticmethod
    def _controller_circuit_reason(exc: BaseException, persisted_reason: Any) -> str:
        if persisted_reason in _CONTROLLER_CIRCUIT_REASONS:
            return str(persisted_reason)
        if isinstance(exc, UpstreamRateLimited):
            return "http_429"
        if isinstance(exc, UpstreamBlocked):
            return "block_page"
        return "half_open_probe_failed"

    async def _recover_interrupted_work(self, now: float) -> None:
        db = self._require_db()
        await db.execute(
            """
            UPDATE work_items
            SET status = 'pending', attempts = MAX(0, attempts - 1),
                lease_token = NULL, lease_expires_at = NULL, updated_at = ?
            WHERE status = 'leased'
            """,
            (now,),
        )
        await db.execute(
            """
            UPDATE jobs SET state = 'queued', updated_at = ?
            WHERE state = 'running'
            """,
            (now,),
        )
        await db.execute(
            "UPDATE runtime_state SET probe_in_flight = 0, updated_at = ? "
            "WHERE singleton = 1",
            (now,),
        )
        runtime = await self._fetch_runtime_locked(db)
        if runtime["circuit_reason"] is None:
            await db.execute(
                """
                UPDATE jobs SET state = 'queued', updated_at = ?
                WHERE state = 'waiting_cooldown'
                """,
                (now,),
            )

    async def _recover_expired_leases_locked(
        self, db: aiosqlite.Connection, now: float
    ) -> None:
        recovered_any = False
        if self._executions:
            expired_cursor = await db.execute(
                """
                SELECT id FROM work_items
                WHERE status = 'leased' AND lease_expires_at <= ?
                """,
                (now,),
            )
            recoverable_ids = [
                int(row[0])
                for row in await expired_cursor.fetchall()
                if int(row[0]) not in self._executions
            ]
            if recoverable_ids:
                recovered_any = True
                await db.executemany(
                    """
                    UPDATE work_items
                    SET status = 'pending', attempts = MAX(0, attempts - 1),
                        lease_token = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE id = ? AND status = 'leased' AND lease_expires_at <= ?
                    """,
                    ((now, work_id, now) for work_id in recoverable_ids),
                )
        else:
            recovered = await db.execute(
                """
                UPDATE work_items
                SET status = 'pending', attempts = MAX(0, attempts - 1),
                    lease_token = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE status = 'leased' AND lease_expires_at <= ?
                """,
                (now, now),
            )
            recovered_any = bool(recovered.rowcount)
        if recovered_any:
            # If persistence failed while abandoning a half-open executor, its
            # lease expiry is the final recovery boundary. No local executor
            # still owns the reclaimed reservation here.
            await db.execute(
                """
                UPDATE runtime_state
                SET probe_in_flight = 0,
                    circuit_open_until = CASE
                        WHEN circuit_reason IS NULL THEN circuit_open_until
                        ELSE MIN(circuit_open_until, ?)
                    END,
                    updated_at = ?
                WHERE singleton = 1 AND probe_in_flight = 1
                """,
                (now, now),
            )
        await db.execute(
            """
            UPDATE jobs
            SET state = 'queued', updated_at = ?
            WHERE state = 'running'
              AND NOT EXISTS (
                  SELECT 1 FROM work_items
                  WHERE work_items.job_id = jobs.id AND status = 'leased'
              )
            """,
            (now,),
        )

    async def _prune_expired_jobs(self, now: float) -> None:
        db = self._require_db()
        cutoff = now - self.settings.retention_days * 86_400
        # Detail jobs contain a durable snapshot and may outlive their source
        # search. Remove expired children first, then only unreferenced searches;
        # otherwise SQLite's foreign-key check can make the entire application
        # startup fail when a younger detail job still references an old search.
        await db.execute(
            """
            DELETE FROM jobs
            WHERE kind = 'details'
              AND state IN ('completed', 'cancelled')
              AND completed_at IS NOT NULL AND completed_at < ?
            """,
            (cutoff,),
        )
        await db.execute(
            """
            DELETE FROM jobs AS source
            WHERE source.kind = 'search'
              AND source.state IN ('completed', 'cancelled')
              AND source.completed_at IS NOT NULL
              AND source.completed_at < ?
              AND NOT EXISTS (
                  SELECT 1 FROM jobs AS dependent
                  WHERE dependent.source_search_job_id = source.id
              )
            """,
            (cutoff,),
        )

    async def _fetch_job_locked(
        self, db: aiosqlite.Connection, job_id: str
    ) -> aiosqlite.Row:
        cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        if row is None:
            raise JobNotFoundError("Job not found")
        return row

    async def _fetch_runtime_locked(self, db: aiosqlite.Connection) -> aiosqlite.Row:
        cursor = await db.execute("SELECT * FROM runtime_state WHERE singleton = 1")
        row = await cursor.fetchone()
        if row is None:
            raise JobStorageUnavailable("Job runtime state is missing")
        return row

    @asynccontextmanager
    async def _transaction(self):
        db = self._require_db()
        async with self._db_lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                await self._finish_db_operation(db.rollback())
                raise
            else:
                await self._finish_db_operation(db.commit())

    @staticmethod
    async def _finish_db_operation(operation: Awaitable[Any]) -> Any:
        task = asyncio.create_task(operation)
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
        result = task.result()
        if cancellation is not None:
            raise cancellation
        return result

    def _acquire_process_lock(self) -> None:
        lock_path = Path(f"{self.settings.database_path}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise JobManagerAlreadyRunning(
                "Another scheduler already owns this job database"
            ) from exc
        self._lock_file = handle

    def _release_process_lock(self) -> None:
        if self._lock_file is None:
            return
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock_file.close()
            self._lock_file = None

    def _require_started(self) -> None:
        if not self._started or self._db is None or self._closing:
            raise JobManagerNotStarted("Job manager is not available")

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise JobManagerNotStarted("Job manager is not available")
        return self._db


__all__ = [
    "DetailExecutor",
    "DetailItemResult",
    "DetailTask",
    "InvalidCursorError",
    "JobConflictError",
    "JobManager",
    "JobManagerAlreadyRunning",
    "JobManagerError",
    "JobManagerNotStarted",
    "JobNotFoundError",
    "JobSettings",
    "JobState",
    "JobStorageUnavailable",
    "JobValidationError",
    "PermanentJobError",
    "RetryableJobError",
    "SearchExecutor",
    "SearchPageResult",
    "SearchTask",
]
