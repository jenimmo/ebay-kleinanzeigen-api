"""Offline lifecycle coverage for the durable job integration in main.py."""

from __future__ import annotations

import pytest

import main


class _FakeBrowser:
    def is_connected(self) -> bool:
        return True


class _FakeBrowserManager:
    def __init__(self, *, max_contexts: int, max_concurrent: int) -> None:
        self.max_contexts = max_contexts
        self.max_concurrent = max_concurrent
        self._browser = None
        self.closed = False

    async def start(self) -> None:
        self._browser = _FakeBrowser()

    async def close(self) -> None:
        self.closed = True
        self._browser = None


@pytest.mark.asyncio
async def test_lifespan_restores_runtime_before_starting_workers(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    database = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("KLEINANZEIGEN_JOB_DATABASE_PATH", str(database))
    monkeypatch.setenv("KLEINANZEIGEN_JOB_MIN_FREE_DISK_BYTES", "0")
    monkeypatch.setenv("KLEINANZEIGEN_JOB_POLL_INTERVAL_SECONDS", "0.01")
    monkeypatch.setenv("KLEINANZEIGEN_OUTBOUND_RETRY_BUDGET", "0")
    monkeypatch.setattr(main, "OptimizedPlaywrightManager", _FakeBrowserManager)

    async with main.lifespan(main.app):
        manager = main.app.state.job_manager
        controller = main.app.state.outbound_controller

        assert manager is not None
        assert controller is not None
        assert manager.settings.retry_budget == 0
        assert controller.settings.retry_budget == 0
        assert len(manager._workers) == 1
        status = await manager.status()
        assert status["workers"]["configured"] == 1
        assert status["workers"]["running"] == 1
        assert sum(status["jobs"].values()) == 0
        assert database.exists()

    assert main.app.state.job_manager is None
    assert main.app.state.outbound_controller is None
    assert main.app.state.browser_manager is None


@pytest.mark.asyncio
async def test_lifespan_rejects_conflicting_retry_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KLEINANZEIGEN_OUTBOUND_RETRY_BUDGET", "0")
    monkeypatch.setenv("KLEINANZEIGEN_JOB_RETRY_BUDGET", "1")

    with pytest.raises(ValueError, match="one shared budget"):
        async with main.lifespan(main.app):
            raise AssertionError("startup must fail before allocating resources")
