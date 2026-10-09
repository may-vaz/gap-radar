"""ForgeOS zero-framework HTTP server. Serves the single-file UI and JSON API."""
from __future__ import annotations

import json
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from forgeos import ForgeError, ForgeOS

ROOT = Path(__file__).resolve().parent
APP = ForgeOS()
MAX_BODY = 1_000_000


class Handler(BaseHTTPRequestHandler):
    server_version = "ForgeOS/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[forgeos] {self.address_string()} {fmt % args}")

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ForgeError("Invalid Content-Length", 400)
        if length <= 0 or length > MAX_BODY:
            raise ForgeError("Request body is empty or too large", 413)
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            raise ForgeError("Malformed JSON", 400)
        if not isinstance(value, dict):
            raise ForgeError("JSON body must be an object", 400)
        return value

    def _route(self, method: str) -> None:
        path = urlparse(self.path).path
        if method == "GET" and path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            return
        if method == "GET" and path == "/api/health":
            return self._json(200, APP.health())
        if method == "GET" and path == "/api/projects":
            return self._json(200, {"projects": APP.store.list_projects()})
        match = re.fullmatch(r"/api/projects/([a-zA-Z0-9_-]+)", path)
        if method == "GET" and match:
            return self._json(200, APP.store.get_project(match.group(1)))
        if method == "POST" and path == "/api/compile":
            return self._json(201, APP.compile(self._body()))
        match = re.fullmatch(r"/api/projects/([a-zA-Z0-9_-]+)/heal", path)
        if method == "POST" and match:
            return self._json(201, APP.heal(match.group(1), self._body()))
        if method == "GET" and path in ("/", "/index.html"):
            return self._file(ROOT / "index.html")
        raise ForgeError("Route not found", 404)

    def _file(self, path: Path) -> None:
        if not path.is_file():
            raise ForgeError("File not found", 404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        try:
            self._route("GET")
        except ForgeError as exc:
            print(f"[forgeos] GET {self.path} failed ({exc.status}): {exc}", flush=True)
            self._json(exc.status, {"error": str(exc), "details": exc.details})
        except Exception as exc:
            print(f"[forgeos] unhandled GET error: {type(exc).__name__}: {exc}", flush=True)
            self._json(500, {"error": "Internal server error", "details": None})

    def do_POST(self) -> None:
        try:
            self._route("POST")
        except ForgeError as exc:
            print(f"[forgeos] POST {self.path} failed ({exc.status}): {exc}", flush=True)
            self._json(exc.status, {"error": str(exc), "details": exc.details})
        except Exception as exc:
            print(f"[forgeos] unhandled POST error: {type(exc).__name__}: {exc}", flush=True)
            self._json(500, {"error": "Internal server error", "details": None})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "4280"))
    print(f"ForgeOS running on http://0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
