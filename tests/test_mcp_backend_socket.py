"""Real loopback socket restarts, without starting a backend or using its port."""

from concurrent.futures import ThreadPoolExecutor
import errno
import socket
import threading

import pytest

from kleinanzeigen_mcp.backend_launcher import _reserve_socket


def reserve_available(host):
    try:
        listener = _reserve_socket(host, 0)
    except OSError as error:
        if host == "::1" and error.errno in {errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL}:
            pytest.skip("IPv6 loopback is unavailable")
        raise
    listener.settimeout(2)
    return listener


def exchange(listener, *, close_server_first):
    address = listener.getsockname()
    with socket.create_connection(address[:2], timeout=2) as client:
        with listener.accept()[0] as server:
            server.settimeout(2)
            client.sendall(b"isolated loopback test")
            assert server.recv(128) == b"isolated loopback test"
            if close_server_first:
                # The listener's local port enters TIME_WAIT when this accepted
                # connection actively closes; no arbitrary sleep is necessary.
                server.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""
                client.shutdown(socket.SHUT_WR)
                assert server.recv(1) == b""
            else:
                client.shutdown(socket.SHUT_WR)
                assert server.recv(1) == b""
                server.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_reserved_socket_restarts_immediately_after_server_active_close(host):
    with reserve_available(host) as initial:
        port = initial.getsockname()[1]
        exchange(initial, close_server_first=True)
    # The old implementation raises EADDRINUSE here on macOS. The port must
    # remain the same; retrying on a new port would invalidate Safari pairing.
    with _reserve_socket(host, port) as restarted:
        restarted.settimeout(2)
        assert restarted.getsockname()[1] == port
        assert restarted.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) != 0
        if hasattr(socket, "SO_REUSEPORT"):
            assert restarted.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT) == 0
        exchange(restarted, close_server_first=True)
    with _reserve_socket(host, port) as second_restart:
        second_restart.settimeout(2)
        exchange(second_restart, close_server_first=False)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_reuseaddr_does_not_take_over_an_existing_listener(host):
    with reserve_available(host) as original:
        port = original.getsockname()[1]
        with pytest.raises(OSError) as caught:
            _reserve_socket(host, port)
        assert caught.value.errno == errno.EADDRINUSE
        exchange(original, close_server_first=False)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_simultaneous_reservations_never_create_two_listeners(host):
    with reserve_available(host) as selecting:
        port = selecting.getsockname()[1]
    ready = threading.Barrier(2, timeout=2)

    def attempt():
        ready.wait()
        try:
            return _reserve_socket(host, port)
        except OSError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = list(executor.map(lambda _: attempt(), range(2)))
    listeners = [value for value in attempts if isinstance(value, socket.socket)]
    errors = [value for value in attempts if isinstance(value, OSError)]
    try:
        assert len(listeners) == 1
        assert len(errors) == 1
        assert errors[0].errno == errno.EADDRINUSE
        listeners[0].settimeout(2)
        exchange(listeners[0], close_server_first=False)
    finally:
        for listener in listeners:
            listener.close()
