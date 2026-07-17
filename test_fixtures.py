"""Fake printer HTTP servers for end-to-end backend tests.

A stdlib ThreadingHTTPServer bound to 127.0.0.1:0 (ephemeral port), one handler
per protocol implementing just the endpoints the backend calls. Pointing a real
backend at it exercises the actual httpx client over a real socket — which
proves what httpx.MockTransport cannot: PrusaLink's HTTP Digest challenge, the
Duet session/disconnect lifecycle, and real multipart upload bodies.

No dependency beyond the stdlib. Used by test_server.py.
"""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Base(BaseHTTPRequestHandler):
    calls: list = []          # (method, path) log, per-server-class

    def log_message(self, *a):  # silence
        pass

    def _send(self, code, body=None, headers=None, raw=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        payload = raw if raw is not None else (
            json.dumps(body).encode() if body is not None else b"")
        if raw is None and body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    # subclasses override handle_get/handle_other
    def do_GET(self):
        type(self).calls.append(("GET", self.path.split("?")[0]))
        self.handle_get()

    def do_POST(self):
        type(self).calls.append(("POST", self.path.split("?")[0]))
        self.handle_other("POST")

    def do_PUT(self):
        type(self).calls.append(("PUT", self.path.split("?")[0]))
        self.handle_other("PUT")

    def do_DELETE(self):
        type(self).calls.append(("DELETE", self.path.split("?")[0]))
        self.handle_other("DELETE")


class MoonrakerHandler(_Base):
    def handle_get(self):
        if self.path.startswith("/printer/objects/query"):
            self._send(200, {"result": {"status": {
                "print_stats": {"state": "printing", "filename": "a.gcode",
                                "info": {"current_layer": 3, "total_layer": 50}},
                "extruder": {"temperature": 200.0, "target": 200},
                "heater_bed": {"temperature": 60.0, "target": 60},
                "virtual_sdcard": {"progress": 0.06}}}})
        else:
            self._send(404)

    def handle_other(self, method):
        if self.path.startswith("/server/files/upload"):
            body = self._body()
            # a real multipart body, over a real socket
            ok = b'name="file"' in body and b'name="print"' not in body
            self._send(201 if ok else 400,
                       {"print_started": False} if ok else {"error": "bad"})
        elif self.path.startswith("/printer/print/"):
            self._send(200, {"result": "ok"})
        else:
            self._send(404)


class OctoPrintHandler(_Base):
    def _auth_ok(self):
        return self.headers.get("X-Api-Key") == "testkey"

    def handle_get(self):
        p = self.path.split("?")[0]
        if not self._auth_ok():
            self._send(403, {"error": "no key"})
        elif p == "/api/printer":
            self._send(200, {"state": {"text": "Printing",
                                       "flags": {"printing": True}},
                             "temperature": {"tool0": {"actual": 205.0, "target": 205},
                                             "bed": {"actual": 60.0, "target": 60}}})
        elif p == "/api/job":
            self._send(200, {"job": {"file": {"name": "a.gcode"}},
                             "progress": {"completion": 12.0}})
        else:
            self._send(404)

    def handle_other(self, method):
        if not self._auth_ok():
            self._send(403, {"error": "no key"})
        elif self.path == "/api/files/local":
            body = self._body()
            ok = b'name="file"' in body and b'name="print"' not in body \
                and b'name="select"' not in body
            self._send(201 if ok else 400, {"done": ok})
        elif self.path == "/api/job":
            self._send(204)
        else:
            self._send(404)


class PrusaLinkHandler(_Base):
    """Issues a real HTTP Digest challenge and requires the resend."""

    def _has_digest(self):
        return self.headers.get("Authorization", "").startswith("Digest ")

    def _challenge(self):
        self._send(401, {"error": "auth"}, headers={
            "WWW-Authenticate": 'Digest realm="Printer API", '
            'nonce="abc123", qop="auth"'})

    def handle_get(self):
        if not self._has_digest():
            return self._challenge()
        if self.path == "/api/v1/status":
            self._send(200, {"printer": {"state": "PRINTING", "temp_nozzle": 210.0,
                                         "target_nozzle": 210, "temp_bed": 60.0,
                                         "target_bed": 60},
                             "job": {"id": 7, "progress": 25.0}})
        elif self.path == "/api/v1/job":
            self._send(200, {"file": {"display_name": "a.gcode"}})
        else:
            self._send(404)

    def handle_other(self, method):
        if not self._has_digest():
            return self._challenge()
        if method == "PUT" and self.path.startswith("/api/v1/files/"):
            # the safety invariant: upload must say do-not-print
            ok = self.headers.get("Print-After-Upload") == "?0"
            self._body()
            self._send(201 if ok else 400, {})
        elif self.path.startswith("/api/v1/files/"):   # POST = start
            self._send(204)
        else:
            self._send(404)


class DuetHandler(_Base):
    sessions = 0
    _lock = threading.Lock()

    def handle_get(self):
        p = self.path.split("?")[0]
        if p == "/rr_connect":
            with type(self)._lock:        # ThreadingHTTPServer is multi-thread
                type(self).sessions += 1
            self._send(200, {"err": 0, "sessionKey": 99})
        elif p == "/rr_disconnect":
            with type(self)._lock:
                type(self).sessions -= 1
            self._send(200, {"err": 0})
        elif p == "/rr_model":
            key = _param(self.path, "key")
            self._send(200, {"result": {
                "state": {"status": "processing"},
                "heat": {"bedHeaters": [0],
                         "heaters": [{"current": 60.0, "active": 60},
                                     {"current": 210.0, "active": 210}]},
                "job": {"layer": 4, "filePosition": 400,
                        "file": {"fileName": "a.gcode", "size": 800,
                                 "numLayers": 40}}}.get(key, {})})
        elif p == "/rr_gcode":
            self._send(200, {"buff": 240})
        else:
            self._send(404)

    def handle_other(self, method):
        if self.path.split("?")[0] == "/rr_upload":
            self._body()
            self._send(200, {"err": 0})
        else:
            self._send(404)


def _param(path, name):
    from urllib.parse import urlparse, parse_qs
    return parse_qs(urlparse(path).query).get(name, [None])[0]


@contextmanager
def fixture(handler_cls):
    """Run a fixture server on an ephemeral port; yields the port."""
    handler_cls.calls = []
    if hasattr(handler_cls, "sessions"):   # reset all per-class mutable state
        handler_cls.sessions = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
