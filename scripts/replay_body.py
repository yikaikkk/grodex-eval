#!/usr/bin/env python3
"""Replay captured request bodies against the live API, with ablation.

Reads bodies logged by scripts/http_probe_proxy.py and re-POSTs them, so the
system prompt, tool list and top-level params are byte-for-byte what Grodex
actually sent. Ablations then isolate which field matters.

    python3 scripts/replay_body.py /tmp/grodex_bodies.jsonl 3
"""

from __future__ import annotations

import copy
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = Path.home() / ".grodex" / "config.toml"


def read_config(section: str = "") -> dict:
    cfg: dict = {}
    current = ""
    for raw in CONFIG.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = line.strip("[]").strip()
            continue
        if "=" not in line or current != section:
            continue
        k, v = line.split("=", 1)
        v = v.split("#", 1)[0].strip().strip('"')
        if k.strip() in ("api_key", "endpoint", "model"):
            cfg[k.strip()] = v
    return cfg


def post(cfg: dict, body: dict) -> tuple[int, str]:
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        cfg["endpoint"].rstrip("/") + "/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            resp.read()
            return resp.status, ""
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, (json.loads(raw).get("error") or {}).get("message", "")[:160]
        except json.JSONDecodeError:
            return e.code, raw[:160]
    except Exception as e:
        return 0, repr(e)


def strip_reasoning(body: dict) -> dict:
    b = copy.deepcopy(body)
    for m in b.get("messages") or []:
        if isinstance(m, dict):
            m.pop("reasoning_content", None)
    return b


def main() -> int:
    log = sys.argv[1] if len(sys.argv) > 1 else "/tmp/grodex_bodies.jsonl"
    idx = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    cfg = read_config()
    rows = [json.loads(l) for l in open(log, encoding="utf-8") if l.strip()]
    body = rows[idx].get("body")
    if not body:
        print(f"row {idx} has no full body")
        return 1

    tcs = [t.get("function", {}).get("name") for t in (body.get("tools") or [])]
    print(f"replaying row {idx}: model={body.get('model')} stream={body.get('stream')} "
          f"msgs={len(body.get('messages') or [])} tools={len(tcs)} tool_choice={body.get('tool_choice')}")
    print(f"endpoint: {cfg.get('endpoint')}\n")

    # Grodex always streams; the API still returns a plain HTTP error for a
    # rejected request, so the status is comparable across variants.
    variants = [
        ("verbatim (reasoning_content present)", body),
        ("reasoning_content removed", strip_reasoning(body)),
    ]

    # Also try the same two, non-streaming, to see whether stream flips it.
    for label, b in list(variants):
        b2 = copy.deepcopy(b)
        b2["stream"] = False
        variants.append((f"{label} + stream=false", b2))

    print(f"{'variant':48} {'status':>6}  detail")
    print("-" * 92)
    out = {}
    for label, b in variants:
        st, detail = post(cfg, b)
        out[label] = st
        print(f"{label:48} {st:>6}  {detail}")

    print("\n=== verdict ===")
    v_ok = out["verbatim (reasoning_content present)"]
    v_no = out["reasoning_content removed"]
    if v_ok == 200 and v_no == 400:
        print("CONFIRMED: this request is accepted with reasoning_content and rejected")
        print("without it. Dropping the field is the trigger for the 400.")
    elif v_ok == 200 and v_no == 200:
        print("NOT confirmed: dropping reasoning_content is accepted here.")
        print("=> the 400 needs something else on top (e.g. the response genuinely")
        print("   contained reasoning that Grodex failed to record).")
    else:
        print(f"inconclusive: verbatim={v_ok} removed={v_no}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
