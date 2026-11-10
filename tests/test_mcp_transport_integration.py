"""Offline integration tests for the public MCP transports."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import sys
from typing import Any

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest
from starlette.routing import Mount, Route

import main
import kleinanzeigen_mcp.server as mcp_server_module
from kleinanzeigen_mcp.server import (
    MCP_HTTP_SESSION_IDLE_TIMEOUT_SECONDS,
    asgi_client_factory,
    create_mcp_server,
    create_streamable_http_app,
    network_client_factory,
)


EXPECTED_TOOLS = {
    "search_listings",
    "continue_search",
    "merge_search_results",
    "get_listing_details",
    "get_job_status",
    "get_job_results",
    "resume_job",
    "cancel_job",
    "list_accounts",
    "connect_account",
    "disconnect_account",
    "list_conversations",
    "get_conversation",
    "get_message_image",
    "upload_message_images",
    "start_conversation",
    "send_message",
    "get_message_operation",
}
_HANDSHAKE_VERSION = "2025-11-25"
_SUBPROCESS_TIMEOUT_SECONDS = 10.0
_TEST_IDLE_TIMEOUT_SECONDS = 0.5


@pytest.mark.asyncio
async def test_network_client_factory_ignores_environment_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, Any]] = []

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(mcp_server_module.httpx, "AsyncClient", FakeAsyncClient)

    async with network_client_factory("http://127.0.0.1:8765")():
        pass

    assert created == [{"trust_env": False}]


async def _write_message(
    process: asyncio.subprocess.Process,
    message: dict[str, Any],
) -> None:
    assert process.stdin is not None
    process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
    await process.stdin.drain()


def _decode_protocol_line(line: bytes) -> dict[str, Any]:
    assert line, "STDIO server closed stdout before sending the response"
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        pytest.fail(f"non-MCP output on stdout: {line!r} ({exc})")

    assert isinstance(message, dict), message
    assert message.get("jsonrpc") == "2.0", message
    return message


async def _read_response(
    process: asyncio.subprocess.Process,
    request_id: int,
    *,
    timeout: float = _SUBPROCESS_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    assert process.stdout is not None
    for _ in range(10):
        line = await asyncio.wait_for(
            process.stdout.readline(),
            timeout=timeout,
        )
        message = _decode_protocol_line(line)
        if message.get("id") == request_id:
            assert "error" not in message, message
            return message
    pytest.fail(f"no JSON-RPC response for request id {request_id}")


@pytest.mark.asyncio
async def test_installed_stdio_entrypoint_speaks_only_mcp_and_stops_on_eof() -> None:
    executable_name = (
        "kleinanzeigen-mcp.exe" if os.name == "nt" else "kleinanzeigen-mcp"
    )
    entrypoint = Path(sys.executable).with_name(executable_name)
    assert entrypoint.is_file(), f"installed MCP entry point missing: {entrypoint}"

    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.update(
        {
            "KLEINANZEIGEN_API_BASE_URL": "http://127.0.0.1:9",
            "KLEINANZEIGEN_MCP_AUTOSTART": "0",
            "PYTHONUNBUFFERED": "1",
        }
    )
    process = await asyncio.create_subprocess_exec(
        str(entrypoint),
        cwd=repository,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stderr is not None
    stderr_reader = asyncio.create_task(process.stderr.read())

    try:
        await _write_message(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _HANDSHAKE_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "kleinanzeigen-transport-test",
                        "version": "1.0",
                    },
                },
            },
        )
        initialize = await _read_response(process, 1)
        assert initialize["result"]["protocolVersion"] == _HANDSHAKE_VERSION
        assert initialize["result"]["serverInfo"]["name"] == "kleinanzeigen"

        await _write_message(
            process,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        await _write_message(
            process,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        tools_response = await _read_response(process, 2)
        tools = tools_response["result"]["tools"]
        assert {tool["name"] for tool in tools} == EXPECTED_TOOLS
        assert len(tools) == 18

        await _write_message(
            process,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "get_job_status",
                    "arguments": {"job_id": "offline-transport-probe"},
                },
            },
        )
        tool_response = await _read_response(process, 3)
        tool_result = tool_response["result"]
        assert tool_result["isError"] is True
        assert tool_result["structuredContent"]["error"]["code"] == ("transport_error")
        assert (
            json.loads(tool_result["content"][0]["text"])
            == tool_result["structuredContent"]
        )

        assert process.stdin is not None
        process.stdin.close()
        await process.stdin.wait_closed()
        return_code = await asyncio.wait_for(
            process.wait(),
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
        assert return_code == 0

        assert process.stdout is not None
        stdout_tail = await process.stdout.read()
        for line in stdout_tail.splitlines():
            _decode_protocol_line(line)
        assert await process.stdout.read() == b""

        # Logs and diagnostics may use stderr, but they must never contaminate stdout.
        await stderr_reader
    finally:
        if process.returncode is None:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        if not stderr_reader.done():
            stderr_reader.cancel()
        with suppress(asyncio.CancelledError):
            await stderr_reader


async def _initialize_stdio_process(
    process: asyncio.subprocess.Process,
    *,
    request_id: int,
) -> None:
    await _write_message(
        process,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": _HANDSHAKE_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "kleinanzeigen-autostart-test",
                    "version": "1.0",
                },
            },
        },
    )
    initialized = await _read_response(process, request_id)
    assert initialized["result"]["serverInfo"]["name"] == "kleinanzeigen"
    await _write_message(
        process,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    )


async def _call_missing_job(
    process: asyncio.subprocess.Process,
    *,
    request_id: int,
) -> None:
    await _write_message(
        process,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": "get_job_status",
                "arguments": {"job_id": "autostart-e2e-missing-job"},
            },
        },
    )
    response = await _read_response(process, request_id, timeout=60.0)
    result = response["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "job_not_found"


async def _close_stdio_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None and process.stdin is not None:
        process.stdin.close()
        await process.stdin.wait_closed()
        await asyncio.wait_for(process.wait(), timeout=10.0)
    assert process.returncode == 0
    assert process.stdout is not None
    for line in (await process.stdout.read()).splitlines():
        _decode_protocol_line(line)


async def _read_runtime_state(
    state_path: Path,
    *,
    excluded_pid: int | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            await asyncio.sleep(0.05)
            continue
        if state.get("status") == "ready" and state.get("pid") != excluded_pid:
            return state
        await asyncio.sleep(0.05)
    pytest.fail(f"no ready backend state appeared at {state_path}")


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _stop_owned_backend(pid: int) -> None:
    if not _pid_is_running(pid):
        return
    os.kill(pid, signal.SIGTERM)
    deadline = asyncio.get_running_loop().time() + 15.0
    while _pid_is_running(pid) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    if _pid_is_running(pid):
        os.kill(pid, signal.SIGKILL)


@pytest.mark.live
@pytest.mark.asyncio
async def test_stdio_clients_autostart_share_and_recover_backend(
    tmp_path: Path,
) -> None:
    """Exercise the installed command and a real Playwright/Uvicorn backend."""

    executable_name = (
        "kleinanzeigen-mcp.exe" if os.name == "nt" else "kleinanzeigen-mcp"
    )
    entrypoint = Path(sys.executable).with_name(executable_name)
    runtime_dir = tmp_path / "runtime"
    state_path = runtime_dir / "backend.json"
    environment = os.environ.copy()
    environment.pop("KLEINANZEIGEN_API_BASE_URL", None)
    environment.pop("KLEINANZEIGEN_JOB_RETRY_BUDGET", None)
    environment.update(
        {
            "ALL_PROXY": "http://127.0.0.1:9",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
            "KLEINANZEIGEN_JOB_DATABASE_PATH": str(tmp_path / "jobs.sqlite3"),
            "KLEINANZEIGEN_JOB_MIN_FREE_DISK_BYTES": "0",
            "KLEINANZEIGEN_MCP_AUTOSTART": "1",
            "KLEINANZEIGEN_MCP_HEALTH_TIMEOUT_SECONDS": "0.2",
            "KLEINANZEIGEN_MCP_RUNTIME_DIR": str(runtime_dir),
            "KLEINANZEIGEN_MCP_STARTUP_TIMEOUT_SECONDS": "60",
            "KLEINANZEIGEN_OUTBOUND_RETRY_BUDGET": "0",
            "PYTHONUNBUFFERED": "1",
        }
    )
    processes: list[asyncio.subprocess.Process] = []
    owned_backend_pids: set[int] = set()

    try:
        for _ in range(2):
            process = await asyncio.create_subprocess_exec(
                str(entrypoint),
                cwd=tmp_path,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            processes.append(process)

        await asyncio.gather(
            *(
                _initialize_stdio_process(process, request_id=index)
                for index, process in enumerate(processes, start=1)
            )
        )
        assert not state_path.exists(), "initialize must not start the backend"

        await asyncio.gather(
            *(
                _call_missing_job(process, request_id=10 + index)
                for index, process in enumerate(processes)
            )
        )
        first_state = await _read_runtime_state(state_path)
        first_pid = int(first_state["pid"])
        owned_backend_pids.add(first_pid)
        assert _pid_is_running(first_pid)

        await _close_stdio_process(processes[0])
        assert _pid_is_running(first_pid), "backend must survive MCP adapter EOF"

        await _stop_owned_backend(first_pid)
        await _call_missing_job(processes[1], request_id=20)
        second_state = await _read_runtime_state(
            state_path,
            excluded_pid=first_pid,
        )
        second_pid = int(second_state["pid"])
        owned_backend_pids.add(second_pid)
        assert second_pid != first_pid
        assert _pid_is_running(second_pid)

        await _close_stdio_process(processes[1])
        assert _pid_is_running(second_pid), "restarted backend must also survive EOF"
    finally:
        for process in processes:
            if process.returncode is None:
                process.terminate()
                with suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=3.0)
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        for pid in owned_backend_pids:
            await _stop_owned_backend(pid)


class _ConnectedBrowser:
    def is_connected(self) -> bool:
        return True


class _FakeBrowserManager:
    def __init__(self, *, max_contexts: int, max_concurrent: int) -> None:
        self.max_contexts = max_contexts
        self.max_concurrent = max_concurrent
        self._browser: _ConnectedBrowser | None = None

    async def start(self) -> None:
        self._browser = _ConnectedBrowser()

    async def close(self) -> None:
        self._browser = None


def _install_fresh_http_server(monkeypatch: pytest.MonkeyPatch):
    routes = [
        route
        for route in main.app.routes
        if type(route) is Route and route.app is main.mcp_http_app
    ]
    assert len(routes) == 1
    route = routes[0]

    assert main.app.routes[-1] is route
    assert route.path == "/mcp"
    assert not any(
        isinstance(candidate, Mount) and candidate.app is main.mcp_http_app
        for candidate in main.app.routes
    )
    assert {route.path for route in main.mcp_http_app.routes} >= {"/mcp"}
    assert main.mcp_http_server.session_manager.stateless is False
    assert (
        main.mcp_http_server.session_manager.session_idle_timeout
        == MCP_HTTP_SESSION_IDLE_TIMEOUT_SECONDS
    )

    # The SDK manager is deliberately single-use. Other lifespan tests may have
    # consumed the import-time instance, so this test installs an equivalent
    # fresh instance without weakening the production lifecycle contract.
    server = create_mcp_server(asgi_client_factory(main.app))
    http_app = create_streamable_http_app(
        server,
        session_idle_timeout_seconds=_TEST_IDLE_TIMEOUT_SECONDS,
    )
    monkeypatch.setattr(main, "mcp_http_server", server)
    monkeypatch.setattr(main, "mcp_http_app", http_app)
    monkeypatch.setattr(route, "app", http_app)
    monkeypatch.setattr(route, "endpoint", http_app)
    return server


async def _wait_until(predicate: Any, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail("condition did not become true before timeout")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_http_transport_route_security_lifecycle_and_rest_precedence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "KLEINANZEIGEN_JOB_DATABASE_PATH",
        str(tmp_path / "transport-jobs.sqlite3"),
    )
    monkeypatch.setenv("KLEINANZEIGEN_JOB_MIN_FREE_DISK_BYTES", "0")
    monkeypatch.setenv("KLEINANZEIGEN_JOB_POLL_INTERVAL_SECONDS", "0.01")
    monkeypatch.setenv("KLEINANZEIGEN_OUTBOUND_RETRY_BUDGET", "0")
    monkeypatch.delenv("KLEINANZEIGEN_JOB_RETRY_BUDGET", raising=False)
    monkeypatch.setattr(main, "OptimizedPlaywrightManager", _FakeBrowserManager)
    server = _install_fresh_http_server(monkeypatch)
    manager = server.session_manager

    assert manager._has_started is False
    assert manager._task_group is None
    assert manager.stateless is False
    assert manager.session_idle_timeout == _TEST_IDLE_TIMEOUT_SECONDS

    transport = httpx2.ASGITransport(app=main.app, raise_app_exceptions=True)
    async with main.lifespan(main.app):
        assert manager._has_started is True
        assert manager._task_group is not None

        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8000",
            follow_redirects=False,
            timeout=5.0,
        ) as http_client:
            # A known MCP path returns a protocol error for an unknown session,
            # not a slash redirect or Starlette's plain 404.
            exact_path = await http_client.get(
                "/mcp",
                headers={
                    "Accept": "text/event-stream",
                    "Mcp-Session-Id": "unknown-session",
                },
            )
            assert exact_path.status_code == 404
            assert not exact_path.history
            assert "location" not in exact_path.headers
            assert exact_path.json()["error"]["message"] == "Session not found"

            trailing_slash = await http_client.get("/mcp/")
            assert trailing_slash.status_code == 307
            assert trailing_slash.headers["location"] == ("http://127.0.0.1:8000/mcp")

            doubled_path = await http_client.get(
                "/mcp/mcp",
                headers={
                    "Accept": "text/event-stream",
                    "Mcp-Session-Id": "unknown-session",
                },
            )
            assert doubled_path.status_code == 404
            assert doubled_path.json() == {"detail": "Not Found"}

            # Exact MCP routing must not change the parent REST router semantics.
            root = await http_client.get("/")
            assert root.status_code == 200
            assert root.json()["message"] == "Welcome to the Kleinanzeigen API"
            assert "/mcp" in root.json()["endpoints"]

            health = await http_client.get("/health")
            assert health.status_code == 200
            assert health.json()["status"] == "healthy"
            health_slash = await http_client.get("/health/")
            assert health_slash.status_code == 307
            assert health_slash.headers["location"] == ("http://127.0.0.1:8000/health")

            conversion = await http_client.post("/convert-url", json={"url": ""})
            assert conversion.status_code == 200
            assert conversion.json()["inserate_params"]["page_count"] == 1

            async with streamable_http_client(
                "http://127.0.0.1:8000/mcp",
                http_client=http_client,
                terminate_on_close=False,
            ) as streams:
                async with ClientSession(*streams) as session:
                    initialized = await asyncio.wait_for(
                        session.initialize(), timeout=5.0
                    )
                    assert initialized.server_info.name == "kleinanzeigen"
                    listed = await asyncio.wait_for(session.list_tools(), timeout=5.0)
                    assert {tool.name for tool in listed.tools} == EXPECTED_TOOLS
                    called = await asyncio.wait_for(
                        session.call_tool(
                            "get_job_status",
                            {"job_id": "missing-offline-job"},
                        ),
                        timeout=5.0,
                    )
                    assert called.is_error is True
                    assert called.structured_content is not None
                    assert called.structured_content["error"]["code"] == (
                        "job_not_found"
                    )
                    assert len(manager._server_instances) == 1
                    session_id = next(iter(manager._server_instances))

            assert session_id in manager._server_instances
            assert not manager._server_instances[session_id].is_terminated

            reconnect_headers = {
                "Mcp-Session-Id": session_id,
                "Mcp-Protocol-Version": str(initialized.protocol_version),
            }
            remote_transport = httpx2.ASGITransport(
                app=main.app,
                raise_app_exceptions=True,
                client=("203.0.113.10", 49152),
            )
            async with httpx2.AsyncClient(
                transport=remote_transport,
                base_url="http://127.0.0.1:8000",
                headers=reconnect_headers,
                follow_redirects=False,
            ) as remote_client:
                unauthorized_delete = await remote_client.delete("/mcp")
            assert unauthorized_delete.status_code == 403
            assert session_id in manager._server_instances
            assert not manager._server_instances[session_id].is_terminated

            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:8000",
                headers=reconnect_headers,
                follow_redirects=False,
                timeout=5.0,
            ) as reconnect_client:
                async with streamable_http_client(
                    "http://127.0.0.1:8000/mcp",
                    http_client=reconnect_client,
                    terminate_on_close=False,
                ) as reconnect_streams:
                    async with ClientSession(*reconnect_streams) as reconnect_session:
                        reconnected_tools = await asyncio.wait_for(
                            reconnect_session.list_tools(), timeout=5.0
                        )
                        assert {
                            tool.name for tool in reconnected_tools.tools
                        } == EXPECTED_TOOLS

                deleted = await reconnect_client.delete("/mcp")
                assert deleted.status_code == 200

            # SDK 2.0 leaves the terminated transport registered. The local
            # compatibility middleware must remove exactly that session now.
            assert session_id not in manager._server_instances
            assert session_id not in manager._session_owners
            deleted_session = await http_client.get(
                "/mcp",
                headers={
                    "Accept": "text/event-stream",
                    "Mcp-Session-Id": session_id,
                },
            )
            assert deleted_session.status_code == 404
            assert deleted_session.json()["error"]["message"] == ("Session not found")

            # A client that disconnects without DELETE remains reconnectable
            # only until the configured finite idle deadline.
            async with streamable_http_client(
                "http://127.0.0.1:8000/mcp",
                http_client=http_client,
                terminate_on_close=False,
            ) as idle_streams:
                async with ClientSession(*idle_streams) as idle_session:
                    await asyncio.wait_for(idle_session.initialize(), timeout=5.0)
                    idle_session_id = next(iter(manager._server_instances))
                    idle_transport = manager._server_instances[idle_session_id]

            await _wait_until(
                lambda: (
                    idle_session_id not in manager._server_instances
                    and idle_transport.is_terminated
                )
            )
            expired_session = await http_client.get(
                "/mcp",
                headers={
                    "Accept": "text/event-stream",
                    "Mcp-Session-Id": idle_session_id,
                },
            )
            assert expired_session.status_code == 404
            assert expired_session.json()["error"]["message"] == ("Session not found")

            # Host validation happens when the SDK creates or resumes a transport.
            # An unknown explicit session is rejected earlier as "Session not found".
            foreign_host = await http_client.get(
                "/mcp",
                headers={
                    "Accept": "text/event-stream",
                    "Host": "attacker.invalid:8000",
                },
            )
            assert foreign_host.status_code == 421
            assert foreign_host.text == "Invalid Host header"
            rejected_transport = next(iter(manager._server_instances.values()))
            await _wait_until(
                lambda: (
                    manager._server_instances == {} and rejected_transport.is_terminated
                )
            )

            async with httpx2.AsyncClient(
                transport=remote_transport,
                base_url="http://127.0.0.1:8000",
                follow_redirects=False,
            ) as remote_client:
                remote_peer = await remote_client.get(
                    "/mcp",
                    headers={"Accept": "text/event-stream"},
                )
            assert remote_peer.status_code == 403
            assert remote_peer.text == ("MCP endpoint is available only from loopback")

    assert manager._task_group is None
    assert manager._lifespan_state is None
    assert manager._server_instances == {}
    assert main.app.state.browser_manager is None
    assert main.app.state.job_manager is None
