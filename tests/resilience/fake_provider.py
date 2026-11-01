import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _event(content):
    payload = {
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


class FakeProvider:
    def __init__(self, port=0):
        self.lock = threading.Lock()
        self.started = 0
        self.finished = 0
        self.disconnected = 0
        self.in_flight = 0
        self.peak_in_flight = 0
        self.delay_seconds = 0.0
        self.fragment = False
        self.answer = "yes"
        self.authorizations = set()
        self.model_spans = {}

        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path == "/__config":
                    provider.configure(self)
                else:
                    provider.handle(self)

            def do_GET(self):
                provider.report(self)

        self.server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def configure(self, handler):
        length = int(handler.headers.get("Content-Length", "0"))
        for name, value in json.loads(handler.rfile.read(length)).items():
            setattr(self, name, value)
        handler.send_response(204)
        handler.end_headers()

    def report(self, handler):
        with self.lock:
            body = json.dumps(
                {
                    "started": self.started,
                    "finished": self.finished,
                    "disconnected": self.disconnected,
                    "in_flight": self.in_flight,
                    "authorizations": sorted(self.authorizations),
                    "model_spans": self.model_spans,
                }
            ).encode()
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def handle(self, handler):
        with self.lock:
            self.started += 1
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            remaining = int(handler.headers.get("Content-Length", "0"))
            body = bytearray()
            while remaining:
                chunk = handler.rfile.read(min(remaining, 1 << 20))
                remaining -= len(chunk)
                if len(body) < 1 << 20:
                    body += chunk
            match = re.search(rb'"model"\s*:\s*"([^"]+)"', body)
            model = match.group(1).decode() if match else ""
            with self.lock:
                self.authorizations.add(handler.headers.get("Authorization", ""))
                first, _ = self.model_spans.get(model, (time.monotonic(), 0))
                self.model_spans[model] = (first, time.monotonic())
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.close_connection = True
            deadline = time.monotonic() + self.delay_seconds
            while time.monotonic() < deadline:
                handler.wfile.write(b": keep-alive\n\n")
                handler.wfile.flush()
                time.sleep(0.2)
            body = _event(self.answer) + b"data: [DONE]\n\n"
            if self.fragment:
                for index in range(len(body)):
                    handler.wfile.write(body[index : index + 1])
                    handler.wfile.flush()
                    time.sleep(0.002)
            else:
                handler.wfile.write(body)
                handler.wfile.flush()
            with self.lock:
                self.finished += 1
                first, _ = self.model_spans[model]
                self.model_spans[model] = (first, time.monotonic())
        except (BrokenPipeError, ConnectionResetError):
            with self.lock:
                self.disconnected += 1
        finally:
            with self.lock:
                self.in_flight -= 1


if __name__ == "__main__":
    with FakeProvider(port=8000):
        threading.Event().wait()
