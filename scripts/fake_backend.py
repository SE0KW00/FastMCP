"""A stand-in for the real search API, for running the MCP server locally.

Serves the index list and the four retrieval endpoints with canned data, and
prints the credential headers it received so you can confirm they are being
forwarded. Dependency-free — plain standard library.

    python scripts/fake_backend.py [port]     # default 9000
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

INDICES = [
    {"name": "faq", "description": "Customer FAQ articles", "doc_count": 3},
    {"name": "manuals", "description": "Product manuals and setup guides", "doc_count": 12},
]

METHODS = frozenset({"bm25", "knn", "cc", "rrf"})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default access log; each handler logs its own line."""

    def _trace(self, body: dict[str, Any] | None = None) -> None:
        print(
            f"[backend] {self.command} {self.path}"
            f"  Authorization={self.headers.get('Authorization')!r}"
            f"  X-API-Key={self.headers.get('X-API-Key')!r}" + (f"  body={body}" if body else ""),
            flush=True,
        )

    def _reply(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._trace()
        if self.path.rstrip("/") == "/indices":
            self._reply(INDICES)
        else:
            self._reply({"error": f"unknown endpoint {self.path}"}, status=404)

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0)
        request = json.loads(raw or b"{}")
        self._trace(request)

        method = self.path.rstrip("/").removeprefix("/retrieve-")
        if method not in METHODS:
            self._reply({"error": f"unknown endpoint {self.path}"}, status=404)
            return

        query = request.get("query", "")
        top_k = int(request.get("top_k", 5))
        # The server sends the indices as one comma-joined string under a
        # capital-I key; split it back so the canned hits look plausible.
        indices = [name for name in request.get("Index_name", "").split(",") if name]
        groups = request.get("permission_groups", [])
        self._reply(
            {
                "documents": [
                    {
                        "_id": f"{method}-{rank}",
                        "_score": round(3.0 - rank * 0.4, 2),
                        "text": f"[{method}] result {rank} for {query!r}",
                        "_source": {
                            "index": indices[(rank - 1) % len(indices)] if indices else None,
                            "permission_groups": groups,
                            "rank": rank,
                        },
                    }
                    for rank in range(1, top_k + 1)
                ]
            }
        )


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    print(f"fake search backend on http://127.0.0.1:{port}", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
