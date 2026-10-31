"""Vercel serverless endpoint: the profile card, rendered on request.

    GET /api/card          -> SVG for CARD_LOGIN (default touhi99)
    GET /api/card?user=... -> 404 for any other login

The renderer lives in stats/generate.py, so the CLI and this endpoint cannot
drift. Set GH_TOKEN (classic PAT with `repo` scope) in the Vercel project's
environment to include private repos and private contributions.

Caching: GitHub's image proxy and Vercel's edge both honour s-maxage, so the
upstream GitHub API is hit at most about once an hour per cache region.
"""

from __future__ import annotations

import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "stats")
)
import generate  # noqa: E402

LOGIN = os.environ.get("CARD_LOGIN", "touhi99")
CACHE = "public, s-maxage=3600, stale-while-revalidate=86400"


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server's spelling)
        params = parse_qs(urlparse(self.path).query)
        user = (params.get("user") or [LOGIN])[0]
        if user != LOGIN:
            return self._send(404, b"unknown user\n", "text/plain; charset=utf-8")

        tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not tok:
            return self._send(500, b"GH_TOKEN is not set\n", "text/plain; charset=utf-8")

        try:
            data = generate.fetch(user, tok)
            data["login"] = user
            svg = generate.build(data)
        except SystemExit as exc:  # generate.py exits on API errors
            return self._send(502, f"{exc}\n".encode(), "text/plain; charset=utf-8")
        except Exception as exc:  # noqa: BLE001 - report, do not crash the function
            return self._send(
                502, f"render failed: {exc}\n".encode(), "text/plain; charset=utf-8"
            )

        self._send(200, svg.encode(), "image/svg+xml; charset=utf-8", CACHE)

    def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        pass
