#!/usr/bin/env python3
"""Transparent HTTP logging proxy for the Grodex model endpoint.

Point Grodex at this instead of the real API:

    GRODEX_API_ENDPOINT=http://127.0.0.1:8899/v1 grodex serve

Every request body is appended to a JSONL log so we can inspect exactly what
the agent sends, instead of guessing from the source. Streaming responses are
relayed chunk-by-chunk so SSE behaviour is preserved.

    python3 scripts/http_probe_proxy.py [--port 8899] [--log /tmp/bodies.jsonl]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "https://api.deepseek.com/v1"
LOG_PATH = "/tmp/grodex_bodies.jsonl"

_HOP_BY_HOP = {"transfer-encoding", "connection", "content-length", "keep-alive"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence default stderr noise
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        entry = {
            "path": self.path,
            "body": None,
            "messages": None,
            "stream": None,
            "model": None,
            "reasoning_fields": [],
        }
        try:
            parsed = json.loads(body)
            entry["body"] = parsed
            entry["model"] = parsed.get("model")
            entry["stream"] = parsed.get("stream")
            msgs = parsed.get("messages") or parsed.get("input") or []
            entry["messages"] = msgs
            for i, m in enumerate(msgs):
                if isinstance(m, dict) and "reasoning_content" in m:
                    entry["reasoning_fields"].append(
                        {"index": i, "role": m.get("role"), "len": len(m.get("reasoning_content") or "")}
                    )
        except Exception:
            entry["raw_prefix"] = body[:500].decode("utf-8", "replace")

        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in _HOP_BY_HOP and k.lower() != "host"
        }
        req = urllib.request.Request(
            UPSTREAM + self.path, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() in _HOP_BY_HOP:
                        continue
                    self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = resp.read(1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"upstream_status": exc.code,
                                     "upstream_body": payload.decode("utf-8", "replace")[:2000]},
                                    ensure_ascii=False) + "\n")
            self.send_response(exc.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:  # pragma: no cover - diagnostic path
            print(f"[proxy] upstream error: {exc}", file=sys.stderr)
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()


def main() -> int:
    global LOG_PATH, UPSTREAM

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--log", default=LOG_PATH)
    ap.add_argument("--upstream", default=UPSTREAM)
    args = ap.parse_args()

    LOG_PATH = args.log
    UPSTREAM = args.upstream
    open(LOG_PATH, "w").close()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[proxy] 127.0.0.1:{args.port} -> {UPSTREAM}  (log: {LOG_PATH})", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
