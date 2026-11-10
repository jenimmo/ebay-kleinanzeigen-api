"""Exercise the native HTTP boundary using local fixtures only, never a website."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import subprocess
import sys
import threading


class Fixture(BaseHTTPRequestHandler):
    mode = "ok"
    redirected = 0
    checked_authorization = 0

    def log_message(self, *_):
        pass

    def do_GET(self):
        Fixture.redirected += 1
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        assert self.path == "/messaging/safari-bridge/exchange"
        assert self.headers["Authorization"] == "Bearer " + "x" * 48
        assert self.headers.get_all("X-Kleinanzeigen-Native-Image-Preview") == ["1"]
        assert self.headers.get("Cookie") is None
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body["profile_id"] == "default"
        assert body["action"] == "poll"
        Fixture.checked_authorization += 1
        mode = Fixture.mode
        status = 302 if mode == "redirect" else 200
        payload = b'{"ok":true}'
        if mode in {"huge_length", "huge_stream"}:
            payload = b"x" * (65536 + 1)
        elif mode == "invalid_json":
            payload = b"not JSON"
        self.send_response(status)
        self.send_header("Content-Type", "text/html" if mode == "wrong_type" else "application/json")
        if mode == "redirect":
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/never-follow")
        if mode != "huge_stream":
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


with ThreadingHTTPServer(("127.0.0.1", 0), Fixture) as server:
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/messaging/safari-bridge/exchange"
    for mode, expected in [
        ("ok", "ok"),
        ("redirect", "safari_backend_response_invalid"),
        ("huge_length", "safari_response_too_large"),
        ("huge_stream", "safari_response_too_large"),
        ("invalid_json", "safari_backend_response_invalid"),
        ("wrong_type", "safari_backend_response_invalid"),
    ]:
        Fixture.mode = mode
        result = subprocess.run([sys.argv[1], endpoint], check=True, capture_output=True, text=True, timeout=45)
        assert result.stdout.strip() == expected, (mode, result.stdout.strip())
    assert Fixture.redirected == 0, "Native bridge followed a redirect"
    assert Fixture.checked_authorization == 6
    server.shutdown()
    worker.join()
print("Native loopback transport: 6 checks passed; no redirects followed")
