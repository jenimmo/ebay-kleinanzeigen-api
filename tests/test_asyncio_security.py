import asyncio

import pytest

from scrapers.inserate_ultra_optimized import UltraOptimizedScraper
from utils.asyncio_optimizations import (
    HighPerformanceTaskManager,
    MemoryOptimizedProcessor,
)


@pytest.mark.parametrize(
    "factory",
    [HighPerformanceTaskManager, MemoryOptimizedProcessor],
)
def test_concurrency_limit_must_have_at_least_one_permit(factory):
    with pytest.raises(ValueError, match="max_concurrent must be at least 1"):
        factory(max_concurrent=0)


@pytest.mark.parametrize(
    "factory",
    [HighPerformanceTaskManager, MemoryOptimizedProcessor],
)
def test_positive_concurrency_limit_remains_supported(factory):
    instance = factory(max_concurrent=1)

    assert instance.semaphore._value == 1


def test_ultra_scraper_uses_configured_limit_not_drained_runtime_permits(
    outbound_controller,
):
    class BusyBrowserManager:
        max_concurrent = 3
        _semaphore = asyncio.Semaphore(0)

    scraper = UltraOptimizedScraper(BusyBrowserManager(), outbound_controller)

    assert scraper.task_manager.semaphore._value == 3
    assert scraper.memory_processor.semaphore._value == 3
    assert scraper.memory_processor.task_manager.semaphore._value == 3
