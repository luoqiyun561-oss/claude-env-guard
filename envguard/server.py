"""Local companion: serve the browser page and the system facts on one origin.

Why a companion at all: the browser half of this tool can never read the
registry, the routing table or the process list, and the native half can never
see WebRTC. The only way to put both in ONE page is for a local process to
serve that page, so the page and the system facts share an origin and no CORS
negotiation is needed — the same loopback-server pattern the DSH plugin relays
use (127.0.0.1 loopback, bind-to-localhost-only).

Lifecycle: double-click ClaudeEnvGuard-web.exe -> it scans in the background,
serves http://127.0.0.1:<port>/, and opens the default browser. Closing the
console window stops the server. Nothing is installed, nothing autostarts, and
the socket is bound to 127.0.0.1 only — the system facts it exposes (adapter
names, proxy settings) must not leave this machine.
"""

import http.server
import json
import os
import socket
import sys
import threading
import webbrowser

from . import collector, report as report_mod

DEFAULT_PORT = 8788
_PORT_ATTEMPTS = 11

# Plain ASCII in console output: the console is GBK on a zh-CN machine and a
# non-GBK glyph raises UnicodeEncodeError — measured during the exe build.
_MSG_STARTING = "ClaudeEnvGuard companion - scanning and serving..."
_MSG_READY = "Ready. The report is at {url}  (close this window to stop)"
_MSG_OPEN = "Opening browser..."
_MSG_FAIL = "Could not open a browser automatically; open {url} yourself."
_MSG_NO_PAGE = "ClaudeEnvGuard.html not found next to the program, or no free port."


def _html_path():
    """Locate the page to serve, frozen or from source.

    Frozen: PyInstaller's --add-data put it at the bundle root. From source: the
    project root, which is one level above this package.
    """
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(os.path.join(base, "ClaudeEnvGuard.html"))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "ClaudeEnvGuard.html"))
    candidates.append(os.path.join(os.path.dirname(here), "ClaudeEnvGuard.html"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


class _ScanState:
    """One background scan at a time; the API serves the latest finished one."""

    def __init__(self):
        self._lock = threading.Lock()
        self._scanning = False
        self.report = None
        self.error = None
        self.finished_at = None

    @property
    def scanning(self):
        with self._lock:
            return self._scanning

    def start(self):
        """Kick a scan; returns False when one is already running."""
        with self._lock:
            if self._scanning:
                return False
            self._scanning = True
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self):
        try:
            result = collector.collect()
            payload, error = report_mod.to_dict(result), None
        except Exception as exc:  # a probe crash must not kill the companion
            payload, error = None, f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._scanning = False
            self.report = payload
            self.error = error
            import datetime as _dt

            self.finished_at = _dt.datetime.now().astimezone().isoformat(timespec="seconds")


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ClaudeEnvGuard/1.0"
    state: _ScanState = None  # injected by serve()
    html_bytes: bytes = b""

    def log_message(self, fmt, *args):
        pass  # quiet: the console is a status line, not an access log

    def _send(self, status, body, content_type, extra=None):
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("cache-control", "no-store")
        # Allow any origin so a file:// copy of the page can also read the API
        # when the companion happens to be running. Same-origin serving is the
        # primary path; this is a best-effort bonus, not a dependency.
        self.send_header("access-control-allow-origin", "*")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""

        if path in ("/", "/index.html"):
            if not self.html_bytes:
                self._send(500, b"page missing", "text/plain; charset=utf-8")
                return
            self._send(200, self.html_bytes, "text/html; charset=utf-8")
            return

        if path == "/api/system":
            if "refresh=1" in query:
                self.state.start()
            payload = {
                "ready": self.state.report is not None or self.state.error is not None,
                "scanning": self.state.scanning,
                "finishedAt": self.state.finished_at,
            }
            if self.state.error is not None:
                payload["error"] = self.state.error
            if self.state.report is not None:
                payload["report"] = self.state.report
            self._send(
                200,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")


def _pick_port():
    for port in range(DEFAULT_PORT, DEFAULT_PORT + _PORT_ATTEMPTS):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return None



def start(open_browser=True):
    """Start the companion without blocking.

    Split out of `serve` so the windowed build can own the lifetime instead: the
    console version waits on a thread, the Claude-styled control window waits on
    tkinter, and both drive the SAME server object. Returns
    `(http_server, url, scan_state)`, or None when the page or a port is missing.
    """
    html_path = _html_path()
    if html_path is None:
        return None
    with open(html_path, "rb") as handle:
        html_bytes = handle.read()

    port = _pick_port()
    if port is None:
        return None

    state = _ScanState()
    _Handler.state = state
    _Handler.html_bytes = html_bytes

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}/"

    state.start()
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # Open immediately: the page polls /api/system until the scan lands, so the
    # user sees the browser checks render while the system half is still running.
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return server, url, state


def serve(open_browser=True):
    """Console flavour: start, print the URL, block until Ctrl+C."""
    started = start(open_browser=open_browser)
    if started is None:
        print(_MSG_NO_PAGE)
        return 1
    server, url, _state = started
    print(_MSG_READY.format(url=url))
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0
