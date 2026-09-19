"""Minimal ACP-over-stdio driver for `grodex serve`.

Grodex has no one-shot CLI flag, so the only headless entry point is the ACP
server: `grodex serve` speaks newline-delimited JSON on stdin/stdout. This
module drives that protocol from Python so the eval harness can run tasks
without touching the Grodex source tree.

Wire format (see grodex-protocol/src/transport.rs):

  client -> server
    {"frame_type":"command","type":"Prompt", ...SessionPrompt fields}
    {"frame_type":"command","type":"ResolveApproval", ...ResolveApprovalCommand}
    {"frame_type":"ack","last_consumed_seq":N}
    {"frame_type":"ping","sent_at_ms":N}

  server -> client
    {"frame_type":"event", "seq":N, "content":{"type":...}, ...}
    {"frame_type":"snapshot", ...}
    {"frame_type":"flow_control"/"ping"/"pong"/"protocol_error", ...}

Two protocol details that matter for a headless driver:

1. ``Prompt`` ignores its ``session_id`` field. ``route_command`` reads only
   ``text``/``mode`` and forwards ``StartTurn`` to the session the server
   booted with, so a placeholder UUID is safe. We still must send a *valid*
   UUID because ``SessionId(Uuid)`` fails deserialisation otherwise, and a
   failed parse kills the whole frame with a ``PROTOCOL_ERROR``.

2. The server applies backpressure: once ``inflight = next_seq -
   last_consumed_seq`` reaches the cap it stops emitting events until the
   client acknowledges. A driver that never ACKs will appear to hang.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# Must be a syntactically valid UUID; the value is ignored server-side.
PLACEHOLDER_SESSION = "00000000-0000-0000-0000-000000000000"

DEFAULT_BINARY = Path(
    os.environ.get(
        "GRODEX_BINARY",
        Path.home() / "Documents/个人项目/grodex/grodex/target/debug/grodex",
    )
)

# Content types emitted by UpdateContent (grodex-protocol/src/acp.rs).
FINAL_TYPES = {"TurnComplete"}
APPROVAL_TYPE = "RequestPermission"


@dataclass
class Event:
    """One decoded `ServerFrame` carrying a session update."""

    seq: int
    type: str
    payload: dict
    t: float

    @property
    def text(self) -> str:
        return self.payload.get("text", "") or ""


@dataclass
class TurnResult:
    """Everything observable about one prompt -> TurnComplete cycle."""

    prompt: str
    events: list[Event] = field(default_factory=list)
    text: str = ""
    tool_calls: list[tuple[str, str]] = field(default_factory=list)  # (call_id, name)
    tool_results: list[tuple[str, bool]] = field(default_factory=list)  # (call_id, is_error)
    approvals: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    protocol_errors: list[str] = field(default_factory=list)
    input_tokens: int = 0
    cached_tokens: int = 0
    turns_completed: int = 0
    duration_s: float = 0.0
    timed_out: bool = False
    stop_reason: str = "unknown"  # turn_complete | timeout | exited
    exit_code: int | None = None

    @property
    def tool_error_count(self) -> int:
        return sum(1 for _, is_err in self.tool_results if is_err)

    @property
    def cost_proxy_tokens(self) -> int:
        return self.input_tokens


class AcpError(RuntimeError):
    pass


class AcpDriver:
    """Spawns `grodex serve` and exchanges ACP frames with it."""

    def __init__(
        self,
        cwd: str | Path,
        binary: str | Path = DEFAULT_BINARY,
        env: dict | None = None,
        approval_policy: str = "always_allow",
    ) -> None:
        self.cwd = str(cwd)
        self.binary = str(binary)
        self.env = env
        self.approval_policy = approval_policy
        self.proc: subprocess.Popen | None = None
        self._rx: queue.Queue[dict | None] = queue.Queue()
        self._stderr: list[str] = []
        self._lock = threading.Lock()
        self._prompt_seq = 0

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self, startup_timeout: float = 60.0) -> None:
        if not Path(self.binary).exists():
            raise AcpError(f"grodex binary not found: {self.binary}")
        self.proc = subprocess.Popen(
            [self.binary, "serve"],
            cwd=self.cwd,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._rx.put(json.loads(line))
            except json.JSONDecodeError:
                self._rx.put({"frame_type": "_unparsed", "raw": line[:400]})
        self._rx.put(None)  # EOF sentinel

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            line = line.strip()
            if line:
                self._stderr.append(line)

    def close(self, grace: float = 3.0) -> None:
        if not self.proc:
            return
        # Closing stdin makes the server's read loop hit EOF and shut down
        # gracefully (it then flushes rollout extraction before exiting).
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=grace)

    def __enter__(self) -> "AcpDriver":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def stderr_text(self) -> str:
        return "\n".join(self._stderr)

    # ── framing ──────────────────────────────────────────────────────

    def _send(self, obj: dict) -> None:
        if not self.proc or not self.proc.stdin:
            raise AcpError("driver not started")
        with self._lock:
            self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()

    def _next_command_id(self, kind: str) -> str:
        self._prompt_seq += 1
        return f"eval-{kind}-{self._prompt_seq}-{uuid.uuid4().hex[:8]}"

    def _ack(self, seq: int) -> None:
        try:
            self._send({"frame_type": "ack", "last_consumed_seq": seq})
        except (AcpError, OSError, ValueError):
            pass  # pipe closed; the read loop will surface the real reason

    def _resolve_approval(self, ticket_id: str, resolution: str) -> None:
        self._send(
            {
                "frame_type": "command",
                "type": "ResolveApproval",
                "command_id": self._next_command_id("approve"),
                "ticket_id": ticket_id,
                "resolution": resolution,
                "issued_by": "grodex-eval",
                "issued_at_ms": int(time.time() * 1000),
            }
        )

    # ── the interesting part ─────────────────────────────────────────

    def prompt(
        self,
        text: str,
        timeout: float = 900.0,
        mode: str | None = None,
    ) -> TurnResult:
        """Send one prompt and block until the Turn completes or times out."""
        if not self.is_alive():
            raise AcpError("grodex serve is not running")
        res = TurnResult(prompt=text)
        cmd = {
            "frame_type": "command",
            "type": "Prompt",
            "command_id": self._next_command_id("prompt"),
            "idempotency_key": uuid.uuid4().hex,
            "session_id": PLACEHOLDER_SESSION,
            "text": text,
        }
        if mode:
            cmd["mode"] = mode
        self._send(cmd)

        deadline = time.monotonic() + timeout
        last_seq = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                res.timed_out = True
                res.stop_reason = "timeout"
                break
            try:
                frame = self._rx.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if not self.is_alive():
                    res.stop_reason = "exited"
                    res.exit_code = self.proc.poll() if self.proc else None
                    break
                continue
            if frame is None:
                res.stop_reason = "exited"
                res.exit_code = self.proc.poll() if self.proc else None
                break

            kind = frame.get("frame_type")
            if kind == "event":
                ev = Event(
                    seq=int(frame.get("seq", 0)),
                    type=(frame.get("content") or {}).get("type", "?"),
                    payload=frame.get("content") or {},
                    t=time.monotonic(),
                )
                res.events.append(ev)
                if ev.seq > last_seq:
                    last_seq = ev.seq
                self._handle(ev, res)
                self._ack(last_seq)
                if ev.type in FINAL_TYPES:
                    res.turns_completed += 1
                    res.stop_reason = "turn_complete"
                    break
            elif kind == "protocol_error":
                res.protocol_errors.append(
                    f"{frame.get('code')}: {frame.get('message')}"
                )
                if frame.get("reference_command_id") == cmd["command_id"]:
                    res.stop_reason = "protocol_error"
                    break
            elif kind == "flow_control":
                pass  # informational; our per-event ACK keeps the window open
            elif kind == "_unparsed":
                res.protocol_errors.append(f"unparsed stdout: {frame.get('raw')}")

        res.duration_s = round(timeout - (deadline - time.monotonic()), 3)
        return res

    def _handle(self, ev: Event, res: TurnResult) -> None:
        if ev.type == "TextDelta":
            res.text += ev.text
        elif ev.type == "ToolCallStart":
            res.tool_calls.append((ev.payload.get("call_id", ""), ev.payload.get("name", "")))
        elif ev.type == "ToolResult":
            res.tool_results.append(
                (ev.payload.get("call_id", ""), bool(ev.payload.get("is_error")))
            )
        elif ev.type == "Error":
            res.errors.append(ev.payload.get("message", ""))
        elif ev.type == APPROVAL_TYPE:
            resolution = self.approval_policy
            res.approvals.append(
                {
                    "ticket_id": ev.payload.get("ticket_id", ""),
                    "tool_name": ev.payload.get("tool_name", ""),
                    "risk": ev.payload.get("risk", ""),
                    "resolution": resolution,
                }
            )
            try:
                self._resolve_approval(ev.payload.get("ticket_id", ""), resolution)
            except (AcpError, OSError, ValueError):
                pass
        elif ev.type == "TurnComplete":
            res.input_tokens += int(ev.payload.get("input_tokens", 0) or 0)
            res.cached_tokens += int(ev.payload.get("cached_tokens", 0) or 0)
