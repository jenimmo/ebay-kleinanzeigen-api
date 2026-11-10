"""Focused contracts for the coordinated STDIO backend launcher."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from typing import Any

import httpx
import pytest

from kleinanzeigen_mcp import backend_launcher
from kleinanzeigen_mcp.backend_launcher import (
    BackendLaunchError,
    BackendLauncher,
    LauncherSettings,
)


class _FakeProcess:
    def __init__(self, *, pid: int = 42_424, return_code: int | None = None) -> None:
        self.pid = pid
        self.return_code = return_code
        self.terminated = False
        self.killed = False
        self.waited = 0

    def poll(self) -> int | None:
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.return_code = -15

    def kill(self) -> None:
        self.killed = True
        self.return_code = -9

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.waited += 1
        return self.return_code or 0


class _FakeSocket:
    def __init__(
        self,
        address: tuple[str, int] = ("127.0.0.1", 43_210),
        *,
        file_descriptor: int = 73,
    ) -> None:
        self.address = address
        self.file_descriptor = file_descriptor
        self.closed = False

    def getsockname(self) -> tuple[str, int]:
        return self.address

    def fileno(self) -> int:
        return -1 if self.closed else self.file_descriptor

    def close(self) -> None:
        self.closed = True


def _settings(
    tmp_path: Path,
    *,
    explicit_base_url: str | None = None,
    autostart: bool = True,
    startup_timeout: float = 1.0,
) -> LauncherSettings:
    return LauncherSettings(
        autostart=autostart,
        explicit_base_url=explicit_base_url,
        startup_timeout=startup_timeout,
        health_timeout=0.01,
        runtime_dir=tmp_path / "runtime",
        log_path=tmp_path / "runtime" / "backend.log",
        log_max_bytes=1024,
        backend_directory=Path(__file__).resolve().parents[1],
    )


def _probe(kind: backend_launcher._ProbeKind) -> backend_launcher._ProbeResult:
    return backend_launcher._ProbeResult(kind, kind.value)


def test_autostart_can_be_disabled_without_touching_runtime(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        explicit_base_url="http://127.0.0.1:8765",
        autostart=False,
    )

    assert BackendLauncher(settings).ensure_backend() == "http://127.0.0.1:8765"
    assert not settings.runtime_dir.exists()


def test_healthy_explicit_backend_is_reused_without_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        explicit_base_url="http://127.0.0.1:8765",
    )
    monkeypatch.setattr(
        backend_launcher,
        "_probe_backend",
        lambda *_args: _probe(backend_launcher._ProbeKind.HEALTHY),
    )
    launcher = BackendLauncher(settings)
    monkeypatch.setattr(
        launcher,
        "_spawn_backend",
        lambda _socket: pytest.fail("healthy backend must not be started again"),
    )

    assert launcher.ensure_backend() == "http://127.0.0.1:8765"


def test_incompatible_explicit_service_is_never_replaced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        explicit_base_url="http://127.0.0.1:8765",
    )
    monkeypatch.setattr(
        backend_launcher,
        "_probe_backend",
        lambda *_args: _probe(backend_launcher._ProbeKind.INCOMPATIBLE),
    )
    monkeypatch.setattr(
        backend_launcher,
        "_reserve_socket",
        lambda *_args: pytest.fail("occupied address must not be bound"),
    )

    with pytest.raises(BackendLaunchError, match="already occupied.*not modified"):
        BackendLauncher(settings).ensure_backend()


def test_explicit_address_never_competes_with_published_living_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        explicit_base_url="http://127.0.0.1:8765",
    )
    launcher = BackendLauncher(settings)
    launcher._prepare_runtime_directory()
    published_url = "http://127.0.0.1:45678"
    launcher._write_state(
        backend_launcher._RuntimeState(
            base_url=published_url,
            pid=os.getpid(),
            started_at=time.time(),
            status="ready",
        )
    )
    monkeypatch.setattr(
        backend_launcher,
        "_probe_backend",
        lambda *_args: _probe(backend_launcher._ProbeKind.UNREACHABLE),
    )
    monkeypatch.setattr(
        backend_launcher,
        "_reserve_socket",
        lambda *_args: pytest.fail("conflicting backend must not be started"),
    )

    with pytest.raises(BackendLaunchError, match="conflicts.*no second"):
        launcher.ensure_backend()

    persisted = json.loads((settings.runtime_dir / "backend.json").read_text())
    assert persisted["base_url"] == published_url
    assert persisted["pid"] == os.getpid()


def test_default_start_uses_dynamic_port_and_publishes_ready_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        backend_launcher,
        "_probe_backend",
        lambda *_args: _probe(backend_launcher._ProbeKind.HEALTHY),
    )
    monkeypatch.setattr(
        backend_launcher,
        "_reserve_socket",
        lambda *_args: _FakeSocket(),
    )
    launcher = BackendLauncher(settings)
    started: list[tuple[str, int]] = []
    monkeypatch.setattr(launcher, "_start_reaper", lambda *_args: None)

    def fake_spawn(reserved_socket: _FakeSocket) -> _FakeProcess:
        started.append(reserved_socket.getsockname())
        reserved_socket.close()
        return _FakeProcess()

    monkeypatch.setattr(launcher, "_spawn_backend", fake_spawn)

    base_url = launcher.ensure_backend()

    assert base_url.startswith("http://127.0.0.1:")
    assert started == [("127.0.0.1", int(base_url.rsplit(":", 1)[1]))]
    state = json.loads((settings.runtime_dir / "backend.json").read_text())
    assert state == {
        "base_url": base_url,
        "pid": 42_424,
        "schema_version": 1,
        "started_at": state["started_at"],
        "status": "ready",
    }


def test_parallel_launchers_share_one_dynamic_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, startup_timeout=2.0)
    ready = threading.Event()
    spawn_count = 0
    spawn_count_lock = threading.Lock()

    def probe_backend(*_args: Any) -> backend_launcher._ProbeResult:
        kind = (
            backend_launcher._ProbeKind.HEALTHY
            if ready.is_set()
            else backend_launcher._ProbeKind.UNREACHABLE
        )
        return _probe(kind)

    def spawn_backend(reserved_socket: socket.socket) -> _FakeProcess:
        nonlocal spawn_count
        reserved_socket.close()
        with spawn_count_lock:
            spawn_count += 1
        threading.Timer(0.05, ready.set).start()
        return _FakeProcess(pid=os.getpid())

    monkeypatch.setattr(backend_launcher, "_probe_backend", probe_backend)
    monkeypatch.setattr(
        backend_launcher,
        "_reserve_socket",
        lambda *_args: _FakeSocket(),
    )
    monkeypatch.setattr(
        BackendLauncher,
        "_spawn_backend",
        lambda self, sock: spawn_backend(sock),
    )
    monkeypatch.setattr(
        BackendLauncher,
        "_start_reaper",
        lambda *_args: None,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(BackendLauncher(settings).ensure_backend) for _ in range(2)
        ]
        results = [future.result(timeout=3.0) for future in futures]

    assert results[0] == results[1]
    assert spawn_count == 1


def test_starting_state_is_waited_for_without_second_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    launcher = BackendLauncher(settings)
    launcher._prepare_runtime_directory()
    state = backend_launcher._RuntimeState(
        base_url="http://127.0.0.1:45678",
        pid=os.getpid(),
        started_at=time.time(),
        status="starting",
    )
    launcher._write_state(state)
    probes = iter(
        [
            _probe(backend_launcher._ProbeKind.UNREACHABLE),
            _probe(backend_launcher._ProbeKind.UNREACHABLE),
            _probe(backend_launcher._ProbeKind.HEALTHY),
        ]
    )
    monkeypatch.setattr(
        backend_launcher,
        "_probe_backend",
        lambda *_args: next(probes),
    )
    monkeypatch.setattr(
        launcher,
        "_spawn_backend",
        lambda _socket: pytest.fail("published starting process must be shared"),
    )

    assert launcher.ensure_backend() == state.base_url


def test_ready_but_unhealthy_state_is_not_treated_as_starting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    launcher = BackendLauncher(settings)
    launcher._prepare_runtime_directory()
    launcher._write_state(
        backend_launcher._RuntimeState(
            base_url="http://127.0.0.1:45678",
            pid=os.getpid(),
            started_at=time.time(),
            status="ready",
        )
    )
    monkeypatch.setattr(
        backend_launcher,
        "_probe_backend",
        lambda *_args: _probe(backend_launcher._ProbeKind.COMPATIBLE_UNHEALTHY),
    )
    monkeypatch.setattr(
        launcher,
        "_spawn_backend",
        lambda _socket: pytest.fail("unhealthy backend must not be duplicated"),
    )

    with pytest.raises(BackendLaunchError, match="compatible but unhealthy"):
        launcher.ensure_backend()


def test_living_ready_state_is_never_replaced_when_temporarily_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, startup_timeout=0.01)
    launcher = BackendLauncher(settings)
    launcher._prepare_runtime_directory()
    old_url = "http://127.0.0.1:45678"
    launcher._write_state(
        backend_launcher._RuntimeState(
            base_url=old_url,
            pid=os.getpid(),
            started_at=time.time(),
            status="ready",
        )
    )
    monkeypatch.setattr(
        backend_launcher,
        "_probe_backend",
        lambda *_args: _probe(backend_launcher._ProbeKind.UNREACHABLE),
    )
    monkeypatch.setattr(
        backend_launcher,
        "_reserve_socket",
        lambda *_args: pytest.fail("a living recorded process must not be duplicated"),
    )

    with pytest.raises(BackendLaunchError, match="still running.*no second"):
        launcher.ensure_backend()

    persisted = json.loads((settings.runtime_dir / "backend.json").read_text())
    assert persisted["pid"] == os.getpid()
    assert persisted["status"] == "ready"


def test_ready_state_is_replaced_only_after_recorded_process_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    launcher = BackendLauncher(settings)
    launcher._prepare_runtime_directory()
    old_url = "http://127.0.0.1:45678"
    launcher._write_state(
        backend_launcher._RuntimeState(
            base_url=old_url,
            pid=42_424,
            started_at=time.time(),
            status="ready",
        )
    )
    monkeypatch.setattr(backend_launcher, "_pid_is_running", lambda _pid: False)

    def probe_backend(base_url: str, _timeout: float) -> backend_launcher._ProbeResult:
        kind = (
            backend_launcher._ProbeKind.UNREACHABLE
            if base_url == old_url
            else backend_launcher._ProbeKind.HEALTHY
        )
        return _probe(kind)

    monkeypatch.setattr(backend_launcher, "_probe_backend", probe_backend)
    monkeypatch.setattr(
        backend_launcher,
        "_reserve_socket",
        lambda *_args: _FakeSocket(("127.0.0.1", 43_211)),
    )
    monkeypatch.setattr(
        launcher,
        "_spawn_backend",
        lambda sock: (sock.close(), _FakeProcess())[1],
    )
    monkeypatch.setattr(launcher, "_start_reaper", lambda *_args: None)

    assert launcher.ensure_backend() == "http://127.0.0.1:43211"


def test_child_exit_fails_without_signalling_or_replacing_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        explicit_base_url="http://127.0.0.1:45678",
    )
    monkeypatch.setattr(
        backend_launcher,
        "_probe_backend",
        lambda *_args: _probe(backend_launcher._ProbeKind.UNREACHABLE),
    )
    launcher = BackendLauncher(settings)
    monkeypatch.setattr(
        backend_launcher,
        "_reserve_socket",
        lambda *_args: _FakeSocket(("127.0.0.1", 45_678)),
    )

    def fake_spawn(reserved_socket: socket.socket) -> _FakeProcess:
        reserved_socket.close()
        return _FakeProcess(return_code=23)

    monkeypatch.setattr(launcher, "_spawn_backend", fake_spawn)

    with pytest.raises(BackendLaunchError, match="exited with status 23"):
        launcher.ensure_backend()


def test_state_publish_failure_stops_and_reaps_only_owned_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    launcher = BackendLauncher(_settings(tmp_path))
    process = _FakeProcess()
    monkeypatch.setattr(
        backend_launcher,
        "_reserve_socket",
        lambda *_args: _FakeSocket(),
    )
    monkeypatch.setattr(
        launcher,
        "_spawn_backend",
        lambda sock: (sock.close(), process)[1],
    )
    monkeypatch.setattr(
        launcher,
        "_write_state",
        lambda _state: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(BackendLaunchError, match="runtime state.*stopped"):
        launcher.ensure_backend()

    assert process.terminated is True
    assert process.killed is False
    assert process.waited == 1


def test_reaper_start_failure_stops_child_and_removes_ready_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    launcher = BackendLauncher(settings)
    process = _FakeProcess()
    monkeypatch.setattr(
        backend_launcher,
        "_reserve_socket",
        lambda *_args: _FakeSocket(),
    )
    monkeypatch.setattr(
        backend_launcher,
        "_probe_backend",
        lambda *_args: _probe(backend_launcher._ProbeKind.HEALTHY),
    )
    monkeypatch.setattr(
        launcher,
        "_spawn_backend",
        lambda sock: (sock.close(), process)[1],
    )
    monkeypatch.setattr(
        launcher,
        "_start_reaper",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("no thread")),
    )

    with pytest.raises(BackendLaunchError, match="monitored safely.*stopped"):
        launcher.ensure_backend()

    assert process.terminated is True
    assert process.waited == 1
    assert not (settings.runtime_dir / "backend.json").exists()


def test_owned_startup_timeout_stops_child_instead_of_returning_unready_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    launcher = BackendLauncher(_settings(tmp_path, startup_timeout=0.01))
    process = _FakeProcess()
    monkeypatch.setattr(
        backend_launcher,
        "_reserve_socket",
        lambda *_args: _FakeSocket(),
    )
    monkeypatch.setattr(
        backend_launcher,
        "_probe_backend",
        lambda *_args: _probe(backend_launcher._ProbeKind.UNREACHABLE),
    )
    monkeypatch.setattr(
        launcher,
        "_spawn_backend",
        lambda sock: (sock.close(), process)[1],
    )

    with pytest.raises(BackendLaunchError, match="did not become ready.*stopped"):
        launcher.ensure_backend()

    assert process.terminated is True
    assert process.waited == 1


def test_old_but_living_starting_child_times_out_without_second_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, startup_timeout=0.01)
    launcher = BackendLauncher(settings)
    launcher._prepare_runtime_directory()
    state = backend_launcher._RuntimeState(
        base_url="http://127.0.0.1:45678",
        pid=os.getpid(),
        started_at=1.0,
        status="starting",
    )
    launcher._write_state(state)
    monkeypatch.setattr(
        backend_launcher,
        "_probe_backend",
        lambda *_args: _probe(backend_launcher._ProbeKind.UNREACHABLE),
    )
    monkeypatch.setattr(
        backend_launcher,
        "_reserve_socket",
        lambda *_args: pytest.fail("living recorded child must not be duplicated"),
    )

    with pytest.raises(BackendLaunchError, match="still running.*no second"):
        launcher.ensure_backend()

    persisted = json.loads((settings.runtime_dir / "backend.json").read_text())
    assert persisted["pid"] == os.getpid()
    assert persisted["status"] == "starting"


def test_owned_cleanup_escalates_to_kill_when_sigterm_does_not_finish() -> None:
    class StubbornProcess(_FakeProcess):
        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            self.waited += 1
            if timeout is not None and not self.killed:
                raise subprocess.TimeoutExpired("uvicorn", timeout)
            return self.return_code or 0

    process = StubbornProcess()

    BackendLauncher._stop_owned_process(process)

    assert process.terminated is True
    assert process.killed is True
    assert process.waited == 2


def test_reaper_waits_for_child_and_clears_only_matching_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    launcher = BackendLauncher(settings)
    state = backend_launcher._RuntimeState(
        base_url="http://127.0.0.1:45678",
        pid=42_424,
        started_at=time.time(),
        status="starting",
    )
    cleared: list[backend_launcher._RuntimeState] = []

    monkeypatch.setattr(launcher, "_clear_matching_state_under_lock", cleared.append)
    process = _FakeProcess()

    launcher._reap_backend(process, state)

    assert process.waited == 1
    assert cleared == [state]


def test_url_provider_is_cached_until_explicit_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = backend_launcher.BackendUrlProvider(_settings(tmp_path))
    resolved = iter(["http://127.0.0.1:41001", "http://127.0.0.1:41002"])
    monkeypatch.setattr(
        provider._launcher,
        "ensure_backend",
        lambda: next(resolved),
    )

    assert provider() == "http://127.0.0.1:41001"
    assert provider() == "http://127.0.0.1:41001"
    assert provider(refresh=True) == "http://127.0.0.1:41002"


@pytest.mark.parametrize("browser_channel", ["chrome", "safari"])
def test_spawn_is_detached_uses_inherited_socket_and_never_mcp_stdout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    browser_channel: str,
) -> None:
    monkeypatch.setenv("KLEINANZEIGEN_MESSAGING_BROWSER_CHANNEL", browser_channel)
    monkeypatch.delenv("KLEINANZEIGEN_MESSAGING_SAFARI_ENDPOINT", raising=False)
    settings = _settings(tmp_path)
    settings.runtime_dir.mkdir(parents=True)
    captured: dict[str, Any] = {}

    def fake_popen(args: list[str], **kwargs: Any) -> _FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(backend_launcher.subprocess, "Popen", fake_popen)
    reserved_socket = _FakeSocket()
    inherited_fd = reserved_socket.fileno()
    reserved_port = reserved_socket.getsockname()[1]

    result = BackendLauncher(settings)._spawn_backend(reserved_socket)

    assert result.pid == 42_424
    assert captured["args"] == [
        os.sys.executable,
        "-m",
        "uvicorn",
        "main:app",
        "--fd",
        str(inherited_fd),
        "--workers",
        "1",
        "--no-access-log",
        "--log-level",
        "warning",
    ]
    kwargs = captured["kwargs"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is kwargs["stderr"]
    assert kwargs["stdout"] is not None
    assert kwargs["close_fds"] is True
    assert kwargs["pass_fds"] == (inherited_fd,)
    assert kwargs["start_new_session"] is True
    if browser_channel == "safari":
        assert kwargs["env"]["KLEINANZEIGEN_MESSAGING_SAFARI_ENDPOINT"] == (
            f"http://127.0.0.1:{reserved_port}/messaging/safari-bridge/exchange"
        )
    else:
        assert "KLEINANZEIGEN_MESSAGING_SAFARI_ENDPOINT" not in kwargs["env"]
    assert reserved_socket.fileno() == -1


def test_log_is_rotated_before_append(tmp_path: Path) -> None:
    log_path = tmp_path / "backend.log"
    log_path.write_bytes(b"old diagnostic output")

    with backend_launcher._open_log_file(log_path, max_bytes=8) as handle:
        handle.write(b"new")

    assert (tmp_path / "backend.log.1").read_bytes() == b"old diagnostic output"
    assert log_path.read_bytes() == b"new"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KLEINANZEIGEN_MCP_AUTOSTART", "sometimes"),
        ("KLEINANZEIGEN_MCP_STARTUP_TIMEOUT_SECONDS", "nan"),
        ("KLEINANZEIGEN_MCP_HEALTH_TIMEOUT_SECONDS", "0"),
        ("KLEINANZEIGEN_MCP_BACKEND_LOG_MAX_BYTES", "-1"),
    ],
)
def test_invalid_launcher_environment_fails_fast(
    name: str,
    value: str,
    tmp_path: Path,
) -> None:
    environment = {
        name: value,
        "KLEINANZEIGEN_MCP_RUNTIME_DIR": str(tmp_path / "runtime"),
        "KLEINANZEIGEN_MCP_BACKEND_DIRECTORY": str(Path(__file__).resolve().parents[1]),
    }

    with pytest.raises(BackendLaunchError):
        LauncherSettings.from_env(environment)


def test_probe_requires_health_and_canonical_job_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["trust_env"] is False

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, url: str) -> httpx.Response:
            if url.endswith("/health"):
                return httpx.Response(
                    200,
                    json={"status": "healthy", "browser_status": "optimized"},
                )
            return httpx.Response(200, json={"endpoints": ["/jobs/search"]})

    monkeypatch.setattr(backend_launcher.httpx, "Client", FakeClient)

    result = backend_launcher._probe_backend("http://127.0.0.1:8765", 0.1)

    assert result.kind is backend_launcher._ProbeKind.INCOMPATIBLE


def test_default_runtime_is_project_scoped_data_directory() -> None:
    settings = LauncherSettings.from_env({"KLEINANZEIGEN_MCP_AUTOSTART": "0"})
    repository = Path(__file__).resolve().parents[1]

    assert settings.runtime_dir == repository / "data" / ".mcp-runtime"
    assert settings.backend_directory == repository
