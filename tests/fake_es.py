"""A tiny fake Elasticsearch HTTP server (just enough of /, /_bulk, /<index> HEAD) for tests."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeES:
    def __init__(self):
        self.docs = {}                  # id -> source
        self.bulk_calls = 0
        self.fail_first_n_bulks = 0     # respond 503 to the first N bulk requests
        self.item_503_once = set()      # doc ids that get a 429 item error the first time
        self.reject_ids = set()         # doc ids that get a 400 mapping error
        self.always_503 = False
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"{}"):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Elastic-Product", "Elasticsearch")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_HEAD(self):
                self._send(200)

            def do_GET(self):
                self._send(200, json.dumps({"name": "fake", "cluster_name": "fake",
                                            "version": {"number": "8.19.18", "build_flavor": "default"},
                                            "tagline": "You Know, for Search"}).encode())

            def do_PUT(self):
                self.do_POST()

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n).decode()
                if not self.path.split("?")[0].endswith("/_bulk"):
                    return self._send(404)
                outer.bulk_calls += 1
                if outer.always_503 or outer.bulk_calls <= outer.fail_first_n_bulks:
                    return self._send(503, b'{"error":"unavailable"}')
                lines = [json.loads(x) for x in raw.splitlines() if x.strip()]
                items = []
                for meta, src in zip(lines[0::2], lines[1::2]):
                    _id = meta["index"]["_id"]
                    if _id in outer.reject_ids:
                        items.append({"index": {"_id": _id, "status": 400, "error": {"type": "mapper_parsing_exception"}}})
                    elif _id in outer.item_503_once:
                        outer.item_503_once.discard(_id)
                        items.append({"index": {"_id": _id, "status": 429, "error": {"type": "es_rejected_execution_exception"}}})
                    else:
                        outer.docs[_id] = src
                        items.append({"index": {"_id": _id, "status": 201, "result": "created"}})
                self._send(200, json.dumps({"took": 1, "errors": any(i["index"]["status"] >= 400 for i in items),
                                            "items": items}).encode())

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
