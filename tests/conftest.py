from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass
class FakeServer:
    """Serves in-memory blobs, honouring single-range Range requests."""

    base: str
    files: dict[str, bytes] = field(default_factory=dict)
    honour_range: bool = True
    requests: list[dict[str, str]] = field(default_factory=list)

    def url(self, name: str) -> str:
        return f"{self.base}/{name}"


@pytest.fixture
def http_server() -> Iterator[FakeServer]:
    state = FakeServer(base="")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.requests.append({k.lower(): v for k, v in self.headers.items()})
            body = state.files.get(self.path.lstrip("/"))
            if body is None:
                self.send_error(404)
                return
            rng = self.headers.get("Range")
            if rng and state.honour_range:
                start = int(rng.removeprefix("bytes=").split("-")[0])
                chunk = body[start:]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
            else:
                chunk = body
                self.send_response(200)
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)

        def log_message(self, format: str, *args: object) -> None:
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield state
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def products_xml() -> bytes:
    return (FIXTURES / "products.xml").read_bytes()
