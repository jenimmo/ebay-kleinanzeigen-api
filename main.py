import os
from contextlib import asynccontextmanager
from dataclasses import replace

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from routers import (
    inserate_ultra as inserate,
    inserat,
    inserate_detailed_ultra as inserate_detailed,
    inserate_batch,
    convert_url,
    inserate_by_url,
    jobs,
)
from scrapers.inserat import DetailExtractionError, execute_detail_page
from scrapers.inserate_ultra_optimized import SearchExtractionError, execute_search_page
from utils.browser import OptimizedPlaywrightManager
from utils.asyncio_optimizations import EventLoopOptimizer
from utils.http_security import RequestBodyLimitMiddleware
from utils.job_manager import (
    DetailItemResult,
    DetailTask,
    JobManager,
    JobManagerError,
    JobSettings,
    PermanentJobError,
    SearchPageResult,
    SearchTask,
)
from utils.outbound_control import (
    OutboundControlError,
    OutboundController,
    OutboundSettings,
)

# Global browser manager instance for sharing across all endpoints
browser_manager = None
outbound_controller = None
job_manager = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle - startup and shutdown events"""
    global browser_manager, outbound_controller, job_manager

    # Validate outbound limits before allocating or configuring runtime resources.
    # Invalid environment values deliberately abort startup with a clear ValueError.
    outbound_settings = OutboundSettings.from_env()
    parsed_job_settings = JobSettings.from_env()
    legacy_retry_name = "KLEINANZEIGEN_JOB_RETRY_BUDGET"
    if (
        legacy_retry_name in os.environ
        and parsed_job_settings.retry_budget != outbound_settings.retry_budget
    ):
        raise ValueError(
            f"{legacy_retry_name} must match "
            "KLEINANZEIGEN_OUTBOUND_RETRY_BUDGET; retries use one shared budget"
        )
    # The outbound setting is authoritative for every navigation retry. The
    # JobSettings field remains an internal scheduler input and a compatibility
    # hook for standalone tests, but production cannot configure it separately.
    job_settings = replace(
        parsed_job_settings,
        retry_budget=outbound_settings.retry_budget,
    )

    # Setup uvloop for maximum performance (2-4x improvement)
    uvloop_enabled = EventLoopOptimizer.setup_uvloop()

    # Optimize event loop settings
    EventLoopOptimizer.optimize_event_loop()

    manager_holder: dict[str, JobManager] = {}

    async def persist_outbound_state(snapshot: dict) -> None:
        manager = manager_holder.get("manager")
        if manager is not None:
            await manager.save_runtime_state(snapshot)

    outbound_controller = OutboundController(
        outbound_settings,
        state_change_callback=persist_outbound_state,
    )
    browser_manager = OptimizedPlaywrightManager(
        max_contexts=max(2, job_settings.worker_count + 1),
        max_concurrent=job_settings.worker_count,
    )

    try:
        await browser_manager.start()

        async def search_executor(task: SearchTask) -> SearchPageResult:
            try:
                execution = await execute_search_page(
                    browser_manager,
                    outbound_controller,
                    search=task.search,
                    page_number=task.page_number,
                    is_retry=task.is_retry,
                    page_setup_timeout_seconds=task.page_setup_timeout_seconds,
                    extraction_timeout_seconds=task.extraction_timeout_seconds,
                    cleanup_timeout_seconds=task.cleanup_timeout_seconds,
                )
            except SearchExtractionError as exc:
                raise PermanentJobError(
                    "Search result page could not be extracted completely",
                    error_code="listing_extraction_error",
                ) from exc
            return SearchPageResult(
                listings=execution.listings,
                total_results=execution.total_results,
                total_pages=execution.total_pages,
                page_size=execution.page_size,
                has_next_page=execution.has_next_page,
            )

        async def detail_executor(task: DetailTask) -> DetailItemResult:
            try:
                execution = await execute_detail_page(
                    browser_manager,
                    outbound_controller,
                    listing_id=task.listing_id,
                    is_retry=task.is_retry,
                    page_setup_timeout_seconds=task.page_setup_timeout_seconds,
                    extraction_timeout_seconds=task.extraction_timeout_seconds,
                    cleanup_timeout_seconds=task.cleanup_timeout_seconds,
                )
            except DetailExtractionError as exc:
                raise PermanentJobError(
                    "Detail page could not be extracted completely",
                    error_code="detail_extraction_error",
                ) from exc
            return DetailItemResult(status=execution.status, data=execution.data)

        job_manager = JobManager(
            job_settings,
            search_executor=search_executor,
            detail_executor=detail_executor,
        )
        manager_holder["manager"] = job_manager
        await job_manager.start(start_workers=False)
        runtime = await job_manager.load_controller_snapshot()
        await outbound_controller.restore_circuit_snapshot(runtime)
        await job_manager.start_workers()

        app.state.outbound_controller = outbound_controller
        app.state.browser_manager = browser_manager
        app.state.job_manager = job_manager
        app.state.uvloop_enabled = uvloop_enabled
        yield
    finally:
        # Stop durable workers before tearing down their shared browser state.
        try:
            if job_manager is not None:
                await job_manager.close()
        finally:
            try:
                await outbound_controller.close()
            finally:
                try:
                    await browser_manager.close()
                finally:
                    app.state.outbound_controller = None
                    app.state.browser_manager = None
                    app.state.job_manager = None
                    outbound_controller = None
                    browser_manager = None
                    job_manager = None


app = FastAPI(version="1.0.0", lifespan=lifespan)
app.add_middleware(RequestBodyLimitMiddleware, max_body_size=64 * 1024)


ACTIVE_ENDPOINTS = [
    "/health",
    "/outbound-status",
    "/jobs/search",
    "/jobs/details",
    "/jobs/{job_id}",
    "/jobs/{job_id}/results",
    "/jobs/{job_id}/continue",
    "/jobs/{job_id}/resume",
    "/inserate",
    "/inserat/{id}",
    "/inserate-detailed",
    "/inserate/batch",
    "/inserate-by-url",
    "/convert-url",
]


@app.exception_handler(OutboundControlError)
async def outbound_control_exception_handler(
    _request: Request, exc: OutboundControlError
) -> JSONResponse:
    """Map admission and upstream-block errors without leaking request data."""

    headers = {}
    if exc.retry_after_seconds is not None:
        headers["Retry-After"] = str(exc.retry_after_seconds)
    return JSONResponse(
        status_code=exc.http_status,
        headers=headers,
        content={
            "detail": {
                "error": exc.error_code,
                "message": str(exc),
                "retry_after_seconds": exc.retry_after_seconds,
            }
        },
    )


@app.exception_handler(JobManagerError)
async def job_manager_exception_handler(
    _request: Request, exc: JobManagerError
) -> JSONResponse:
    """Map storage/readiness failures outside the dedicated job router."""

    retry_after = getattr(exc, "retry_after_seconds", None)
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return JSONResponse(
        status_code=exc.http_status,
        headers=headers,
        content={
            "detail": {
                "error": exc.error_code,
                "message": str(exc),
            }
        },
    )


def _browser_is_ready(request: Request) -> bool:
    """Return whether the shared Playwright browser can accept requests."""
    manager = getattr(request.app.state, "browser_manager", None)
    browser = getattr(manager, "_browser", None)
    if browser is None:
        return False

    is_connected = getattr(browser, "is_connected", None)
    if not callable(is_connected):
        return True

    try:
        return bool(is_connected())
    except Exception:
        return False


async def _job_manager_is_ready(request: Request) -> bool:
    """Verify that durable storage and the configured scheduler pool are live."""

    manager = getattr(request.app.state, "job_manager", None)
    if manager is None:
        return False
    try:
        snapshot = await manager.status()
        workers = snapshot["workers"]
        return bool(
            workers["configured"] >= 1 and workers["running"] == workers["configured"]
        )
    except Exception:
        return False


@app.get("/")
async def root(request: Request):
    return {
        "message": "Welcome to the Kleinanzeigen API",
        "endpoints": ACTIVE_ENDPOINTS,
        "status": "operational",
        "browser_status": "optimized" if _browser_is_ready(request) else "unavailable",
    }


@app.get("/health")
async def health(request: Request):
    """Readiness check for load balancers and uptime monitors."""
    browser_ready = _browser_is_ready(request)
    job_manager_ready = await _job_manager_is_ready(request)
    if not browser_ready or not job_manager_ready:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "unavailable",
                "browser_status": "optimized" if browser_ready else "unavailable",
                "job_manager_status": ("ready" if job_manager_ready else "unavailable"),
            },
        )

    return {"status": "healthy", "browser_status": "optimized"}


@app.get("/outbound-status")
async def outbound_status(request: Request):
    """Expose non-sensitive admission, circuit, and durable queue metrics."""

    controller = getattr(request.app.state, "outbound_controller", None)
    if controller is None:
        raise HTTPException(status_code=503, detail="Outbound controller unavailable")
    snapshot = await controller.status()
    manager = getattr(request.app.state, "job_manager", None)
    if manager is not None:
        snapshot["jobs"] = await manager.status()
    return snapshot


app.include_router(inserate.router)
app.include_router(inserat.router)
app.include_router(inserate_detailed.router)
app.include_router(inserate_batch.router)
app.include_router(convert_url.router)
app.include_router(inserate_by_url.router)
app.include_router(jobs.router)
