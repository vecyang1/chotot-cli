"""Local HTTP servers for end-to-end tests of the transport's fallback path.

The one flow nobody had run was the paid one: direct request blocked, a
residential proxy resolved, the request re-issued through it. It cannot be
provoked politely against the real gateway, so these stand in for the three
parties -- a gateway that blocks, a proxy that forwards, and an upstream that
answers -- as real sockets on 127.0.0.1, driven through the real entry point.
Nothing here is a mock of the transport: ``urllib`` speaks to these servers
exactly as it speaks to the internet.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit


class _Server:
    """Bind on an ephemeral port and serve on a daemon thread."""

    def __init__(self, handler_class: type) -> None:
        self.hits: List[str] = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        self.httpd.owner = self  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "_Server":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class BlockingGateway(_Server):
    """Answers every request with the status it was told to (default 403).

    Models an anti-bot block of the caller's address: the body is JSON so the
    transport cannot mistake the block for a contract change.
    """

    def __init__(self, status: int = 403) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                owner.hits.append(self.path)
                body = json.dumps({"blocked": True, "status": status}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if status == 429:
                    self.send_header("Retry-After", "0")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        super().__init__(Handler)


class UpstreamStub(_Server):
    """Serves one canned JSON payload for any path, as the real gateway would."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        owner = self
        body = json.dumps(payload).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                owner.hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        super().__init__(Handler)


class RewritingProxy(_Server):
    """An HTTP forward proxy that sends every request to ``upstream_base``.

    ``urllib`` addresses an ``http://`` target through a proxy with an
    absolute-URI request line (``GET http://host/path``). The proxy keeps the
    path and query, swaps the origin for ``upstream_base``, fetches it with a
    proxy-free opener (so an ambient ``HTTPS_PROXY`` cannot loop it back), and
    relays status and body. Only the origin changes, so the test observes the
    exact request the transport re-issued after switching.
    """

    def __init__(self, upstream_base: str) -> None:
        owner = self
        self.upstream_base = upstream_base.rstrip("/")
        self.forwarded: List[str] = []
        # The product's own context: on a python.org interpreter the system
        # trust store is empty and only certifi verifies, so a bare opener here
        # would fail every https upstream while the http:// stubs stayed green.
        from chotot.http import get_default_ssl_context

        direct = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=get_default_ssl_context()),
        )

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                owner.hits.append(self.path)
                parts = urlsplit(self.path)
                target = urlsplit(owner.upstream_base)
                rewritten = urlunsplit((target.scheme, target.netloc,
                                        target.path.rstrip("/") + parts.path,
                                        parts.query, ""))
                owner.forwarded.append(rewritten)
                headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in ("host", "proxy-connection", "accept-encoding")}
                request = urllib.request.Request(rewritten, headers=headers)
                try:
                    with direct.open(request, timeout=30) as response:
                        status, body = response.status, response.read()
                        content_type = response.headers.get("Content-Type", "application/json")
                except urllib.error.HTTPError as exc:
                    status, body = exc.code, exc.read()
                    content_type = exc.headers.get("Content-Type", "application/json")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        super().__init__(Handler)


def resolver_script(directory, url: str, argv_log: Optional[str] = None) -> str:
    """Write a stand-in resolver: prints ``url`` and records how it was called."""
    from pathlib import Path

    script = Path(directory) / "fake_resolver.py"
    log_line = (f"open({argv_log!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                if argv_log else "")
    script.write_text("import sys\n" + log_line + f"print({url!r})\n", encoding="utf-8")
    return str(script)
