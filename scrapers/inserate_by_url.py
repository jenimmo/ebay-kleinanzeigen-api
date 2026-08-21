"""
URL-passthrough scraper: takes a full Kleinanzeigen URL and injects page numbers.
Reuses UltraOptimizedScraper for fetching/extraction; only the URL-building differs.
"""

import asyncio
import gc
import logging
import math
import re
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

from fastapi import HTTPException

from utils.browser import OptimizedPlaywrightManager
from utils.performance import PerformanceTracker
from utils.outbound_control import OutboundController
from utils.url_security import (
    MAX_SCRAPE_PAGES,
    UnsafeOutboundUrlError,
    validate_kleinanzeigen_url_syntax,
    validate_public_https_url,
)
from scrapers.inserate_ultra_optimized import (
    create_ultra_optimized_scraper,
    _page_has_old_listings,
    _filter_by_min_publish_date,
)

_TOTAL_RESULTS_SELECTOR = {"breadcrump_summary": ".breadcrump-summary"}
SCRAPE_TOTAL_TIMEOUT_SECONDS = 180.0


def inject_page(url: str, page_num: int) -> str:
    """
    Strip any existing seite/s-seite segment and inject the requested page number.

    Category URLs: seite:N is inserted immediately before the filter segment
    (the segment matching k?\\d*c\\d+), preserving any extra path components
    (e.g. anzeige:angebote, preis::N) that appear before it:
      /s-autos/anzeige:angebote/preis::15000/seite:2/c216+...
    Generic search URLs (no filter segment): s-seite:N appended before the query string.
    """
    from urllib.parse import urlparse, urlunparse, unquote

    parsed = urlparse(url)
    path = unquote(parsed.path)

    # Strip any existing page segment
    segments = [
        s
        for s in path.strip("/").split("/")
        if s and not re.match(r"^s-seite:\d+$", s) and not re.match(r"^seite:\d+$", s)
    ]

    if page_num > 1:
        filter_idx = next(
            (i for i, s in enumerate(segments) if re.match(r"^k?\d*c\d+", s)),
            None,
        )
        if filter_idx is not None:
            # Insert seite:N directly before the filter segment
            segments.insert(filter_idx, f"seite:{page_num}")
        else:
            # Generic search: append s-seite:N before query string
            segments.append(f"s-seite:{page_num}")

    new_path = "/" + "/".join(segments)
    return urlunparse(parsed._replace(path=new_path))


def _parse_breadcrumb(breadcrump_text: str) -> Tuple[Optional[int], Optional[int]]:
    """Parse the breadcrumb summary into (total_results, actual_page_count).

    Kleinanzeigen renders e.g. 'Autos 1 - 25 von 48 Gebrauchtwagen...'
    Page size is only unambiguous on page 1 (range always starts at 1), so
    page_count is only computed then; otherwise returns None for page_count.
    Returns (None, None) if the text cannot be parsed at all.
    """
    match = re.search(r"(\d[\d.]*)\s*-\s*(\d[\d.]*)\s+von\s+([\d.]+)", breadcrump_text)
    if not match:
        return None, None
    page_start = int(match.group(1).replace(".", ""))
    page_end = int(match.group(2).replace(".", ""))
    total = int(match.group(3).replace(".", ""))
    if page_start != 1:
        # Can't derive page size from a partial last-page range
        return total, None
    page_size = page_end  # page_end - 1 + 1
    if page_size <= 0:
        return total, None
    return total, math.ceil(total / page_size)


async def scrape_by_url(
    browser_manager: OptimizedPlaywrightManager,
    outbound_controller: OutboundController,
    base_url: str,
    max_pages: int = 1,
    min_publish_date: datetime = None,
) -> Dict[str, Any]:
    """Validate and scrape a Kleinanzeigen URL within a fixed request budget."""
    if not isinstance(max_pages, int) or not 1 <= max_pages <= MAX_SCRAPE_PAGES:
        raise HTTPException(
            status_code=400,
            detail=f"max_pages must be between 1 and {MAX_SCRAPE_PAGES}",
        )

    try:
        validate_kleinanzeigen_url_syntax(base_url)
    except UnsafeOutboundUrlError as exc:
        raise HTTPException(status_code=400, detail="URL is not allowed") from exc

    try:
        async with asyncio.timeout(SCRAPE_TOTAL_TIMEOUT_SECONDS):
            # Resolve once before acquiring scarce browser capacity. The page-level
            # route guard resolves again immediately before Chromium sends the
            # navigation and every subsequent redirect or subresource request.
            await validate_public_https_url(base_url, require_kleinanzeigen_host=True)
            return await _scrape_by_url_within_budget(
                browser_manager=browser_manager,
                outbound_controller=outbound_controller,
                base_url=base_url,
                max_pages=max_pages,
                min_publish_date=min_publish_date,
            )
    except UnsafeOutboundUrlError as exc:
        raise HTTPException(
            status_code=400, detail="URL could not be safely resolved"
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504, detail="Scraping time budget exceeded"
        ) from exc


async def _scrape_by_url_within_budget(
    browser_manager: OptimizedPlaywrightManager,
    outbound_controller: OutboundController,
    base_url: str,
    max_pages: int,
    min_publish_date: datetime = None,
) -> Dict[str, Any]:
    """Scrape up to max_pages pages starting from base_url.

    If min_publish_date is set, stops fetching once a page contains listings
    older than that date and trims those listings from the final results.
    """
    scraper = await create_ultra_optimized_scraper(browser_manager, outbound_controller)
    tracker = PerformanceTracker()
    tracker.start_request()

    try:
        all_results = []
        all_metrics = []
        total_results: Optional[int] = None
        actual_max_pages: Optional[int] = None
        _log = logging.getLogger("scrape_by_url")

        # Fetch pages sequentially. The shared outbound controller applies the
        # process-wide start interval across this and every other API request.
        for page_num in range(1, max_pages + 1):
            # After page 1 we know the real page count — stop before wasting requests.
            if actual_max_pages is not None and page_num > actual_max_pages:
                _log.info(
                    f"[OVERVIEW] stopping after page {page_num - 1} "
                    f"(breadcrumb says {actual_max_pages} page(s) total, "
                    f"{total_results} results)"
                )
                break

            result = await scraper.ultra_optimized_fetch_page(
                inject_page(base_url, page_num),
                page_num,
                extra_selectors=_TOTAL_RESULTS_SELECTOR if page_num == 1 else None,
            )
            if not isinstance(result, Exception):
                page_results, page_metrics, extras = result

                if total_results is None and "breadcrump_summary" in extras:
                    total_results, actual_max_pages = _parse_breadcrumb(
                        extras["breadcrump_summary"]
                    )
                    effective = (
                        min(max_pages, actual_max_pages)
                        if actual_max_pages is not None
                        else max_pages
                    )
                    _log.info(
                        f"[OVERVIEW] breadcrumb: {total_results} results → "
                        f"{actual_max_pages} page(s) available, "
                        f"fetching up to {effective}"
                    )

                if not page_results:
                    _log.info(
                        f"[OVERVIEW] page {page_num} returned no results — stopping early"
                    )
                    all_metrics.append(page_metrics)
                    tracker.add_page_metric(page_metrics)
                    break

                stop = min_publish_date and _page_has_old_listings(
                    page_results, min_publish_date
                )
                if min_publish_date:
                    page_results = _filter_by_min_publish_date(
                        page_results, min_publish_date
                    )

                all_results.extend(page_results)
                all_metrics.append(page_metrics)
                tracker.add_page_metric(page_metrics)

                if stop:
                    break
            gc.collect()

        pages_attempted = len(all_metrics)
        tracker.set_concurrent_level(1)
        browser_metrics = browser_manager.get_performance_metrics()
        tracker.set_browser_contexts_used(
            browser_metrics["contexts_in_use"] + browser_metrics["contexts_in_pool"]
        )
        request_metrics = tracker.get_request_metrics()
        request_metrics_dict = request_metrics.to_dict()

        successful_pages = sum(1 for m in all_metrics if m.success)
        success_rate = (
            (successful_pages / pages_attempted) * 100 if pages_attempted > 0 else 0
        )

        response = {
            # Keep an empty but successfully loaded search distinguishable
            # from a browser/network failure on every attempted page.
            "success": successful_pages > 0,
            "results": all_results,
            "unique_results": len(all_results),
            "time_taken": round(request_metrics.total_time, 3),
            "performance_metrics": {
                "pages_requested": pages_attempted,
                "pages_successful": successful_pages,
                "success_rate": round(success_rate, 2),
                "average_page_time": round(
                    request_metrics_dict.get("average_page_time", 0), 3
                ),
            },
            "browser_metrics": browser_metrics,
        }

        if total_results is not None:
            response["total_results"] = total_results

        return response
    finally:
        try:
            await scraper.cleanup()
        except Exception:
            logging.getLogger(__name__).warning("URL scraper cleanup reported an error")
