import asyncio
import logging
import math
from typing import List
from playwright.async_api import async_playwright, BrowserContext, Page
from utils.user_agent import get_random_ua


logger = logging.getLogger(__name__)


async def _finish_cleanup_task(
    cleanup_task: asyncio.Task[BaseException | None], *, suppress_errors: bool
) -> None:
    pending_cancellation: asyncio.CancelledError | None = None
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as exc:
            pending_cancellation = pending_cancellation or exc

    cleanup_error = cleanup_task.result()
    if pending_cancellation is not None:
        raise pending_cancellation
    if cleanup_error is not None:
        if not suppress_errors:
            raise cleanup_error
        logger.warning("Browser resource cleanup reported an error")


async def close_page_and_release_context(
    page,
    context,
    browser_manager,
    *,
    suppress_errors: bool = False,
    timeout_seconds: float | None = None,
) -> None:
    """Clean scraper resources without masking a primary error or cancellation."""

    if timeout_seconds is not None and (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a finite positive number")

    async def cleanup() -> BaseException | None:
        cleanup_error: BaseException | None = None
        if context is not None and timeout_seconds is not None:
            # The optimized manager owns every page in the context. Let its
            # bounded release close the pages and, on timeout, discard the
            # unusable context while returning the pool slot deterministically.
            try:
                await browser_manager.release_context(
                    context, timeout_seconds=float(timeout_seconds)
                )
            except (asyncio.CancelledError, Exception) as exc:
                cleanup_error = exc
            return cleanup_error

        if page is not None:
            try:
                await page.close()
            except (asyncio.CancelledError, Exception) as exc:
                cleanup_error = exc

        if context is not None:
            try:
                await browser_manager.release_context(context)
            except (asyncio.CancelledError, Exception) as exc:
                cleanup_error = cleanup_error or exc
        return cleanup_error

    await _finish_cleanup_task(
        asyncio.create_task(cleanup()), suppress_errors=suppress_errors
    )


async def close_playwright_managed_page(
    page, browser_manager, *, suppress_errors: bool = False
) -> None:
    """Close a legacy manager page while preserving an in-flight primary error."""

    async def cleanup() -> BaseException | None:
        try:
            await browser_manager.close_page(page)
        except (asyncio.CancelledError, Exception) as exc:
            return exc
        return None

    await _finish_cleanup_task(
        asyncio.create_task(cleanup()), suppress_errors=suppress_errors
    )


class PlaywrightManager:
    def __init__(self):
        self._playwright = None
        self._browser = None

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)

    async def new_context_page(self):
        context = await self._browser.new_context(
            user_agent=get_random_ua(), service_workers="block"
        )
        try:
            page = await context.new_page()
        except BaseException:
            await context.close()
            raise
        page._context_ref = context
        return page

    async def close_page(self, page):
        context = getattr(page, "_context_ref", None)
        try:
            await page.close()
        finally:
            if context:
                await context.close()

    async def close(self):
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            logger.warning("Browser was already unavailable during shutdown")
        finally:
            self._browser = None

        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            logger.warning("Playwright driver was already unavailable during shutdown")
        finally:
            self._playwright = None


class OptimizedPlaywrightManager:
    def __init__(
        self,
        max_contexts: int = 10,
        max_concurrent: int = 5,
        context_acquire_timeout: float = 30.0,
    ):
        if max_contexts < 1:
            raise ValueError("max_contexts must be at least 1")
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        if context_acquire_timeout <= 0:
            raise ValueError("context_acquire_timeout must be greater than 0")

        self._playwright = None
        self._browser = None
        self._context_pool: List[BrowserContext] = []
        self._context_in_use: List[BrowserContext] = []
        self._max_contexts = max_contexts
        self._max_concurrent = max_concurrent
        self._context_acquire_timeout = context_acquire_timeout
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._context_slots = asyncio.BoundedSemaphore(max_contexts)
        self._context_lock = asyncio.Lock()

        # Performance metrics
        self._contexts_created = 0
        self._contexts_reused = 0
        self._concurrent_operations = 0
        self._max_concurrent_reached = 0

    async def start(self):
        """Initialize the browser and create initial context pool"""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)

        # Pre-create some contexts for the pool
        initial_contexts = min(3, self._max_contexts)
        for _ in range(initial_contexts):
            context = await self._browser.new_context(
                user_agent=get_random_ua(), service_workers="block"
            )
            self._context_pool.append(context)
            self._contexts_created += 1

    async def get_context(self) -> BrowserContext:
        """Get a context, waiting for a globally bounded slot if necessary."""
        try:
            await asyncio.wait_for(
                self._context_slots.acquire(),
                timeout=self._context_acquire_timeout,
            )
        except TimeoutError as exc:
            raise TimeoutError(
                "Timed out waiting for an available browser context "
                f"after {self._context_acquire_timeout:g} seconds"
            ) from exc

        context = None
        try:
            async with self._context_lock:
                if self._context_pool:
                    context = self._context_pool.pop()
                    self._context_in_use.append(context)
                    self._contexts_reused += 1
                    return context

            if self._browser is None:
                raise RuntimeError("Browser manager has not been started")

            context = await self._browser.new_context(
                user_agent=get_random_ua(), service_workers="block"
            )
            async with self._context_lock:
                self._context_in_use.append(context)
                self._contexts_created += 1
            return context
        except BaseException:
            # A failed or cancelled creation must never consume a pool slot.
            if context is not None:
                try:
                    await asyncio.shield(context.close())
                except BaseException:
                    pass
            self._context_slots.release()
            raise

    async def release_context(
        self,
        context: BrowserContext,
        *,
        timeout_seconds: float | None = None,
    ):
        """Return a context to the pool, even if the caller is cancelled.

        A configured timeout force-discards an unresponsive context and returns
        its capacity slot. External cancellation still waits for that bounded
        cleanup before being propagated.
        """

        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a finite positive number")

        cleanup_task = asyncio.create_task(self._release_context(context))
        pending_cancellation: asyncio.CancelledError | None = None
        deadline = (
            asyncio.get_running_loop().time() + float(timeout_seconds)
            if timeout_seconds is not None
            else None
        )
        timed_out = False
        while not cleanup_task.done():
            try:
                if deadline is None:
                    await asyncio.shield(cleanup_task)
                else:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        timed_out = True
                        break
                    async with asyncio.timeout(remaining):
                        await asyncio.shield(cleanup_task)
            except TimeoutError:
                timed_out = True
                break
            except asyncio.CancelledError as exc:
                # Repeated task.cancel() calls must not cancel the cleanup task
                # or leak the bounded context slot.
                pending_cancellation = pending_cancellation or exc

        if timed_out:

            async def force_discard() -> None:
                cleanup_task.cancel()
                await asyncio.gather(cleanup_task, return_exceptions=True)
                await self._discard_context_slot(context)

            finalizer = asyncio.create_task(force_discard())
            while not finalizer.done():
                try:
                    await asyncio.shield(finalizer)
                except asyncio.CancelledError as exc:
                    # A second cancellation during timeout recovery must not
                    # strand either the context semaphore or outbound cleanup.
                    pending_cancellation = pending_cancellation or exc
            finalizer.result()
            if pending_cancellation is not None:
                raise pending_cancellation
            raise TimeoutError("Timed out cleaning browser context")

        if pending_cancellation is not None:
            try:
                cleanup_task.result()
            except BaseException:
                logger.warning("Context cleanup reported an error during cancellation")
            raise pending_cancellation
        cleanup_task.result()

    async def _discard_context_slot(self, context: BrowserContext) -> None:
        """Forget a timed-out context and release its slot exactly once."""

        release_slot = False
        async with self._context_lock:
            if context in self._context_in_use:
                self._context_in_use.remove(context)
                release_slot = True
            if context in self._context_pool:
                self._context_pool.remove(context)
        if release_slot:
            self._context_slots.release()

    async def _release_context(self, context: BrowserContext):
        async with self._context_lock:
            if context not in self._context_in_use:
                return
            self._context_in_use.remove(context)

        try:
            # Clean outside the state lock. The capacity slot stays occupied until
            # the context is safe to reuse or has been closed.
            for page in list(context.pages):
                await page.close()
            await context.clear_cookies()

            reuse_context = False
            async with self._context_lock:
                if len(self._context_pool) < self._max_contexts // 2:
                    self._context_pool.append(context)
                    reuse_context = True

            if not reuse_context:
                await context.close()
        except asyncio.CancelledError:
            # A bounded release intentionally abandons a driver call that did
            # not finish. The context has already been removed from reuse and
            # the capacity slot is returned in ``finally``.
            raise
        except BaseException:
            try:
                await asyncio.shield(context.close())
            except BaseException:
                pass
            raise
        finally:
            self._context_slots.release()

    async def execute_with_semaphore(self, coro):
        """Execute a coroutine with concurrency control"""
        async with self._semaphore:
            self._concurrent_operations += 1
            self._max_concurrent_reached = max(
                self._max_concurrent_reached, self._concurrent_operations
            )
            try:
                result = await coro
                return result
            finally:
                self._concurrent_operations -= 1

    async def new_context_page(self) -> Page:
        """Create a new page using context pooling (backward compatibility)"""
        context = await self.get_context()
        try:
            page = await context.new_page()
            # Store context reference on page for cleanup
            page._context_ref = context
            return page
        except BaseException:
            await self.release_context(context)
            raise

    async def close_page(self, page: Page):
        """Close a page and return its context to the pool"""
        context = getattr(page, "_context_ref", None)
        try:
            await page.close()
        finally:
            if context:
                await self.release_context(context)

    @property
    def max_concurrent(self) -> int:
        """Configured operation limit, independent of semaphore runtime state."""
        return self._max_concurrent

    def get_performance_metrics(self) -> dict:
        """Get current performance metrics"""
        return {
            "contexts_created": self._contexts_created,
            "contexts_reused": self._contexts_reused,
            "contexts_in_pool": len(self._context_pool),
            "contexts_in_use": len(self._context_in_use),
            "max_contexts": self._max_contexts,
            "max_concurrent_reached": self._max_concurrent_reached,
            "current_concurrent": self._concurrent_operations,
            "reuse_ratio": self._contexts_reused / max(self._contexts_created, 1),
        }

    async def close(self):
        """Clean up all resources without failing app shutdown on a dead driver."""
        contexts = []
        for context in [*self._context_pool, *self._context_in_use]:
            if not any(existing is context for existing in contexts):
                contexts.append(context)
        self._context_pool.clear()
        self._context_in_use.clear()

        for context in contexts:
            try:
                await context.close()
            except Exception:
                logger.warning(
                    "Browser context was already unavailable during shutdown"
                )

        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            logger.warning("Browser was already unavailable during shutdown")
        finally:
            self._browser = None

        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            logger.warning("Playwright driver was already unavailable during shutdown")
        finally:
            self._playwright = None
