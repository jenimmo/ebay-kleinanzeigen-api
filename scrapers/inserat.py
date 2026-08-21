import asyncio
from dataclasses import dataclass
from fastapi import HTTPException
from libs.websites import kleinanzeigen as lib
import math
import re
import sys
import time
from datetime import datetime, timezone
from utils.browser import OptimizedPlaywrightManager, close_page_and_release_context
from utils.performance import PageMetrics
from utils.error_handling import (
    WarningManager,
    ErrorLogger,
    ErrorSeverity,
    error_handling_context,
)
from utils.input_validation import normalize_listing_id, sanitize_log_value
from utils.url_security import install_safe_page_routing
from utils.outbound_control import (
    OutboundControlError,
    OutboundController,
    OutboundNavigationTimeout,
)

_AD_URL_PREFIX = "https://www.kleinanzeigen.de/s-anzeige/"


class DetailExtractionError(RuntimeError):
    """A detail page loaded but could not be represented reliably."""


@dataclass(frozen=True, slots=True)
class DetailPageExecution:
    """Framework-neutral result of one persisted detail work item."""

    status: str
    data: dict


def _deleted_response(url_requested: str, url_redirected: str) -> dict:
    return {
        "id": None,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "url_requested": url_requested,
        "url_redirected": url_redirected,
        "not_found": True,
        "status": "deleted",
        "categories": [],
        "title": None,
        "price": {"amount": "0", "currency": "€", "negotiable": False},
        "delivery": None,
        "location": {"zip": "", "city": "", "state": ""},
        "media": {
            "images": {"count": 0, "urls": []},
            "videos": {"count": 0, "urls": []},
            "audios": {"count": 0, "urls": []},
        },
        "details": {},
        "features": {},
        "description": None,
        "seller": {
            "name": None,
            "user_id": None,
            "since": None,
            "type": "private",
            "badges": [],
        },
        "extra_info": {"created_at": None, "views": "0"},
    }


async def _extract_inserat_details_from_page(url: str, page) -> dict:
    """Extract one already navigated detail page without owning resources."""

    final_url = page.url
    if not final_url.startswith(_AD_URL_PREFIX):
        return _deleted_response(url, final_url)
    if await page.query_selector("#srchrslt-adexpired"):
        return _deleted_response(url, final_url)

    try:
        await page.wait_for_selector("#viewad-cntr-num", state="visible", timeout=2500)
    except Exception as exc:
        print(f"[WARNING] Views element did not appear within 5 seconds: {exc}")

    ad_id = await lib.get_element_content(
        page,
        "#viewad-ad-id-box > ul > li:nth-child(2)",
        default="[ERROR] Ad ID not found",
    )
    categories = [
        category.strip()
        for category in await lib.get_elements_content(page, ".breadcrump-link")
        if category.strip()
    ]
    title = await lib.get_element_content(
        page, "#viewad-title", default="[ERROR] Title not found"
    )

    status = "active"
    title_element = await page.query_selector("#viewad-title")
    if title_element:
        title_text = await title_element.inner_text()
        if "Verkauft" in title_text:
            status = "sold"
        elif "Reserviert •" in title_text:
            status = "reserved"
        elif "Gelöscht •" in title_text:
            status = "deleted"
        title_classes = await title_element.get_attribute("class")
        if title_classes and "is-sold" in title_classes:
            status = "sold"
    if await page.query_selector(".badge-sold"):
        status = "sold"

    price = lib.parse_price(await lib.get_element_content(page, "#viewad-price"))
    description = await lib.get_element_content(page, "#viewad-description-text")
    if description:
        description = re.sub(r"[ \t]+", " ", description).strip()
        description = re.sub(r"\n+", "\n", description)

    images = await lib.get_image_sources(page, ".galleryimage-element img")
    seller_details = await lib.get_seller_details(page)
    details = (
        await lib.get_details(page)
        if await page.query_selector("#viewad-details")
        else {}
    )
    features = (
        await lib.get_features(page)
        if await page.query_selector("#viewad-configuration")
        else {}
    )
    shipping_text = await lib.get_element_content(
        page, ".boxedarticle--details--shipping"
    )
    shipping = None
    if shipping_text:
        if "Nur Abholung" in shipping_text:
            shipping = "pickup"
        elif "Versand" in shipping_text:
            shipping = "shipping"

    return {
        "id": ad_id,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "url_requested": url,
        "url_redirected": final_url,
        "categories": categories,
        "title": title.split(" • ")[-1].strip() if " • " in title else title.strip(),
        "status": status,
        "price": price,
        "delivery": shipping,
        "location": await lib.get_location(page),
        "media": {
            "images": {"count": len(images), "urls": images},
            "videos": {"count": 0, "urls": []},
            "audios": {"count": 0, "urls": []},
        },
        "details": details,
        "features": features,
        "description": description,
        "seller": seller_details,
        "extra_info": await lib.get_extra_info(page),
    }


async def get_inserate_details(
    url: str,
    page_factory,
    outbound_controller: OutboundController,
    *,
    is_retry: bool = False,
):
    try:
        page, _ = await outbound_controller.navigate_with_page(
            page_factory,
            url,
            is_retry=is_retry,
            timeout_ms=120_000,
        )
        return await _extract_inserat_details_from_page(url, page)
    except OutboundControlError:
        raise
    except Exception as e:
        print(f"[ERROR] {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


async def execute_detail_page(
    browser_manager: OptimizedPlaywrightManager,
    outbound_controller: OutboundController,
    *,
    listing_id: str,
    is_retry: bool = False,
    page_setup_timeout_seconds: float = 30.0,
    extraction_timeout_seconds: float = 30.0,
    cleanup_timeout_seconds: float = 30.0,
) -> DetailPageExecution:
    """Execute exactly one persisted detail work item under one outbound lease."""

    listing_id = normalize_listing_id(listing_id)
    for name, value in (
        ("page_setup_timeout_seconds", page_setup_timeout_seconds),
        ("extraction_timeout_seconds", extraction_timeout_seconds),
        ("cleanup_timeout_seconds", cleanup_timeout_seconds),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise ValueError(f"{name} must be a positive number")

    url = f"https://www.kleinanzeigen.de/s-anzeige/{listing_id}"
    context = None
    page = None

    async def create_page():
        nonlocal context, page
        async with asyncio.timeout(page_setup_timeout_seconds):
            context = await browser_manager.get_context()
            page = await context.new_page()
            await install_safe_page_routing(page)
        return page

    async def extract(navigated_page, response) -> DetailPageExecution:
        async with asyncio.timeout(extraction_timeout_seconds):
            if getattr(response, "status", None) == 404:
                return DetailPageExecution(
                    status="not_found",
                    data=_deleted_response(url, navigated_page.url),
                )
            status = getattr(response, "status", None)
            if isinstance(status, int) and status >= 500:
                raise RuntimeError("detail page returned a transient server error")
            details = await _extract_inserat_details_from_page(url, navigated_page)
            if details.get("not_found"):
                return DetailPageExecution(status="not_found", data=details)
            extracted_id = details.get("id")
            if not extracted_id or str(extracted_id).startswith("[ERROR]"):
                raise DetailExtractionError(
                    "detail page did not expose a reliable listing ID"
                )
            return DetailPageExecution(status="complete", data=details)

    async def cleanup(_navigated_page) -> None:
        await close_page_and_release_context(
            page,
            context,
            browser_manager,
            timeout_seconds=cleanup_timeout_seconds,
        )

    return await outbound_controller.run_navigation(
        create_page,
        url,
        extract,
        cleanup=cleanup,
        is_retry=is_retry,
        wait_until="domcontentloaded",
        wait_for_circuit=False,
    )


async def get_inserate_details_optimized(
    browser_manager: OptimizedPlaywrightManager,
    outbound_controller: OutboundController,
    listing_id: str,
    retry_count: int | None = None,
    request_id: str = None,
    progress: str = None,
) -> dict:
    """
    Optimized version of get_inserate_details with comprehensive error handling and performance tracking.

    Args:
        browser_manager: OptimizedPlaywrightManager instance
        listing_id: The listing ID to fetch details for
        retry_count: Optional caller cap; the shared controller budget is authoritative

    Returns:
        Dictionary containing listing details, performance metrics, and warnings
    """
    from utils.performance import PerformanceTracker

    # This scraper is called by multiple routers. Keep the security boundary at
    # the shared sink as well as at the public request models.
    listing_id = normalize_listing_id(listing_id)
    log_listing_id = sanitize_log_value(listing_id, max_length=42)
    log_request_id = sanitize_log_value(request_id, max_length=64) if request_id else ""
    log_progress = sanitize_log_value(progress, max_length=16) if progress else ""

    # Initialize error handling and performance tracking
    logger = ErrorLogger("inserat_scraper")
    warning_manager = WarningManager()
    tracker = PerformanceTracker()
    tracker.start_request()

    url = f"https://www.kleinanzeigen.de/s-anzeige/{listing_id}"
    _rid = f" {log_request_id}" if log_request_id else ""
    _pos = f" {log_progress}" if log_progress else ""
    logger.logger.info(
        "[DETAIL%s%s] Fetching ad %s: %s",
        _rid,
        _pos,
        log_listing_id,
        url,
    )
    effective_retry_count = outbound_controller.bounded_retry_count(retry_count)

    with error_handling_context(
        operation="fetch_listing_details", listing_id=listing_id, url=url, logger=logger
    ) as error_ctx:
        last_structured_error = None

        for attempt in range(effective_retry_count + 1):
            start_time = time.time()

            try:
                # Use semaphore-controlled execution
                async def fetch_operation():
                    context = None
                    page = None
                    try:

                        async def create_page():
                            nonlocal context, page
                            context = await browser_manager.get_context()
                            page = await context.new_page()
                            return page

                        # Get listing details using existing function
                        details = await get_inserate_details(
                            url,
                            create_page,
                            outbound_controller,
                            is_retry=attempt > 0,
                        )

                        # Ad is gone (redirect or expired page detected)
                        if details.get("not_found"):
                            return details

                        # Validate the extracted details
                        if not details or not details.get("id"):
                            warning_manager.add_warning(
                                f"Incomplete data extracted for listing {log_listing_id}",
                                ErrorSeverity.MEDIUM,
                                error_ctx.context,
                                affected_items=[log_listing_id],
                                impact_description="Some listing information may be missing",
                            )

                        # Check for status-related warnings
                        status = details.get("status", "unknown")
                        if status in ["sold", "deleted"]:
                            warning_manager.add_warning(
                                f"Listing {log_listing_id} has status: {status}",
                                ErrorSeverity.LOW,
                                error_ctx.context,
                                affected_items=[log_listing_id],
                                impact_description=f"Listing is no longer available ({status})",
                            )

                        return details

                    finally:
                        await close_page_and_release_context(
                            page,
                            context,
                            browser_manager,
                            suppress_errors=sys.exception() is not None,
                        )

                # The process-global outbound controller is the first admission
                # boundary; browser contexts are allocated only after admission.
                details = await fetch_operation()

                # Ad was not found (redirect or expired page): return not-found response immediately
                if details.get("not_found"):
                    browser_metrics = browser_manager.get_performance_metrics()
                    tracker.set_browser_contexts_used(
                        browser_metrics["contexts_in_use"]
                        + browser_metrics["contexts_in_pool"]
                    )
                    tracker.set_concurrent_level(1)
                    request_metrics = tracker.get_request_metrics()
                    return {
                        "success": False,
                        "not_found": True,
                        "data": details,
                        "time_taken": round(request_metrics.total_time, 3),
                        "performance_metrics": request_metrics.to_dict(),
                        "browser_metrics": browser_metrics,
                    }

                # Create successful page metric with warning information
                page_metric = PageMetrics(
                    page_number=1,
                    url=url,
                    start_time=start_time,
                    end_time=time.time(),
                    success=True,
                    retry_count=attempt,
                    error_message=None,
                    results_count=1,
                    warning_count=len(warning_manager.get_warnings()),
                )
                tracker.add_page_metric(page_metric)

                # Get browser performance metrics
                browser_metrics = browser_manager.get_performance_metrics()
                tracker.set_browser_contexts_used(
                    browser_metrics["contexts_in_use"]
                    + browser_metrics["contexts_in_pool"]
                )
                tracker.set_concurrent_level(1)  # Single listing request

                # Generate final metrics
                request_metrics = tracker.get_request_metrics()

                # Log successful operation
                if attempt > 0:
                    logger.log_operation_summary(
                        operation=f"fetch_details_{log_listing_id}",
                        total_items=1,
                        successful_items=1,
                        warnings=warning_manager.get_warnings(),
                        errors=[],
                        duration=request_metrics.total_time,
                    )

                # Prepare response with comprehensive information
                response = {
                    "success": True,
                    "data": details,
                    "time_taken": round(request_metrics.total_time, 3),
                    "performance_metrics": request_metrics.to_dict(),
                    "browser_metrics": browser_metrics,
                }

                # Add warning information if present
                warnings = warning_manager.get_warnings()
                if warnings:
                    response["warnings"] = warning_manager.get_user_friendly_messages()
                    response["detailed_warnings"] = [w.to_dict() for w in warnings]
                    response["warning_summary"] = warning_manager.get_warning_summary()

                return response

            except OutboundNavigationTimeout:
                if attempt < effective_retry_count:
                    await outbound_controller.wait_before_retry(attempt)
                    continue
                raise
            except OutboundControlError:
                raise
            except Exception as e:
                # Classify and handle the error
                error_ctx.context.retry_attempt = attempt
                structured_error = error_ctx.handle_exception(e, "detail_fetch")
                last_structured_error = structured_error

                # Check if we should retry based on error classification
                if attempt < effective_retry_count and structured_error.should_retry(
                    effective_retry_count
                ):
                    # Add warning about retry attempt
                    warning_manager.add_warning(
                        f"Retrying listing {log_listing_id} after {structured_error.category.value} error (attempt {attempt + 1}/{effective_retry_count + 1})",
                        ErrorSeverity.MEDIUM,
                        error_ctx.context,
                        affected_items=[log_listing_id],
                        impact_description=f"Temporary delay before retry due to {structured_error.category.value} error",
                    )

                    await outbound_controller.wait_before_retry(attempt)
                    continue

                # All retries exhausted or non-recoverable error
                error_msg = (
                    f"Failed after {attempt + 1} attempts: {structured_error.message}"
                )

                # Create failed page metric with enhanced information
                page_metric = PageMetrics(
                    page_number=1,
                    url=url,
                    start_time=start_time,
                    end_time=time.time(),
                    success=False,
                    retry_count=attempt,
                    error_message=error_msg,
                    results_count=0,
                    error_category=structured_error.category.value,
                    warning_count=len(warning_manager.get_warnings()),
                )
                tracker.add_page_metric(page_metric)

                # Try to get partial metrics if available
                try:
                    browser_metrics = browser_manager.get_performance_metrics()
                    tracker.set_browser_contexts_used(
                        browser_metrics["contexts_in_use"]
                        + browser_metrics["contexts_in_pool"]
                    )
                    tracker.set_concurrent_level(1)

                    request_metrics = tracker.get_request_metrics()

                    # Log failed operation
                    logger.log_operation_summary(
                        operation=f"fetch_details_{log_listing_id}",
                        total_items=1,
                        successful_items=0,
                        warnings=warning_manager.get_warnings(),
                        errors=[structured_error],
                        duration=request_metrics.total_time,
                    )

                    # Prepare comprehensive error response
                    response = {
                        "success": False,
                        "error": structured_error.message,
                        "error_category": structured_error.category.value,
                        "error_severity": structured_error.severity.value,
                        "recovery_suggestions": structured_error.recovery_suggestions,
                        "data": None,
                        "time_taken": round(request_metrics.total_time, 3),
                        "performance_metrics": request_metrics.to_dict(),
                        "browser_metrics": browser_metrics,
                    }

                    # Add warning information if present
                    warnings = warning_manager.get_warnings()
                    if warnings:
                        response["warnings"] = (
                            warning_manager.get_user_friendly_messages()
                        )
                        response["detailed_warnings"] = [w.to_dict() for w in warnings]

                    return response

                except Exception as e:
                    # Fallback response if metrics collection also fails
                    raise HTTPException(
                        status_code=500,
                        detail={
                            "error": structured_error.message,
                            "category": structured_error.category.value,
                            "severity": structured_error.severity.value,
                            "recovery_suggestions": structured_error.recovery_suggestions,
                        },
                    ) from e

        # This should never be reached, but just in case
        if last_structured_error:
            error_msg = f"Final error: {last_structured_error.message}"
            raise HTTPException(
                status_code=500,
                detail={
                    "error": error_msg,
                    "category": last_structured_error.category.value,
                    "severity": last_structured_error.severity.value,
                    "recovery_suggestions": last_structured_error.recovery_suggestions,
                },
            )
        else:
            raise HTTPException(
                status_code=500, detail="Unexpected error in retry loop"
            )
