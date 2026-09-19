#!/usr/bin/env python3
"""Pin down which assistant-message shape the API rejects with
"reasoning_content in the thinking mode must be passed back".

Grodex builds the post-tool assistant message in two shapes
(grodex-sampler/src/client.rs):

    Assistant{content} + ToolCall  -> {"content": <content>, ...}   (line 512)
    ToolCall with no Assistant     -> {"content": null,  ...}       (line 541)

and only attaches `reasoning_content` when the accumulated reasoning is
non-empty (lines 519 / 548). This script replays each shape against the live
API to find the one that 400s.

    python3 scripts/probe_reasoning.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = Path.home() / ".grodex" / "config.toml"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
]


def read_config(section: str = "") -> dict:
    """Read scalar keys from one TOML section (default: the top-level one)."""
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
        if k.strip() in ("api_key", "endpoint", "model", "provider", "wire_protocol"):
            cfg[k.strip()] = v
    return cfg


def call(cfg: dict, messages: list, stream: bool = False) -> tuple[int, dict]:
    body = json.dumps(
        {"model": cfg["model"], "messages": messages, "tools": TOOLS, "stream": stream}
    ).encode()
    req = urllib.request.Request(
        cfg["endpoint"].rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:300]}
    except Exception as e:  # network hiccup — not part of the experiment
        return 0, {"exception": repr(e)}


def err_of(js: dict) -> str:
    e = js.get("error") or {}
    return (e.get("message") or js.get("raw") or "")[:150]


def main() -> int:
    cfg = read_config()
    print(f"endpoint: {cfg.get('endpoint')}   model: {cfg.get('model')}\n")

    user = {"role": "user", "content": "Read the file hello.txt and tell me what it says."}
    msgs = [user]

    print("[warm-up] getting a real tool_call id…")
    status, js = call(cfg, msgs)
    if status != 200:
        print(f"  warm-up failed: {status} {err_of(js)}")
        return 1
    msg = js["choices"][0]["message"]
    print(f"  content={msg.get('content')!r}")
    print(f"  reasoning_content present={('reasoning_content' in msg)} "
          f"repr={msg.get('reasoning_content')!r}")
    print(f"  tool_calls={[t['function']['name'] for t in msg.get('tool_calls') or []]}")
    print(f"  usage={js.get('usage')}\n")

    tc = (msg.get("tool_calls") or [None])[0]
    if not tc:
        print("  no tool call returned; cannot continue")
        return 1
    call_spec = [
        {
            "id": tc["id"],
            "type": "function",
            "function": {
                "name": tc["function"]["name"],
                "arguments": tc["function"]["arguments"],
            },
        }
    ]
    tool_msg = {"role": "tool", "tool_call_id": tc["id"], "content": "the magic word is PONG"}

    def assistant(content, reasoning=..., extra=None):
        m = {"role": "assistant", "content": content, "tool_calls": call_spec}
        if reasoning is not ...:
            m["reasoning_content"] = reasoning
        if extra:
            m.update(extra)
        return m

    variants = [
        ("A  content=null, no reasoning   (paths 541/548)",
         assistant(None), False),
        ("B  content=null, reasoning=\"\"",
         assistant(None, ""), False),
        ("C  content=\"\", no reasoning",
         assistant(""), False),
        ("D  content=null, reasoning=text",
         assistant(None, "I should read the file."), False),
        ("E  content=\"hello\", no reasoning (path 512)",
         assistant("I'll read that file."), False),
        ("F  A but stream=true",
         assistant(None), True),
    ]

    print(f"{'variant':52} {'status':>6}  detail")
    print("-" * 96)
    results = {}
    for label, amsg, stream in variants:
        st, js = call(cfg, msgs + [amsg, tool_msg], stream=stream)
        results[label[0]] = st
        detail = "" if st == 200 else err_of(js)
        print(f"{label:52} {st:>6}  {detail}")

    print("\n=== verdict ===")
    bad = [k for k, v in results.items() if v == 400]
    good = [k for k, v in results.items() if v == 200]
    if bad and good:
        print(f"400 on {sorted(bad)};  200 on {sorted(good)}")
        if "A" in bad and "F" not in bad and "E" not in bad:
            print("=> The trigger is the assistant message with content=null and no")
            print("   reasoning_content — exactly the shape Grodex emits for a")
            print("   tool-call-only step (client.rs:541-550).")
    elif all(v == 200 for v in results.values()):
        print("all variants accepted — content/reasoning shape is NOT the trigger")
    else:
        print(f"results={results}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
