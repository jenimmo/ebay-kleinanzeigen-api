"""Process-wide admission control for outbound Kleinanzeigen navigations.

Only this controller may call Playwright's ``page.goto``. The controller
intentionally stores no URLs, response bodies, cookies, or headers in its metrics.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import os
import random
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar
from urllib.parse import urlsplit


_ENV_PREFIX = "KLEINANZEIGEN_OUTBOUND_"
_MAX_RETRY_AFTER_SECONDS = 86_400.0
_STATE_SNAPSHOT_VERSION = 1
_CIRCUIT_REASONS = {
    "http_429",
    "http_403",
    "block_page",
    "half_open_probe_failed",
}
_ResultT = TypeVar("_ResultT")
_logger = logging.getLogger(__name__)
_BLOCK_TEXT_MARKERS = (
    "ich bin kein roboter",
    "i'm not a robot",
    "verify you are human",
    "verify that you are human",
    "are you a human",
    "bestätigen sie, dass sie ein mensch",
    "bestätige, dass du ein mensch",
    "bist du ein mensch",
    "sicherheitsüberprüfung",
    "security check",
    "ungewöhnlicher datenverkehr",
    "unusual traffic",
    "zugriff verweigert",
    "request blocked",
    "anfrage wurde blockiert",
    "temporarily blocked",
    "ip-bereich wurde gesperrt",
    "ip bereich wurde gesperrt",
    "ip-bereich ist gesperrt",
    "ip bereich ist gesperrt",
    "ip-bereich wurde vorübergehend gesperrt",
    "ip-bereich vorübergehend gesperrt",
    "ip bereich vorübergehend gesperrt",
    "ip-adresse wurde vorübergehend gesperrt",
    "dein zugang ist vorübergehend gesperrt",
    "dein zugang wurde vorübergehend gesperrt",
    "dein konto ist vorübergehend gesperrt",
    "dein konto wurde vorübergehend gesperrt",
    "dein nutzerkonto ist vorübergehend gesperrt",
    "dein nutzerkonto wurde vorübergehend gesperrt",
    "automatisierte anfragen",
    "checking your browser",
    "enable javascript and cookies to continue",
    "press and hold to confirm",
)
_BLOCK_TITLE_MARKERS = (
    "vorübergehend gesperrt",
    "access denied",
    "security check",
    "sicherheitsüberprüfung",
    "just a moment",
    "attention required",
    "captcha challenge",
)
_BLOCK_PATH_MARKERS = (
    "/captcha",
    "/challenge",
    "/blocked",
    "/access-denied",
)
_BLOCK_HOSTS = (
    "captcha-delivery.com",
    "datadome.co",
    "challenges.cloudflare.com",
)
_BLOCK_DOM_SELECTORS = (
    "#challenge-form",
    "#px-captcha",
    ".cf-challenge",
    "[data-captcha]",
    'iframe[src*="captcha"]',
    'iframe[src*="challenge"]',
    'form[action*="captcha"]',
)
_EXPECTED_PAGE_SELECTORS = (
    ".ad-listitem",
    "#srchrslt-list",
    "#viewad-title",
    "#viewad-description-text",
    "#srchrslt-adexpired",
    "#messagebox",
    "#messagebox-bundle",
    '[data-testid="conversation-list"]',
    "[data-conversation-id]",
    "[data-message-id]",
)


def _validate_int(name: str, value: int, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


def _validate_float(name: str, value: float, minimum: float, maximum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite")
    if not minimum <= float(value) <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")


def _env_int(environ: Mapping[str, str], suffix: str, default: int) -> int:
    name = f"{_ENV_PREFIX}{suffix}"
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(environ: Mapping[str, str], suffix: str, default: float) -> float:
    name = f"{_ENV_PREFIX}{suffix}"
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {raw!r}")
    return value


@dataclass(frozen=True, slots=True)
class OutboundSettings:
    """Validated process-local limits for Kleinanzeigen navigation."""

    max_concurrency: int = 1
    min_interval_seconds: float = 3.0
    jitter_seconds: float = 1.0
    queue_capacity: int = 20
    queue_timeout_seconds: float = 30.0
    navigation_timeout_seconds: float = 60.0
    retry_budget: int = 1
    cooldown_seconds: float = 120.0
    max_cooldown_seconds: float = 3_600.0

    def __post_init__(self) -> None:
        _validate_int("max_concurrency", self.max_concurrency, 1, 20)
        _validate_float("min_interval_seconds", self.min_interval_seconds, 0.0, 300.0)
        _validate_float("jitter_seconds", self.jitter_seconds, 0.0, 60.0)
        _validate_int("queue_capacity", self.queue_capacity, 1, 10_000)
        _validate_float(
            "queue_timeout_seconds", self.queue_timeout_seconds, 0.01, 3_600.0
        )
        _validate_float(
            "navigation_timeout_seconds",
            self.navigation_timeout_seconds,
            0.01,
            600.0,
        )
        _validate_int("retry_budget", self.retry_budget, 0, 5)
        _validate_float("cooldown_seconds", self.cooldown_seconds, 1.0, 86_400.0)
        _validate_float(
            "max_cooldown_seconds", self.max_cooldown_seconds, 1.0, 86_400.0
        )
        if self.max_cooldown_seconds < self.cooldown_seconds:
            raise ValueError("max_cooldown_seconds must be at least cooldown_seconds")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> OutboundSettings:
        """Read all limits from the environment and fail clearly on bad values."""

        source = os.environ if environ is None else environ
        defaults = cls()
        settings = cls(
            max_concurrency=_env_int(
                source, "MAX_CONCURRENCY", defaults.max_concurrency
            ),
            min_interval_seconds=_env_float(
                source, "MIN_INTERVAL_SECONDS", defaults.min_interval_seconds
            ),
            jitter_seconds=_env_float(
                source, "JITTER_SECONDS", defaults.jitter_seconds
            ),
            queue_capacity=_env_int(source, "QUEUE_CAPACITY", defaults.queue_capacity),
            queue_timeout_seconds=_env_float(
                source, "QUEUE_TIMEOUT_SECONDS", defaults.queue_timeout_seconds
            ),
            navigation_timeout_seconds=_env_float(
                source,
                "NAVIGATION_TIMEOUT_SECONDS",
                defaults.navigation_timeout_seconds,
            ),
            retry_budget=_env_int(source, "RETRY_BUDGET", defaults.retry_budget),
            cooldown_seconds=_env_float(
                source, "COOLDOWN_SECONDS", defaults.cooldown_seconds
            ),
            max_cooldown_seconds=_env_float(
                source, "MAX_COOLDOWN_SECONDS", defaults.max_cooldown_seconds
            ),
        )
        return settings


class OutboundControlError(RuntimeError):
    """Base class for errors that must reach the API boundary unchanged."""

    error_code = "outbound_control_error"
    http_status = 503

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after

    @property
    def retry_after_seconds(self) -> int | None:
        if self.retry_after is None:
            return None
        value = float(self.retry_after)
        if not math.isfinite(value):
            value = _MAX_RETRY_AFTER_SECONDS
        return max(1, math.ceil(min(value, _MAX_RETRY_AFTER_SECONDS)))


class OutboundQueueFull(OutboundControlError):
    error_code = "outbound_queue_full"
    http_status = 429


class OutboundQueueTimeout(OutboundControlError):
    error_code = "outbound_queue_timeout"
    http_status = 503


class OutboundCircuitOpen(OutboundControlError):
    error_code = "outbound_circuit_open"
    http_status = 503


class OutboundControllerClosed(OutboundControlError):
    error_code = "outbound_controller_closed"
    http_status = 503


class OutboundNavigationTimeout(OutboundControlError):
    error_code = "outbound_navigation_timeout"
    http_status = 504


class UpstreamRateLimited(OutboundControlError):
    error_code = "upstream_rate_limited"
    http_status = 429


class UpstreamBlocked(OutboundControlError):
    error_code = "upstream_blocked"
    http_status = 503


@dataclass(slots=True)
class _Waiter:
    future: asyncio.Future[None]
    is_retry: bool
    probe_generation: int | None
    state: str = "queued"


@dataclass(frozen=True, slots=True)
class _Lease:
    probe_generation: int | None

    @property
    def is_probe(self) -> bool:
        return self.probe_generation is not None


@dataclass(frozen=True, slots=True)
class _UpstreamSignal:
    reason: str
    metric: str
    retry_after: float | None
    error_type: type[OutboundControlError]
    message: str
    block_indicator: str | None = None


class OutboundController:
    """Bound concurrency, start rate, queueing, retries, and upstream cooldowns."""

    def __init__(
        self,
        settings: OutboundSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
        utcnow: Callable[[], datetime] | None = None,
        state_change_callback: Callable[[Mapping[str, Any]], Awaitable[None] | None]
        | None = None,
    ) -> None:
        self.settings = settings
        self._clock = clock
        self._sleep = sleep
        self._random_value = random_value
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self._state_change_callback = state_change_callback

        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._waiters: deque[_Waiter] = deque()
        self._dispatcher: asyncio.Task[None] | None = None
        self._active = 0
        self._next_start_at = 0.0
        self._closed = False

        self._circuit_reason: str | None = None
        self._circuit_open_until = 0.0
        self._circuit_generation = 0
        self._half_open_reserved = False
        self._block_streak = 0

        self._metrics = {
            "starts": 0,
            "completed": 0,
            "failed": 0,
            "retries": 0,
            "queue_full": 0,
            "queue_timeouts": 0,
            "cancellations": 0,
            "circuit_rejections": 0,
            "circuit_trips": 0,
            "circuit_recoveries": 0,
            "upstream_403": 0,
            "upstream_429": 0,
            "block_pages": 0,
            "max_active": 0,
            "max_queued": 0,
            "state_persistence_failures": 0,
        }

    @property
    def retry_budget(self) -> int:
        return self.settings.retry_budget

    def set_state_change_callback(
        self,
        callback: Callable[[Mapping[str, Any]], Awaitable[None] | None] | None,
    ) -> None:
        """Attach or replace persistence after controller construction."""

        if callback is not None and not callable(callback):
            raise TypeError("state_change_callback must be callable or None")
        self._state_change_callback = callback

    def bounded_retry_count(self, requested: int | None = None) -> int:
        """Return a retry count capped by the one shared controller policy."""

        if requested is None:
            return self.settings.retry_budget
        if (
            isinstance(requested, bool)
            or not isinstance(requested, int)
            or requested < 0
        ):
            raise ValueError("retry_count must be a non-negative integer")
        return min(requested, self.settings.retry_budget)

    async def wait_before_retry(self, completed_attempt: int) -> None:
        """Apply the shared bounded exponential retry backoff."""

        if completed_attempt < 0:
            raise ValueError("completed_attempt must be non-negative")
        jitter = self._bounded_random() * self.settings.jitter_seconds
        await self._sleep(min(2**completed_attempt, 5.0) + jitter)

    async def navigate(
        self,
        page: Any,
        url: str,
        *,
        is_retry: bool = False,
        timeout_ms: int | float | None = None,
        wait_until: str | None = None,
    ) -> Any:
        """Admit, execute, and inspect exactly one Playwright navigation."""

        async def existing_page() -> Any:
            return page

        _, response = await self.navigate_with_page(
            existing_page,
            url,
            is_retry=is_retry,
            timeout_ms=timeout_ms,
            wait_until=wait_until,
        )
        return response

    async def run_interaction(
        self,
        operation: Callable[[], Awaitable[_ResultT]],
        *,
        timeout_seconds: float = 60,
        upstream_verified: Callable[[_ResultT], bool] | None = None,
    ) -> _ResultT:
        """Admit one non-Playwright browser operation without retrying it.

        Safari's extension performs its own navigation and DOM checks. It still
        shares concurrency, start spacing, circuit state and accounting with
        the public browser pool. Local extension polling does not use a permit.
        """
        if not callable(operation) or timeout_seconds <= 0:
            raise ValueError("A callable and positive timeout are required")
        lease = await self._acquire(is_retry=False, wait_indefinitely=True)
        succeeded = cancelled = False
        try:
            async with self._start_lock:
                while True:
                    async with self._lock:
                        now = self._clock()
                        self._raise_if_unavailable_locked(
                            now, probe_generation=lease.probe_generation
                        )
                        delay = max(0.0, self._next_start_at - now)
                    if delay <= 0:
                        break
                    await self._sleep(delay)
                async with self._lock:
                    self._raise_if_unavailable_locked(
                        self._clock(), probe_generation=lease.probe_generation
                    )
                    self._record_navigation_start_locked(self._clock(), is_retry=False)
                await self._notify_state_change()
            async with asyncio.timeout(timeout_seconds):
                result = await operation()
            succeeded = True
            if lease.is_probe:
                if upstream_verified is not None and upstream_verified(result):
                    await self._finish_state_change(self._close_after_probe(lease))
                else:
                    # A local DOM read is not evidence that a previously
                    # blocked upstream is reachable again.
                    await self._finish_state_change(self._abandon_probe(lease))
            return result
        except asyncio.CancelledError:
            cancelled = True
            if lease.is_probe:
                await self._finish_state_change(self._abandon_probe(lease))
            raise
        except BaseException:
            if lease.is_probe:
                await self._finish_state_change(self._reopen_failed_probe(lease))
            raise
        finally:
            await self._release_cancellation_safe(
                lease, succeeded=succeeded, cancelled=cancelled
            )

    async def report_interaction_block(
        self, code: str, *, retry_after: float | None = None
    ) -> None:
        """Apply a fixed, verified extension signal to the shared circuit."""
        if code not in {"upstream_blocked", "upstream_rate_limited"}:
            raise ValueError("Unsupported upstream signal")
        limited = code == "upstream_rate_limited"
        await self._raise_upstream_signal(
            _UpstreamSignal(
                reason="http_429" if limited else "block_page",
                metric="upstream_429" if limited else "block_pages",
                retry_after=retry_after,
                error_type=UpstreamRateLimited if limited else UpstreamBlocked,
                message="Kleinanzeigen restricted the Safari interaction",
                block_indicator=None if limited else "safari:verified_block",
            ),
            lease=_Lease(probe_generation=None),
        )

    async def run_navigation(
        self,
        page_factory: Callable[[], Awaitable[Any]],
        url: str,
        operation: Callable[[Any, Any], Awaitable[_ResultT]],
        *,
        cleanup: Callable[[Any | None], Awaitable[None]] | None = None,
        is_retry: bool = False,
        timeout_ms: int | float | None = None,
        wait_until: str | None = None,
        wait_for_circuit: bool = True,
    ) -> _ResultT:
        """Run one durable-scheduler navigation as one controller operation.

        Unlike :meth:`navigate_with_page`, this API neither applies the legacy
        bounded queue timeout nor counts admission, circuit-cooldown, spacing,
        page creation, parsing, or cleanup against Playwright's navigation
        timeout. The concurrency lease is held until ``operation`` and
        ``cleanup`` have both finished. This makes it suitable for a small,
        persistent worker pool without accumulating one coroutine per listing.

        ``operation`` receives the navigated page and Playwright response.
        ``cleanup`` receives the page, or ``None`` when setup did not return it.
        It is always called when supplied; resource-owning factories should
        therefore also retain their context/page in the cleanup closure.
        """

        if not callable(operation):
            raise TypeError("operation must be callable")
        if cleanup is not None and not callable(cleanup):
            raise TypeError("cleanup must be callable")

        timeout_seconds = self._resolve_navigation_timeout(timeout_ms)
        if wait_for_circuit:
            lease = await self._acquire_waiting_for_circuit(is_retry=is_retry)
        else:
            lease = await self._acquire(is_retry=is_retry, wait_indefinitely=True)

        page: Any | None = None
        succeeded = False
        cancelled = False
        cleanup_failed = False
        navigation_succeeded = False
        try:
            page = await self._prepare_page_for_start(page_factory)
            response = await self._goto_and_inspect(
                page,
                url,
                timeout_seconds=timeout_seconds,
                wait_until=wait_until,
                lease=lease,
                is_retry=is_retry,
            )
            navigation_succeeded = True
            if lease.is_probe:
                # The half-open probe answers only whether Kleinanzeigen can be
                # reached without another rate-limit/block signal. Parsing is a
                # separate job concern: a permanent DOM/extraction failure must
                # not leave the process circuit open while the persisted job
                # state already considers the probe recovered.
                await self._finish_state_change(self._close_after_probe(lease))
            result = await operation(page, response)
            succeeded = True
            return result
        except OutboundNavigationTimeout:
            if lease.is_probe:
                await self._finish_state_change(self._reopen_failed_probe(lease))
            raise
        except asyncio.CancelledError:
            cancelled = True
            if lease.is_probe and not navigation_succeeded:
                await self._finish_state_change(self._abandon_probe(lease))
            raise
        except OutboundControlError:
            raise
        except BaseException:
            if lease.is_probe and not navigation_succeeded:
                await self._finish_state_change(self._reopen_failed_probe(lease))
            raise
        finally:
            try:
                if cleanup is not None:
                    await self._run_cleanup_cancellation_safe(cleanup, page)
            except asyncio.CancelledError:
                cleanup_failed = True
                if not cancelled:
                    cancelled = True
                    raise
            except BaseException:
                cleanup_failed = True
                # Cleanup must not replace an upstream block, navigation error,
                # parser failure, or cancellation already propagating.
                if succeeded:
                    raise
            finally:
                await self._release_cancellation_safe(
                    lease,
                    succeeded=succeeded and not cleanup_failed,
                    cancelled=cancelled,
                )

    async def navigate_with_page(
        self,
        page_factory: Callable[[], Awaitable[Any]],
        url: str,
        *,
        is_retry: bool = False,
        timeout_ms: int | float | None = None,
        wait_until: str | None = None,
    ) -> tuple[Any, Any]:
        """Admit before page allocation, then navigate and return page/response."""

        timeout_seconds = self._resolve_navigation_timeout(timeout_ms)
        lease = await self._acquire(is_retry=is_retry)
        succeeded = False
        cancelled = False
        try:
            try:
                # Legacy page factories remain bounded. Spacing is deliberately
                # outside the browser-attempt deadline, matching durable jobs.
                async with asyncio.timeout(timeout_seconds):
                    page = await self._prepare_page_for_start(page_factory)
            except TimeoutError as exc:
                raise OutboundNavigationTimeout(
                    "Kleinanzeigen navigation timed out",
                    retry_after=self.settings.min_interval_seconds,
                ) from exc

            response = await self._goto_and_inspect(
                page,
                url,
                timeout_seconds=timeout_seconds,
                wait_until=wait_until,
                lease=lease,
                is_retry=is_retry,
            )

            if lease.is_probe:
                await self._finish_state_change(self._close_after_probe(lease))
            succeeded = True
            return page, response
        except OutboundNavigationTimeout:
            if lease.is_probe:
                await self._finish_state_change(self._reopen_failed_probe(lease))
            raise
        except asyncio.CancelledError:
            cancelled = True
            if lease.is_probe:
                await self._finish_state_change(self._abandon_probe(lease))
            raise
        except OutboundControlError:
            raise
        except BaseException:
            if lease.is_probe:
                await self._finish_state_change(self._reopen_failed_probe(lease))
            raise
        finally:
            await self._release_cancellation_safe(
                lease, succeeded=succeeded, cancelled=cancelled
            )

    async def _goto_and_inspect(
        self,
        page: Any,
        url: str,
        *,
        timeout_seconds: float,
        wait_until: str | None,
        lease: _Lease,
        is_retry: bool,
    ) -> Any:
        """Run and inspect one browser attempt within its hard deadline."""

        goto_kwargs: dict[str, Any] = {"timeout": timeout_seconds * 1000.0}
        if wait_until is not None:
            goto_kwargs["wait_until"] = wait_until

        attempt_task: asyncio.Task[Any] | None = None
        try:
            async with self._start_lock:
                while True:
                    async with self._lock:
                        now = self._clock()
                        self._raise_if_unavailable_locked(
                            now, probe_generation=lease.probe_generation
                        )
                        delay = max(0.0, self._next_start_at - now)
                    if delay <= 0:
                        break
                    await self._sleep(delay)

                start_outcome: asyncio.Future[bool] = (
                    asyncio.get_running_loop().create_future()
                )
                attempt_task = asyncio.create_task(
                    self._run_browser_attempt(
                        page,
                        url,
                        goto_kwargs=goto_kwargs,
                        timeout_seconds=timeout_seconds,
                        lease=lease,
                        is_retry=is_retry,
                        start_outcome=start_outcome,
                    )
                )
                started = await start_outcome
                if not started:
                    return await attempt_task
                # Persist only after page.goto has actually begun. Keeping the
                # start lock here means a slow callback can lengthen, but never
                # shorten, the configured gap between real navigation starts.
                await self._notify_state_change()

            return await attempt_task
        except BaseException:
            if attempt_task is not None:
                await self._cancel_attempt_task(attempt_task)
            raise

    async def _run_browser_attempt(
        self,
        page: Any,
        url: str,
        *,
        goto_kwargs: Mapping[str, Any],
        timeout_seconds: float,
        lease: _Lease,
        is_retry: bool,
        start_outcome: asyncio.Future[bool],
    ) -> Any:
        try:
            # This is the final check and timestamp immediately before the only
            # production page.goto call. There is no intervening suspension.
            async with self._lock:
                now = self._clock()
                self._raise_if_unavailable_locked(
                    now, probe_generation=lease.probe_generation
                )
                self._record_navigation_start_locked(now, is_retry=is_retry)
            start_outcome.set_result(True)

            try:
                async with asyncio.timeout(timeout_seconds):
                    response = await page.goto(url, **dict(goto_kwargs))
                    # Response/header and block-page inspection are part of the
                    # same upstream attempt. A wedged browser driver must not
                    # retain the sole global slot indefinitely.
                    upstream_signal = await self._inspect_response(page, response)
            except TimeoutError as exc:
                raise OutboundNavigationTimeout(
                    "Kleinanzeigen navigation timed out",
                    retry_after=self.settings.min_interval_seconds,
                ) from exc
            except Exception as exc:
                if self._is_playwright_timeout(exc):
                    raise OutboundNavigationTimeout(
                        "Kleinanzeigen navigation timed out",
                        retry_after=self.settings.min_interval_seconds,
                    ) from exc
                raise

            # Detection stays inside the browser deadline. Once a block signal
            # has been observed, its cancellation-safe circuit transition and
            # classified exception run outside that deadline in this owned
            # child task. Parent cancellation therefore cannot discard a 429
            # tuple between detection and enforcement.
            await self._raise_upstream_signal(upstream_signal, lease=lease)
            return response
        except BaseException:
            if not start_outcome.done():
                start_outcome.set_result(False)
            raise

    @staticmethod
    async def _cancel_attempt_task(task: asyncio.Task[Any]) -> None:
        """Stop a started browser attempt even under repeated cancellation."""

        task.cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            task.result()
        except BaseException:
            pass

    def _resolve_navigation_timeout(self, timeout_ms: int | float | None) -> float:
        timeout_seconds = self.settings.navigation_timeout_seconds
        if timeout_ms is None:
            return timeout_seconds
        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, (int, float))
            or not math.isfinite(float(timeout_ms))
            or float(timeout_ms) <= 0
        ):
            raise ValueError("timeout_ms must be a finite positive number")
        return min(timeout_seconds, float(timeout_ms) / 1000.0)

    @staticmethod
    def _is_playwright_timeout(exc: Exception) -> bool:
        error_type = type(exc)
        return (
            error_type.__name__ == "TimeoutError"
            and error_type.__module__.startswith("playwright.")
        )

    async def circuit_snapshot(self) -> dict[str, Any]:
        """Return restart-safe, non-sensitive rate/circuit state.

        Monotonic deadlines are converted to UTC timestamps because monotonic
        clock values cannot be reused after a process restart.
        """

        async with self._lock:
            return self._state_snapshot_locked()

    async def restore_circuit_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        """Restore a snapshot produced by :meth:`circuit_snapshot`.

        This is intended for startup before workers are launched. Invalid or
        future-version state fails clearly instead of silently disabling a
        persisted cooldown.
        """

        if not isinstance(snapshot, Mapping):
            raise ValueError("outbound state snapshot must be a mapping")
        if snapshot.get("version") != _STATE_SNAPSHOT_VERSION:
            raise ValueError("unsupported outbound state snapshot version")

        reason = snapshot.get("circuit_reason")
        if reason is not None and reason not in _CIRCUIT_REASONS:
            raise ValueError("invalid circuit_reason in outbound state snapshot")

        generation = snapshot.get("circuit_generation", 0)
        streak = snapshot.get("block_streak", 0)
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise ValueError("circuit_generation must be an integer")
        if generation < 0:
            raise ValueError("circuit_generation must be non-negative")
        if isinstance(streak, bool) or not isinstance(streak, int):
            raise ValueError("block_streak must be an integer")
        if not 0 <= streak <= 1_000_000:
            raise ValueError("block_streak is outside the supported range")

        next_start_delay = self._snapshot_deadline_delay(snapshot.get("next_start_at"))
        circuit_delay = self._snapshot_deadline_delay(
            snapshot.get("circuit_open_until")
        )

        async with self._lock:
            if self._active or self._waiters:
                raise RuntimeError("cannot restore outbound state while work is active")
            now = self._clock()
            self._next_start_at = now + next_start_delay if next_start_delay else 0.0
            self._circuit_reason = reason
            self._circuit_open_until = (
                now + circuit_delay if reason is not None and circuit_delay else now
            )
            self._circuit_generation = generation
            self._half_open_reserved = False
            self._block_streak = streak if reason is not None else 0

    def _state_snapshot_locked(self) -> dict[str, Any]:
        now_monotonic = self._clock()
        now_utc = self._normalized_utcnow()
        return {
            "version": _STATE_SNAPSHOT_VERSION,
            "captured_at": now_utc.isoformat(),
            "next_start_at": self._deadline_to_iso(
                self._next_start_at, now_monotonic, now_utc
            ),
            "circuit_reason": self._circuit_reason,
            "circuit_open_until": self._deadline_to_iso(
                self._circuit_open_until, now_monotonic, now_utc
            )
            if self._circuit_reason is not None
            else None,
            "circuit_generation": self._circuit_generation,
            "block_streak": self._block_streak,
        }

    def _normalized_utcnow(self) -> datetime:
        value = self._utcnow()
        if not isinstance(value, datetime):
            raise TypeError("utcnow must return a datetime")
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _deadline_to_iso(
        deadline: float, now_monotonic: float, now_utc: datetime
    ) -> str | None:
        remaining = max(0.0, deadline - now_monotonic)
        if remaining <= 0:
            return None
        return datetime.fromtimestamp(
            now_utc.timestamp() + remaining,
            tz=timezone.utc,
        ).isoformat()

    def _snapshot_deadline_delay(self, raw: Any) -> float:
        if raw is None:
            return 0.0
        if not isinstance(raw, str):
            raise ValueError("outbound state deadlines must be ISO timestamps")
        try:
            value = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("invalid outbound state deadline") from exc
        if value.tzinfo is None:
            raise ValueError("outbound state deadline must include a timezone")
        delay = (
            value.astimezone(timezone.utc) - self._normalized_utcnow()
        ).total_seconds()
        return max(0.0, min(delay, _MAX_RETRY_AFTER_SECONDS))

    async def _notify_state_change(self) -> None:
        callback = self._state_change_callback
        if callback is None:
            return
        snapshot = await self.circuit_snapshot()
        try:
            result = callback(snapshot)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            # In-memory rate/circuit enforcement remains authoritative. A
            # persistence outage must not replace an already detected 429 or
            # block signal with an unrelated callback exception.
            async with self._lock:
                self._metrics["state_persistence_failures"] += 1
            _logger.exception("Persisting outbound controller state failed")

    async def status(self) -> dict[str, Any]:
        """Return a non-sensitive snapshot suitable for an API status endpoint."""

        async with self._lock:
            now = self._clock()
            remaining = max(0.0, self._circuit_open_until - now)
            if self._circuit_reason is None:
                circuit_state = "closed"
            elif remaining > 0:
                circuit_state = "open"
            else:
                circuit_state = "half_open"

            return {
                "limits": {
                    "max_concurrency": self.settings.max_concurrency,
                    "min_interval_seconds": self.settings.min_interval_seconds,
                    "jitter_seconds": self.settings.jitter_seconds,
                    "queue_capacity": self.settings.queue_capacity,
                    "queue_timeout_seconds": self.settings.queue_timeout_seconds,
                    "navigation_timeout_seconds": self.settings.navigation_timeout_seconds,
                    "retry_budget": self.settings.retry_budget,
                    "cooldown_seconds": self.settings.cooldown_seconds,
                    "max_cooldown_seconds": self.settings.max_cooldown_seconds,
                },
                "active": self._active,
                "queued": len(self._waiters),
                "closed": self._closed,
                "circuit": {
                    "state": circuit_state,
                    "reason": self._circuit_reason,
                    "cooldown_remaining_seconds": round(remaining, 3),
                },
                "metrics": dict(self._metrics),
            }

    async def close(self) -> None:
        """Reject new work and release every queued caller during shutdown."""

        dispatcher: asyncio.Task[None] | None
        async with self._lock:
            self._closed = True
            dispatcher = self._dispatcher
            self._dispatcher = None
            self._reject_waiters_locked(OutboundControllerClosed)
        if dispatcher is not None and dispatcher is not asyncio.current_task():
            dispatcher.cancel()
            try:
                await dispatcher
            except asyncio.CancelledError:
                pass

    async def _acquire_waiting_for_circuit(self, *, is_retry: bool) -> _Lease:
        """Wait without an age limit until capacity and circuit admission exist."""

        while True:
            try:
                return await self._acquire(
                    is_retry=is_retry,
                    wait_indefinitely=True,
                )
            except OutboundCircuitOpen as exc:
                # Circuit waiting belongs to persistent job scheduling. It is
                # intentionally outside the navigation timeout and remains
                # cancellation-friendly through the injected sleep primitive.
                await self._sleep(max(0.001, float(exc.retry_after or 1.0)))

    async def _acquire(
        self, *, is_retry: bool, wait_indefinitely: bool = False
    ) -> _Lease:
        waiter: _Waiter | None = None
        async with self._lock:
            now = self._clock()
            self._raise_if_unavailable_locked(now)
            probe_generation = self._reserve_probe_locked(now)

            can_start_now = (
                not self._waiters and self._active < self.settings.max_concurrency
            )
            if can_start_now:
                self._record_admission_locked()
                return _Lease(probe_generation=probe_generation)

            if (
                not wait_indefinitely
                and len(self._waiters) >= self.settings.queue_capacity
            ):
                self._release_probe_reservation_locked(probe_generation)
                self._metrics["queue_full"] += 1
                raise OutboundQueueFull(
                    "Kleinanzeigen outbound queue is full",
                    retry_after=max(1.0, self.settings.min_interval_seconds),
                )

            future = asyncio.get_running_loop().create_future()
            waiter = _Waiter(
                future=future,
                is_retry=is_retry,
                probe_generation=probe_generation,
            )
            self._waiters.append(waiter)
            self._metrics["max_queued"] = max(
                self._metrics["max_queued"], len(self._waiters)
            )
            self._ensure_dispatcher_locked()

        try:
            if wait_indefinitely:
                await asyncio.shield(waiter.future)
            else:
                await asyncio.wait_for(
                    asyncio.shield(waiter.future),
                    timeout=self.settings.queue_timeout_seconds,
                )
            return _Lease(probe_generation=waiter.probe_generation)
        except TimeoutError as exc:
            timed_out = await self._withdraw_waiter_cancellation_safe(
                waiter, timed_out=True
            )
            if not timed_out:
                return _Lease(probe_generation=waiter.probe_generation)
            raise OutboundQueueTimeout(
                "Timed out waiting for Kleinanzeigen outbound capacity",
                retry_after=max(1.0, self.settings.min_interval_seconds),
            ) from exc
        except asyncio.CancelledError:
            await self._withdraw_waiter_cancellation_safe(waiter, cancelled=True)
            raise

    def _raise_if_unavailable_locked(
        self, now: float, *, probe_generation: int | None = None
    ) -> None:
        if self._closed:
            raise OutboundControllerClosed(
                "Kleinanzeigen outbound controller is closed"
            )
        stale_probe = (
            probe_generation is not None
            and probe_generation != self._circuit_generation
        )
        if self._circuit_reason is None:
            if stale_probe:
                self._metrics["circuit_rejections"] += 1
                raise OutboundCircuitOpen(
                    "Kleinanzeigen outbound circuit state changed",
                    retry_after=1.0,
                )
            return
        remaining = self._circuit_open_until - now
        matching_probe = (
            probe_generation is not None
            and self._half_open_reserved
            and probe_generation == self._circuit_generation
        )
        if (
            remaining > 0
            or stale_probe
            or (self._half_open_reserved and not matching_probe)
        ):
            self._metrics["circuit_rejections"] += 1
            raise OutboundCircuitOpen(
                "Kleinanzeigen outbound circuit is open",
                retry_after=max(remaining, 1.0),
            )

    def _reserve_probe_locked(self, now: float) -> int | None:
        if self._circuit_reason is None or now < self._circuit_open_until:
            return None
        self._half_open_reserved = True
        return self._circuit_generation

    def _release_probe_reservation_locked(self, probe_generation: int | None) -> None:
        if (
            probe_generation is not None
            and probe_generation == self._circuit_generation
        ):
            self._half_open_reserved = False

    def _record_admission_locked(self) -> None:
        self._active += 1
        self._metrics["max_active"] = max(self._metrics["max_active"], self._active)

    def _record_navigation_start_locked(self, now: float, *, is_retry: bool) -> None:
        self._metrics["starts"] += 1
        if is_retry:
            self._metrics["retries"] += 1
        self._next_start_at = (
            now
            + self.settings.min_interval_seconds
            + self._bounded_random() * self.settings.jitter_seconds
        )

    def _bounded_random(self) -> float:
        return min(1.0, max(0.0, float(self._random_value())))

    def _ensure_dispatcher_locked(self) -> None:
        if not self._waiters or self._active >= self.settings.max_concurrency:
            return
        if self._dispatcher is None or self._dispatcher.done():
            self._dispatcher = asyncio.create_task(self._dispatch())

    async def _dispatch(self) -> None:
        current = asyncio.current_task()
        try:
            async with self._lock:
                self._discard_cancelled_waiters_locked()
                if self._closed:
                    return
                now = self._clock()
                if self._circuit_reason is not None and now < self._circuit_open_until:
                    self._reject_waiters_locked(OutboundCircuitOpen)
                    return
                while self._waiters and self._active < self.settings.max_concurrency:
                    waiter = self._waiters.popleft()
                    waiter.state = "granted"
                    self._record_admission_locked()
                    if not waiter.future.done():
                        waiter.future.set_result(None)
        finally:
            async with self._lock:
                if self._dispatcher is current:
                    self._dispatcher = None
                self._ensure_dispatcher_locked()

    async def _prepare_page_for_start(
        self,
        page_factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Create a page after admission; start pacing happens at page.goto."""

        return await page_factory()

    def _discard_cancelled_waiters_locked(self) -> None:
        while self._waiters and self._waiters[0].future.cancelled():
            waiter = self._waiters.popleft()
            waiter.state = "cancelled"
            self._release_probe_reservation_locked(waiter.probe_generation)

    async def _withdraw_waiter(
        self,
        waiter: _Waiter,
        *,
        timed_out: bool = False,
        cancelled: bool = False,
    ) -> bool:
        """Withdraw a waiter; return True unless it was already granted cleanly."""

        async with self._lock:
            if waiter.state == "queued":
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
                waiter.state = "cancelled"
                waiter.future.cancel()
                self._release_probe_reservation_locked(waiter.probe_generation)
                if timed_out:
                    self._metrics["queue_timeouts"] += 1
                if cancelled:
                    self._metrics["cancellations"] += 1
                return True

            if waiter.state == "granted" and cancelled:
                waiter.state = "cancelled"
                self._active -= 1
                self._metrics["failed"] += 1
                self._metrics["cancellations"] += 1
                self._release_probe_reservation_locked(waiter.probe_generation)
                self._ensure_dispatcher_locked()
                return True

            return False

    async def _withdraw_waiter_cancellation_safe(
        self,
        waiter: _Waiter,
        *,
        timed_out: bool = False,
        cancelled: bool = False,
    ) -> bool:
        """Finish waiter withdrawal even if cancellation is repeated."""

        withdraw_task = asyncio.create_task(
            self._withdraw_waiter(
                waiter,
                timed_out=timed_out,
                cancelled=cancelled,
            )
        )
        pending_cancellation: asyncio.CancelledError | None = None
        while not withdraw_task.done():
            try:
                await asyncio.shield(withdraw_task)
            except asyncio.CancelledError as exc:
                pending_cancellation = pending_cancellation or exc

        result = withdraw_task.result()
        if pending_cancellation is not None:
            raise pending_cancellation
        return result

    async def _release(
        self, lease: _Lease, *, succeeded: bool, cancelled: bool
    ) -> None:
        async with self._lock:
            self._active -= 1
            if self._active < 0:
                self._active = 0
                raise RuntimeError("Outbound controller slot accounting underflow")
            if succeeded:
                self._metrics["completed"] += 1
            else:
                self._metrics["failed"] += 1
            if cancelled:
                self._metrics["cancellations"] += 1
            self._ensure_dispatcher_locked()

    async def _release_cancellation_safe(
        self, lease: _Lease, *, succeeded: bool, cancelled: bool
    ) -> None:
        """Finish slot accounting even if the caller is cancelled repeatedly."""

        release_task = asyncio.create_task(
            self._release(lease, succeeded=succeeded, cancelled=cancelled)
        )
        pending_cancellation: asyncio.CancelledError | None = None

        while not release_task.done():
            try:
                await asyncio.shield(release_task)
            except asyncio.CancelledError as exc:
                # A second task.cancel() can arrive while the first cancellation
                # is already unwinding navigate(). Do not let it strand a slot.
                pending_cancellation = pending_cancellation or exc

        # Preserve accounting errors instead of silently replacing them with a
        # cancellation that arrived during cleanup.
        release_task.result()
        if pending_cancellation is not None:
            raise pending_cancellation

    async def _run_cleanup_cancellation_safe(
        self,
        cleanup: Callable[[Any | None], Awaitable[None]],
        page: Any | None,
    ) -> None:
        """Complete caller cleanup even under repeated task cancellation."""

        cleanup_task = asyncio.create_task(cleanup(page))
        pending_cancellation: asyncio.CancelledError | None = None
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as exc:
                pending_cancellation = pending_cancellation or exc

        cleanup_task.result()
        if pending_cancellation is not None:
            raise pending_cancellation

    async def _finish_state_change(self, operation: Awaitable[Any]) -> Any:
        """Finish controller state transitions despite repeated cancellation."""

        state_task = asyncio.create_task(operation)
        pending_cancellation: asyncio.CancelledError | None = None
        while not state_task.done():
            try:
                await asyncio.shield(state_task)
            except asyncio.CancelledError as exc:
                pending_cancellation = pending_cancellation or exc

        result = state_task.result()
        if pending_cancellation is not None:
            raise pending_cancellation
        return result

    async def inspect_interaction_response(
        self, page: Any, response: Any = None
    ) -> None:
        """Apply the shared circuit to a form/XHR response inside an admitted operation.

        This does not acquire a second permit or retry a form submission. Callers
        must already hold a navigation lifecycle permit for automated actions.
        Inspecting an already loaded DOM during manual login is also allowed:
        it sends no request and must not reserve a permit for human input.
        """
        signal = await self._inspect_response(page, response)
        await self._raise_upstream_signal(signal, lease=_Lease(probe_generation=None))

    async def _inspect_response(
        self, page: Any, response: Any
    ) -> _UpstreamSignal | None:
        status = getattr(response, "status", None) if response is not None else None
        headers = await self._response_headers(response)
        retry_after = self._parse_retry_after(headers.get("retry-after"))

        if status == 429:
            return _UpstreamSignal(
                reason="http_429",
                retry_after=retry_after,
                metric="upstream_429",
                error_type=UpstreamRateLimited,
                message="Kleinanzeigen returned HTTP 429",
            )
        if status == 403:
            return _UpstreamSignal(
                reason="http_403",
                retry_after=retry_after,
                metric="upstream_403",
                error_type=UpstreamBlocked,
                message="Kleinanzeigen returned HTTP 403",
            )

        block_indicator = await self._page_block_indicator(page)
        if block_indicator:
            return _UpstreamSignal(
                reason="block_page",
                retry_after=retry_after,
                metric="block_pages",
                error_type=UpstreamBlocked,
                message=("Kleinanzeigen returned a captcha, challenge, or block page"),
                block_indicator=block_indicator,
            )
        return None

    async def _raise_upstream_signal(
        self, signal: _UpstreamSignal | None, *, lease: _Lease
    ) -> None:
        if signal is None:
            return
        effective_retry_after = await self._finish_state_change(
            self._trip_circuit(
                reason=signal.reason,
                retry_after=signal.retry_after,
                metric=signal.metric,
                lease=lease,
            )
        )
        error = signal.error_type(
            signal.message,
            retry_after=effective_retry_after,
        )
        # Fixed policy markers only; never page text, full URLs or form values.
        error.block_indicator = signal.block_indicator
        raise error

    async def _response_headers(self, response: Any) -> dict[str, str]:
        if response is None:
            return {}
        raw = getattr(response, "headers", None)
        if isinstance(raw, Mapping):
            return {str(key).lower(): str(value) for key, value in raw.items()}
        all_headers = getattr(response, "all_headers", None)
        if callable(all_headers):
            try:
                values = await all_headers()
            except Exception:
                return {}
            if isinstance(values, Mapping):
                return {str(key).lower(): str(value) for key, value in values.items()}
        return {}

    async def _page_has_block_signal(self, page: Any) -> bool:
        return await self._page_block_indicator(page) is not None

    async def _page_block_indicator(self, page: Any) -> str | None:
        page_url = str(getattr(page, "url", "") or "")
        parsed_url = urlsplit(page_url)
        path = parsed_url.path.casefold()
        host = (parsed_url.hostname or "").casefold()
        for domain in _BLOCK_HOSTS:
            if host == domain or host.endswith(f".{domain}"):
                return "host:" + domain
        for marker in _BLOCK_PATH_MARKERS:
            if path == marker or path.startswith(f"{marker}/"):
                return "path:" + marker

        selector = await self._first_matching_selector(
            page, _BLOCK_DOM_SELECTORS, visible_only=True
        )
        if selector is not None:
            return "visible_selector:" + selector

        has_expected_page_structure = await self._has_any_selector(
            page, _EXPECTED_PAGE_SELECTORS
        )

        title = ""
        get_title = getattr(page, "title", None)
        if callable(get_title):
            try:
                title = str(await get_title())
            except Exception:
                pass

        visible_text = ""
        locator_factory = getattr(page, "locator", None)
        if callable(locator_factory):
            try:
                body = locator_factory("body")
                visible_text = str(await body.inner_text(timeout=2_000))[:100_000]
            except Exception:
                pass
        elif callable(getattr(page, "content", None)):
            try:
                # This fallback primarily supports lightweight offline fakes.
                visible_text = re.sub(
                    r"<[^>]+>", " ", str(await page.content())[:200_000]
                )
            except Exception:
                pass

        folded_title = title.casefold()
        folded_text = " ".join(visible_text.casefold().split())
        # User-generated titles and descriptions may legitimately mention words
        # such as "captcha" or "access denied". Only textual challenge signals
        # without recognizable Kleinanzeigen search/detail structure open the
        # process-wide circuit. Status, challenge host/path, and DOM checks above
        # remain authoritative on their own.
        if not has_expected_page_structure:
            for marker in _BLOCK_TITLE_MARKERS:
                if marker in folded_title:
                    return "title:" + marker
            for marker in _BLOCK_TEXT_MARKERS:
                if marker in folded_text:
                    return "body:" + marker
        return None

    async def _has_any_selector(self, page: Any, selectors: tuple[str, ...]) -> bool:
        return await self._first_matching_selector(page, selectors) is not None

    async def _first_matching_selector(
        self, page: Any, selectors: tuple[str, ...], *, visible_only=False
    ) -> str | None:
        query_selector = getattr(page, "query_selector", None)
        if not callable(query_selector):
            return None
        for selector in selectors:
            try:
                query_all = getattr(page, "query_selector_all", None)
                if visible_only and callable(query_all):
                    elements = await query_all(selector)
                else:
                    elements = [await query_selector(selector)]
                for element in elements:
                    if element is None:
                        continue
                    is_visible = getattr(element, "is_visible", None)
                    if visible_only and callable(is_visible) and not await is_visible():
                        continue
                    return selector
            except Exception:
                continue
        return None

    def _parse_retry_after(self, value: str | None) -> float | None:
        if value is None:
            return None
        raw = value.strip()
        if not raw:
            return None
        if raw.isascii() and raw.isdecimal():
            normalized = raw.lstrip("0") or "0"
            maximum = str(int(_MAX_RETRY_AFTER_SECONDS))
            if len(normalized) > len(maximum) or (
                len(normalized) == len(maximum) and normalized > maximum
            ):
                return _MAX_RETRY_AFTER_SECONDS
            return float(int(normalized))
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        seconds = max(0.0, (parsed - self._utcnow()).total_seconds())
        return min(seconds, _MAX_RETRY_AFTER_SECONDS)

    def _cooldown_duration(
        self, retry_after: float | None, *, block_streak: int
    ) -> float:
        exponent = min(max(0, block_streak - 1), 30)
        exponential = min(
            self.settings.cooldown_seconds * (2**exponent),
            self.settings.max_cooldown_seconds,
        )
        # A valid upstream Retry-After remains authoritative even when it is
        # longer than the locally configured exponential ceiling.
        return max(exponential, retry_after or 0.0)

    async def _trip_circuit(
        self,
        *,
        reason: str,
        retry_after: float | None,
        metric: str,
        lease: _Lease,
    ) -> float:
        dispatcher: asyncio.Task[None] | None
        async with self._lock:
            now = self._clock()
            is_new_trip = self._circuit_reason is None
            is_matching_probe = (
                lease.probe_generation is not None
                and lease.probe_generation == self._circuit_generation
            )
            # A navigation admitted before another concurrent request tripped
            # the circuit is stale, not a failed half-open probe. It may extend
            # Retry-After but must not inflate the exponential streak.
            if is_new_trip or is_matching_probe:
                self._block_streak += 1
            duration = self._cooldown_duration(
                retry_after,
                block_streak=self._block_streak,
            )
            self._circuit_open_until = max(self._circuit_open_until, now + duration)
            self._circuit_reason = reason
            self._circuit_generation += 1
            self._half_open_reserved = False
            self._metrics["circuit_trips"] += 1
            self._metrics[metric] += 1
            effective_retry_after = max(1.0, self._circuit_open_until - now)
            dispatcher = self._dispatcher
            self._dispatcher = None
            self._reject_waiters_locked(OutboundCircuitOpen)
        if dispatcher is not None and dispatcher is not asyncio.current_task():
            dispatcher.cancel()
        await self._notify_state_change()
        return effective_retry_after

    def _reject_waiters_locked(
        self, exception_type: type[OutboundControlError]
    ) -> None:
        remaining = max(1.0, self._circuit_open_until - self._clock())
        while self._waiters:
            waiter = self._waiters.popleft()
            waiter.state = "rejected"
            if not waiter.future.done():
                if exception_type is OutboundCircuitOpen:
                    error = exception_type(
                        "Kleinanzeigen outbound circuit is open",
                        retry_after=remaining,
                    )
                else:
                    error = exception_type(
                        "Kleinanzeigen outbound controller is closed"
                    )
                waiter.future.set_exception(error)

    async def _close_after_probe(self, lease: _Lease) -> None:
        changed = False
        async with self._lock:
            if (
                lease.probe_generation != self._circuit_generation
                or not self._half_open_reserved
            ):
                return
            self._circuit_reason = None
            self._circuit_open_until = 0.0
            self._half_open_reserved = False
            self._block_streak = 0
            self._metrics["circuit_recoveries"] += 1
            changed = True
        if changed:
            await self._notify_state_change()

    async def _abandon_probe(self, lease: _Lease) -> None:
        changed = False
        async with self._lock:
            if lease.probe_generation != self._circuit_generation:
                return
            self._half_open_reserved = False
            self._circuit_open_until = self._clock()
            changed = True
        if changed:
            await self._notify_state_change()

    async def _reopen_failed_probe(self, lease: _Lease) -> None:
        changed = False
        async with self._lock:
            if lease.probe_generation != self._circuit_generation:
                return
            self._half_open_reserved = False
            duration = self._cooldown_duration(
                None,
                block_streak=max(1, self._block_streak),
            )
            self._circuit_open_until = self._clock() + duration
            self._circuit_reason = self._circuit_reason or "half_open_probe_failed"
            changed = True
        if changed:
            await self._notify_state_change()


__all__ = [
    "OutboundCircuitOpen",
    "OutboundControlError",
    "OutboundController",
    "OutboundControllerClosed",
    "OutboundNavigationTimeout",
    "OutboundQueueFull",
    "OutboundQueueTimeout",
    "OutboundSettings",
    "UpstreamBlocked",
    "UpstreamRateLimited",
]
