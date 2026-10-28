from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return
        print(json.dumps({"received_webhook": True, "path": self.path, "payload": payload}, ensure_ascii=False), flush=True)
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"accepted":true}')

    def log_message(self, format, *args):  # noqa: A002
        print(f"mock-webhook: {format % args}", flush=True)


def main():
    host = os.getenv("MOCK_WEBHOOK_HOST", "127.0.0.1")
    port = int(os.getenv("MOCK_WEBHOOK_PORT", "9001"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Mock webhook listening on http://{host}:{port}/webhook", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
