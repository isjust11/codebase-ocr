"""HTTP health server tối giản cho readiness/liveness (Docker healthcheck, k8s).

GET /health → 200 khi engine đã warm-up xong, ngược lại 503.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

logger = logging.getLogger("ocr.health")


def start_health_server(port: int, is_ready: Callable[[], bool]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/") in ("/health", "/healthz", ""):
                ready = is_ready()
                self.send_response(200 if ready else 503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                body = b'{"status":"ok"}' if ready else b'{"status":"starting"}'
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):  # tắt log mặc định ồn ào
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health server chạy tại :%s/health", port)
    return server
