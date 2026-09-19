#!/usr/bin/env python3
"""Smoke test: can we drive `grodex serve` headlessly over ACP?

Costs one real model call. Run manually:

    python3 scripts/smoke_acp.py [SANDBOX_DIR]

Environment overrides (both are applied by rewriting the sandbox's config.toml,
because grodex resolves those settings from the file first — `GRODEX_API_ENDPOINT`
and `GRODEX_MODEL` are only fallbacks via `.or_else`):

    GRODEX_EVAL_PROXY=http://127.0.0.1:8899/v1   point the endpoint at a proxy
    GRODEX_EVAL_MODEL=deepseek-chat              use a non-thinking model
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grodex_eval.acp import AcpDriver  # noqa: E402
from grodex_eval.sandbox import create_sandbox  # noqa: E402


def patch_config(sb, updates: dict) -> None:
    """Rewrite top-level keys of the sandbox's config.toml."""
    cfg = sb.grodex_home / "config.toml"
    pending = dict(updates)
    out, in_top = [], True
    for line in cfg.read_text(encoding="utf-8").splitlines(keepends=True):
        s = line.strip()
        if s.startswith("["):
            in_top = False
        elif in_top and "=" in s:
            key = s.split("=", 1)[0].strip()
            if key in pending:
                line = f'{key} = "{pending.pop(key)}"\n'
        out.append(line)
    if pending:
        out.append("\n" + "".join(f'{k} = "{v}"\n' for k, v in pending.items()))
    cfg.write_text("".join(out), encoding="utf-8")


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/grodex-eval-smoke3")
    print(f"sandbox: {root}")
    sb = create_sandbox(root, copy_workspace_from=None)
    (sb.workspace / "hello.txt").write_text("the magic word is PONG\n", encoding="utf-8")

    updates = {}
    proxy = os.environ.get("GRODEX_EVAL_PROXY")
    if proxy:
        updates["endpoint"] = proxy
    model = os.environ.get("GRODEX_EVAL_MODEL")
    if model:
        updates["model"] = model
    if updates:
        patch_config(sb, updates)
        print("config overrides: " + ", ".join(f"{k}={v}" for k, v in updates.items()))

    driver = AcpDriver(cwd=sb.workspace, env=sb.env(), approval_policy="always_allow")
    t0 = time.monotonic()
    driver.start()
    print(f"spawned in {time.monotonic() - t0:.2f}s; waiting for prompt…")

    try:
        res = driver.prompt(
            "Read hello.txt and reply with exactly the magic word it contains, "
            "nothing else.",
            timeout=180,
        )
    finally:
        driver.close()

    print("\n=== RESULT ===")
    print(f"stop_reason     : {res.stop_reason}")
    print(f"duration        : {res.duration_s}s")
    print(f"events          : {len(res.events)}")
    print(f"tool calls      : {[n for _, n in res.tool_calls]}")
    print(f"tool errors     : {res.tool_error_count}")
    print(f"approvals       : {len(res.approvals)}")
    print(f"input tokens    : {res.input_tokens}")
    print(f"errors          : {res.errors}")
    print(f"protocol errors : {res.protocol_errors}")
    print(f"text            : {res.text[:400]!r}")

    if driver.stderr_text:
        tail = driver.stderr_text.splitlines()[-12:]
        print("\n=== stderr (tail) ===")
        print("\n".join(tail))

    ok = res.stop_reason == "turn_complete" and "PONG" in res.text.upper()
    print(f"\nSMOKE: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
