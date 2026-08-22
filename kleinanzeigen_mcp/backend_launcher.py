"""Coordinate a detached local backend for the STDIO MCP transport.

The launcher deliberately lives outside the MCP protocol implementation.  It
uses a per-installation runtime directory and an advisory Unix lock so several
STDIO clients can safely discover or start one shared API process without ever
writing launcher or Uvicorn output to MCP stdout.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import BinaryIO, Iterator
from urllib.parse import urlsplit

import httpx


MANUAL_BACKEND_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_STARTUP_TIMEOUT_SECONDS = 90.0
DEFAULT_HEALTH_TIMEOUT_SECONDS = 2.0
DEFAULT_LOG_MAX_BYTES = 1_048_576
_POLL_INTERVAL_SECONDS = 0.1
_OWNED_PROCESS_STOP_TIMEOUT_SECONDS = 5.0
_STATE_SCHEMA_VERSION = 1
_REQUIRED_JOB_ENDPOINTS = frozenset(
    {
        "/jobs/search",
        "/jobs/details",
        "/jobs/{job_id}",
        "/jobs/{job_id}/results",
        "/jobs/{job_id}/continue",
        "/jobs/{job_id}/resume",
    }
)


class BackendLaunchError(RuntimeError):
    """The configured backend could not be safely reused or started."""


class _ProbeKind(Enum):
    HEALTHY = "healthy"
    UNREACHABLE = "unreachable"
    COMPATIBLE_UNHEALTHY = "compatible_unhealthy"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True)
class _ProbeResult:
    kind: _ProbeKind
    detail: str


@dataclass(frozen=True)
class _RuntimeState:
    base_url: str
    pid: int
    started_at: float
    status: str


@dataclass(frozen=True)
class LauncherSettings:
    """Validated environment configuration for local backend discovery."""

    autostart: bool
    explicit_base_url: str | None
    startup_timeout: float
    health_timeout: float
    runtime_dir: Path
    log_path: Path
    log_max_bytes: int
    backend_directory: Path

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> LauncherSettings:
        values = os.environ if environ is None else environ
        autostart = _parse_bool(
            values.get("KLEINANZEIGEN_MCP_AUTOSTART"),
            name="KLEINANZEIGEN_MCP_AUTOSTART",
            default=True,
        )
        startup_timeout = _parse_positive_float(
            values.get("KLEINANZEIGEN_MCP_STARTUP_TIMEOUT_SECONDS"),
            name="KLEINANZEIGEN_MCP_STARTUP_TIMEOUT_SECONDS",
            default=DEFAULT_STARTUP_TIMEOUT_SECONDS,
        )
        health_timeout = _parse_positive_float(
            values.get("KLEINANZEIGEN_MCP_HEALTH_TIMEOUT_SECONDS"),
            name="KLEINANZEIGEN_MCP_HEALTH_TIMEOUT_SECONDS",
            default=DEFAULT_HEALTH_TIMEOUT_SECONDS,
        )

        explicit_value = values.get("KLEINANZEIGEN_API_BASE_URL")
        explicit_base_url = (
            _validate_base_url(explicit_value) if explicit_value is not None else None
        )

        project_root = _discover_project_root(values)
        configured_runtime_dir = values.get("KLEINANZEIGEN_MCP_RUNTIME_DIR")
        if configured_runtime_dir is None:
            runtime_dir = _default_runtime_dir(values, project_root)
        else:
            runtime_dir = _absolute_path(
                configured_runtime_dir,
                name="KLEINANZEIGEN_MCP_RUNTIME_DIR",
            )

        configured_log_path = values.get("KLEINANZEIGEN_MCP_BACKEND_LOG_PATH")
        if configured_log_path is None:
            log_path = runtime_dir / "backend.log"
        else:
            candidate = Path(configured_log_path).expanduser()
            log_path = candidate if candidate.is_absolute() else runtime_dir / candidate
        log_max_bytes = _parse_positive_int(
            values.get("KLEINANZEIGEN_MCP_BACKEND_LOG_MAX_BYTES"),
            name="KLEINANZEIGEN_MCP_BACKEND_LOG_MAX_BYTES",
            default=DEFAULT_LOG_MAX_BYTES,
        )

        configured_backend_directory = values.get("KLEINANZEIGEN_MCP_BACKEND_DIRECTORY")
        if configured_backend_directory is None:
            backend_directory = _default_backend_directory(runtime_dir, project_root)
        else:
            backend_directory = _absolute_path(
                configured_backend_directory,
                name="KLEINANZEIGEN_MCP_BACKEND_DIRECTORY",
            )
        if not backend_directory.is_dir():
            raise BackendLaunchError(
                "KLEINANZEIGEN_MCP_BACKEND_DIRECTORY is not a directory"
            )

        return cls(
            autostart=autostart,
            explicit_base_url=explicit_base_url,
            startup_timeout=startup_timeout,
            health_timeout=health_timeout,
            runtime_dir=runtime_dir,
            log_path=log_path,
            log_max_bytes=log_max_bytes,
            backend_directory=backend_directory,
        )


class BackendLauncher:
    """Discover or safely start one detached Uvicorn jobs backend."""

    def __init__(self, settings: LauncherSettings) -> None:
        self.settings = settings
        self._state_path = settings.runtime_dir / "backend.json"
        self._lock_path = settings.runtime_dir / "startup.lock"

    def ensure_backend(self) -> str:
        """Return a healthy compatible base URL, starting it when permitted."""

        explicit = self.settings.explicit_base_url
        if not self.settings.autostart:
            return explicit or MANUAL_BACKEND_BASE_URL

        self._prepare_runtime_directory()
        deadline = time.monotonic() + self.settings.startup_timeout
        if explicit is not None:
            return self._ensure_explicit_backend(explicit, deadline)
        return self._ensure_discovered_backend(deadline)

    def _ensure_explicit_backend(self, base_url: str, deadline: float) -> str:
        initial = _probe_backend(base_url, self.settings.health_timeout)
        if initial.kind is _ProbeKind.HEALTHY:
            return base_url

        with _startup_lock(self._lock_path, deadline):
            current = _probe_backend(base_url, self.settings.health_timeout)
            if current.kind is _ProbeKind.HEALTHY:
                return base_url
            state = self._read_state()
            if state is not None and state.base_url != base_url:
                published = _probe_backend(
                    state.base_url,
                    self.settings.health_timeout,
                )
                published_process_is_running = _pid_is_running(state.pid)
                published_api_is_active = published.kind in {
                    _ProbeKind.HEALTHY,
                    _ProbeKind.COMPATIBLE_UNHEALTHY,
                }
                if published_process_is_running or published_api_is_active:
                    raise BackendLaunchError(
                        "the configured API address conflicts with a different "
                        "published shared backend; no second backend was started"
                    )
                self._clear_matching_state(state)
                state = None
            if state is not None and state.base_url == base_url:
                process_is_running = _pid_is_running(state.pid)
                should_wait = state.status == "starting" or current.kind in {
                    _ProbeKind.UNREACHABLE,
                    _ProbeKind.INCOMPATIBLE,
                }
                if process_is_running and should_wait:
                    if self._wait_for_existing_backend(state, deadline):
                        return base_url
                elif not process_is_running:
                    self._clear_matching_state(state)
                current = _probe_backend(base_url, self.settings.health_timeout)
                if current.kind is _ProbeKind.HEALTHY:
                    return base_url
            if current.kind is not _ProbeKind.UNREACHABLE:
                raise BackendLaunchError(
                    f"the configured API at {base_url} is already occupied but "
                    f"cannot be reused ({current.detail}); it was not modified"
                )

            target = _parse_autostart_target(base_url)
            try:
                reserved_socket = _reserve_socket(target.host, target.port)
            except OSError as exc:
                raced = _probe_backend(base_url, self.settings.health_timeout)
                if raced.kind is _ProbeKind.HEALTHY:
                    return base_url
                raise BackendLaunchError(
                    f"the configured API address {base_url} became occupied; "
                    "the existing service was not modified"
                ) from exc

            return self._launch_reserved_backend(
                reserved_socket,
                base_url=base_url,
                deadline=deadline,
            )

    def _ensure_discovered_backend(self, deadline: float) -> str:
        state = self._read_state()
        if state is not None:
            initial = _probe_backend(state.base_url, self.settings.health_timeout)
            if initial.kind is _ProbeKind.HEALTHY:
                return state.base_url

        with _startup_lock(self._lock_path, deadline):
            state = self._read_state()
            if state is not None:
                current = _probe_backend(
                    state.base_url,
                    self.settings.health_timeout,
                )
                if current.kind is _ProbeKind.HEALTHY:
                    if state.status == "starting":
                        self._write_ready_state(state)
                    return state.base_url
                if state.status == "starting":
                    if self._state_process_may_be_starting(state):
                        if self._wait_for_existing_backend(state, deadline):
                            return state.base_url
                    else:
                        self._clear_matching_state(state)
                    current = _probe_backend(
                        state.base_url,
                        self.settings.health_timeout,
                    )
                    if current.kind is _ProbeKind.HEALTHY:
                        return state.base_url
                if current.kind is _ProbeKind.COMPATIBLE_UNHEALTHY:
                    raise BackendLaunchError(
                        f"the shared API at {state.base_url} is compatible but "
                        "unhealthy; no second backend was started"
                    )
                if _pid_is_running(state.pid):
                    if self._wait_for_existing_backend(state, deadline):
                        return state.base_url
                    current = _probe_backend(
                        state.base_url,
                        self.settings.health_timeout,
                    )
                    if current.kind is _ProbeKind.HEALTHY:
                        return state.base_url
            reserved_socket = _reserve_socket("127.0.0.1", 0)
            port = reserved_socket.getsockname()[1]
            base_url = f"http://127.0.0.1:{port}"
            return self._launch_reserved_backend(
                reserved_socket,
                base_url=base_url,
                deadline=deadline,
            )

    def _launch_reserved_backend(
        self,
        reserved_socket: socket.socket,
        *,
        base_url: str,
        deadline: float,
    ) -> str:
        process = self._spawn_backend(reserved_socket)
        state = _RuntimeState(
            base_url=base_url,
            pid=process.pid,
            started_at=time.time(),
            status="starting",
        )
        try:
            self._write_state(state)
        except BaseException as exc:
            self._stop_owned_process(process)
            self._clear_matching_state(state)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise BackendLaunchError(
                "the local API started but its shared runtime state could not be "
                "published; the owned process was stopped"
            ) from exc
        return self._wait_for_owned_backend(process, state, deadline)

    def _spawn_backend(self, reserved_socket: socket.socket) -> subprocess.Popen[bytes]:
        log_handle: BinaryIO | None = None
        try:
            log_handle = _open_log_file(
                self.settings.log_path,
                self.settings.log_max_bytes,
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "main:app",
                    "--fd",
                    str(reserved_socket.fileno()),
                    "--workers",
                    "1",
                    "--no-access-log",
                    "--log-level",
                    "warning",
                ],
                cwd=self.settings.backend_directory,
                env=dict(os.environ),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                close_fds=True,
                pass_fds=(reserved_socket.fileno(),),
                start_new_session=True,
            )
        except Exception:
            reserved_socket.close()
            if log_handle is not None:
                log_handle.close()
            raise
        reserved_socket.close()
        log_handle.close()
        return process

    def _wait_for_owned_backend(
        self,
        process: subprocess.Popen[bytes],
        state: _RuntimeState,
        deadline: float,
    ) -> str:
        last_probe = _ProbeResult(_ProbeKind.UNREACHABLE, "not reachable")
        while True:
            try:
                last_probe = _probe_backend(
                    state.base_url,
                    self.settings.health_timeout,
                )
            except Exception as exc:
                last_probe = _ProbeResult(
                    _ProbeKind.INCOMPATIBLE,
                    f"health probe failed with {type(exc).__name__}",
                )
            if last_probe.kind is _ProbeKind.HEALTHY:
                ready_state = _RuntimeState(
                    base_url=state.base_url,
                    pid=state.pid,
                    started_at=state.started_at,
                    status="ready",
                )
                try:
                    self._write_state(ready_state)
                    self._start_reaper(process, ready_state)
                except BaseException as exc:
                    self._stop_owned_process(process)
                    self._clear_matching_state(ready_state)
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise
                    raise BackendLaunchError(
                        "the local API became ready but its owned process could not "
                        "be monitored safely; it was stopped"
                    ) from exc
                return state.base_url

            return_code = process.poll()
            if return_code is not None:
                self._reap_exited_owned_process(process)
                self._clear_matching_state(state)
                raise BackendLaunchError(
                    f"the local API process exited with status {return_code} "
                    f"during startup; see {self.settings.log_path}"
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._stop_owned_process(process)
                self._clear_matching_state(state)
                raise BackendLaunchError(
                    f"the local API at {state.base_url} did not become ready within "
                    f"{self.settings.startup_timeout:g} seconds "
                    f"({last_probe.detail}); the owned process was stopped; see "
                    f"{self.settings.log_path}"
                )
            time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))

    def _wait_for_existing_backend(
        self,
        state: _RuntimeState,
        deadline: float,
    ) -> bool:
        last_probe = _ProbeResult(_ProbeKind.UNREACHABLE, "not reachable")
        while True:
            try:
                last_probe = _probe_backend(
                    state.base_url,
                    self.settings.health_timeout,
                )
            except Exception as exc:
                last_probe = _ProbeResult(
                    _ProbeKind.INCOMPATIBLE,
                    f"health probe failed with {type(exc).__name__}",
                )
            if last_probe.kind is _ProbeKind.HEALTHY:
                self._write_ready_state(state)
                return True
            if not _pid_is_running(state.pid):
                self._clear_matching_state(state)
                return False

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BackendLaunchError(
                    f"the shared API process at {state.base_url} is still running "
                    f"but did not become ready within "
                    f"{self.settings.startup_timeout:g} seconds "
                    f"({last_probe.detail}); no second backend was started"
                )
            time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))

    def _write_ready_state(self, state: _RuntimeState) -> None:
        current = self._read_state()
        if (
            current is not None
            and current.base_url == state.base_url
            and current.pid == state.pid
            and current.started_at == state.started_at
        ):
            self._write_state(
                _RuntimeState(
                    base_url=current.base_url,
                    pid=current.pid,
                    started_at=current.started_at,
                    status="ready",
                )
            )

    def _start_reaper(
        self,
        process: subprocess.Popen[bytes],
        state: _RuntimeState,
    ) -> None:
        reaper = threading.Thread(
            target=self._reap_backend,
            args=(process, state),
            name="kleinanzeigen-backend-reaper",
            daemon=True,
        )
        reaper.start()

    def _reap_backend(
        self,
        process: subprocess.Popen[bytes],
        state: _RuntimeState,
    ) -> None:
        try:
            process.wait()
        finally:
            self._clear_matching_state_under_lock(state)

    @staticmethod
    def _reap_exited_owned_process(process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait()
        except ChildProcessError:
            return

    @staticmethod
    def _stop_owned_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            try:
                process.terminate()
            except (OSError, ProcessLookupError):
                pass
        try:
            process.wait(timeout=_OWNED_PROCESS_STOP_TIMEOUT_SECONDS)
            return
        except ChildProcessError:
            return
        except subprocess.TimeoutExpired:
            pass
        if process.poll() is None:
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                pass
        try:
            process.wait()
        except ChildProcessError:
            return

    def _clear_matching_state_under_lock(self, expected: _RuntimeState) -> None:
        deadline = time.monotonic() + self.settings.health_timeout
        try:
            with _startup_lock(self._lock_path, deadline):
                self._clear_matching_state(expected)
        except (BackendLaunchError, OSError):
            return

    def _state_process_may_be_starting(self, state: _RuntimeState) -> bool:
        if state.status != "starting":
            return False
        return _pid_is_running(state.pid)

    def _prepare_runtime_directory(self) -> None:
        self.settings.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.settings.runtime_dir.chmod(0o700)
        except OSError as exc:
            raise BackendLaunchError(
                "the MCP runtime directory cannot be secured"
            ) from exc

    def _read_state(self) -> _RuntimeState | None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            base_url = _validate_base_url(raw["base_url"])
            _parse_autostart_target(base_url)
            pid = raw["pid"]
            started_at = raw["started_at"]
            status = raw["status"]
            if raw.get("schema_version") != _STATE_SCHEMA_VERSION:
                return None
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                return None
            if not isinstance(started_at, (int, float)) or not math.isfinite(
                float(started_at)
            ):
                return None
            if status not in {"starting", "ready"}:
                return None
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None
        return _RuntimeState(
            base_url=base_url,
            pid=pid,
            started_at=float(started_at),
            status=status,
        )

    def _write_state(self, state: _RuntimeState) -> None:
        payload = {
            "schema_version": _STATE_SCHEMA_VERSION,
            "base_url": state.base_url,
            "pid": state.pid,
            "started_at": state.started_at,
            "status": state.status,
        }
        fd, temporary_name = tempfile.mkstemp(
            prefix="backend-",
            suffix=".tmp",
            dir=self.settings.runtime_dir,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(self._state_path)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            temporary_path.unlink(missing_ok=True)
            raise

    def _clear_matching_state(self, expected: _RuntimeState) -> None:
        current = self._read_state()
        if current is None:
            return
        if (
            current.base_url == expected.base_url
            and current.pid == expected.pid
            and current.started_at == expected.started_at
        ):
            self._state_path.unlink(missing_ok=True)


class BackendUrlProvider:
    """Lazily resolve and cache the shared backend URL for one MCP session."""

    def __init__(self, settings: LauncherSettings) -> None:
        self._launcher = BackendLauncher(settings)
        self._lock = threading.Lock()
        self._cached_base_url: str | None = None

    def __call__(self, refresh: bool = False) -> str:
        """Resolve once, or safely re-check/restart after a transport failure."""

        with self._lock:
            if self._cached_base_url is not None and not refresh:
                return self._cached_base_url
            self._cached_base_url = self._launcher.ensure_backend()
            return self._cached_base_url


@dataclass(frozen=True)
class _AutostartTarget:
    host: str
    port: int


def resolve_backend_base_url(
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve the backend used by STDIO, starting a shared local one if needed."""

    try:
        settings = LauncherSettings.from_env(environ)
        return BackendLauncher(settings).ensure_backend()
    except BackendLaunchError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackendLaunchError("the local API could not be started safely") from exc


def _probe_backend(base_url: str, timeout: float) -> _ProbeResult:
    health_url = f"{base_url.rstrip('/')}/health"
    root_url = f"{base_url.rstrip('/')}/"
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            health_response = client.get(health_url)
            try:
                health_body = health_response.json()
            except (json.JSONDecodeError, ValueError):
                health_body = None

            try:
                root_response = client.get(root_url)
                root_body = root_response.json()
            except (httpx.HTTPError, json.JSONDecodeError, ValueError):
                root_response = None
                root_body = None
    except httpx.ConnectError:
        target = _target_if_local_http(base_url)
        if target is not None and _has_tcp_listener(target.host, target.port, timeout):
            return _ProbeResult(
                _ProbeKind.INCOMPATIBLE,
                "a non-HTTP or incompatible listener owns the address",
            )
        return _ProbeResult(_ProbeKind.UNREACHABLE, "connection refused")
    except httpx.HTTPError as exc:
        return _ProbeResult(
            _ProbeKind.INCOMPATIBLE,
            f"the address accepted a connection but the health probe failed: "
            f"{type(exc).__name__}",
        )

    compatible = (
        root_response is not None
        and root_response.status_code == 200
        and isinstance(root_body, dict)
        and isinstance(root_body.get("endpoints"), list)
        and _REQUIRED_JOB_ENDPOINTS.issubset(root_body["endpoints"])
    )
    if not compatible:
        return _ProbeResult(
            _ProbeKind.INCOMPATIBLE,
            "the HTTP service does not expose the canonical jobs API",
        )

    healthy = (
        health_response.status_code == 200
        and isinstance(health_body, dict)
        and health_body.get("status") == "healthy"
        and health_body.get("browser_status") == "optimized"
    )
    if healthy:
        return _ProbeResult(_ProbeKind.HEALTHY, "healthy and compatible")
    return _ProbeResult(
        _ProbeKind.COMPATIBLE_UNHEALTHY,
        f"compatible API returned health status {health_response.status_code}",
    )


def _validate_base_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BackendLaunchError(
            "KLEINANZEIGEN_API_BASE_URL must be a non-empty HTTP(S) URL"
        )
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise BackendLaunchError(
            "KLEINANZEIGEN_API_BASE_URL must be an HTTP(S) URL without "
            "credentials, query, or fragment"
        )
    try:
        parsed.port
    except ValueError as exc:
        raise BackendLaunchError(
            "KLEINANZEIGEN_API_BASE_URL contains an invalid port"
        ) from exc
    return candidate


def _parse_autostart_target(base_url: str) -> _AutostartTarget:
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.path not in {"", "/"}:
        raise BackendLaunchError(
            "automatic startup requires a root-level loopback HTTP base URL"
        )
    host = parsed.hostname
    if host is None or not _is_loopback_host(host):
        raise BackendLaunchError(
            "automatic startup is restricted to a loopback address"
        )
    port = 80 if parsed.port is None else parsed.port
    if port == 0:
        raise BackendLaunchError("automatic startup requires a concrete non-zero port")
    return _AutostartTarget(host=host, port=port)


def _target_if_local_http(base_url: str) -> _AutostartTarget | None:
    try:
        return _parse_autostart_target(base_url)
    except BackendLaunchError:
        return None


def _is_loopback_host(host: str) -> bool:
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _reserve_socket(host: str, port: int) -> socket.socket:
    bind_host = "127.0.0.1" if host.lower().rstrip(".") == "localhost" else host
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
    reserved = socket.socket(family, socket.SOCK_STREAM)
    try:
        reserved.bind((bind_host, port))
        reserved.listen(2048)
    except Exception:
        reserved.close()
        raise
    return reserved


def _has_tcp_listener(host: str, port: int, timeout: float) -> bool:
    connect_host = "127.0.0.1" if host.lower().rstrip(".") == "localhost" else host
    try:
        with socket.create_connection(
            (connect_host, port),
            timeout=min(timeout, 0.5),
        ):
            return True
    except OSError:
        return False


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _open_log_file(path: Path, max_bytes: int = DEFAULT_LOG_MAX_BYTES) -> BinaryIO:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if path.stat().st_size >= max_bytes:
            rotated = path.with_name(f"{path.name}.1")
            rotated.unlink(missing_ok=True)
            path.replace(rotated)
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "ab", buffering=0)


@contextmanager
def _startup_lock(path: Path, deadline: float) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    handle = os.fdopen(fd, "a+b", buffering=0)
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BackendLaunchError(
                        "timed out waiting for another MCP backend launcher"
                    ) from None
                time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _default_runtime_dir(
    environ: Mapping[str, str],
    project_root: Path | None,
) -> Path:
    if project_root is not None:
        return project_root / "data" / ".mcp-runtime"
    identity = hashlib.sha256(
        str(Path(__file__).resolve().parents[1]).encode("utf-8")
    ).hexdigest()[:12]
    xdg_runtime = environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        base = Path(xdg_runtime).expanduser()
    else:
        base = Path.home() / ".cache"
    return base / "kleinanzeigen-mcp" / identity


def _default_backend_directory(
    runtime_dir: Path,
    project_root: Path | None,
) -> Path:
    if project_root is not None:
        return project_root
    source_root = Path(__file__).resolve().parents[1]
    if (source_root / "main.py").is_file():
        return source_root
    return runtime_dir


def _discover_project_root(environ: Mapping[str, str]) -> Path | None:
    configured = environ.get("KLEINANZEIGEN_MCP_BACKEND_DIRECTORY")
    if configured:
        path = Path(configured).expanduser()
        if path.is_absolute() and (path / "main.py").is_file():
            return path

    source_root = Path(__file__).resolve().parents[1]
    candidates = [Path(sys.prefix).resolve().parent, source_root]
    try:
        current_directory = Path.cwd().resolve()
    except OSError:
        current_directory = None
    if current_directory is not None:
        candidates.extend((current_directory, *current_directory.parents))

    for candidate in candidates:
        if (
            (candidate / "main.py").is_file()
            and (candidate / "kleinanzeigen_mcp").is_dir()
            and (candidate / "pyproject.toml").is_file()
        ):
            return candidate
    return None


def _absolute_path(value: str, *, name: str) -> Path:
    if not value.strip():
        raise BackendLaunchError(f"{name} must not be empty")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise BackendLaunchError(f"{name} must be an absolute path")
    return path


def _parse_bool(value: str | None, *, name: str, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise BackendLaunchError(
        f"{name} must be one of 1/0, true/false, yes/no, or on/off"
    )


def _parse_positive_float(value: str | None, *, name: str, default: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise BackendLaunchError(f"{name} must be a positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise BackendLaunchError(f"{name} must be a positive finite number")
    return parsed


def _parse_positive_int(value: str | None, *, name: str, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise BackendLaunchError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise BackendLaunchError(f"{name} must be a positive integer")
    return parsed


__all__ = [
    "BackendLaunchError",
    "BackendLauncher",
    "BackendUrlProvider",
    "LauncherSettings",
    "resolve_backend_base_url",
]
