"""Dependency-free P0 startup scaffold.

This is deliberately not the P1 application runtime.  It gives the repository a
real, locally startable process and a health endpoint while the runtime contract
is still pending.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


PAYLOAD = {
    "service": "ppt-agent-mvp",
    "stage": "P0",
    "status": "ok",
    "runtime_ready": False,
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path not in ("/", "/healthz"):
            self.send_error(404)
            return
        body = json.dumps(PAYLOAD, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert PAYLOAD["stage"] == "P0" and PAYLOAD["status"] == "ok"
        print("P0 最小启动入口自检通过")
        return 0
    print(f"P0 scaffold listening on http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
