"""Metric computation over the telemetry projection.

Every section is a pure function of a :class:`~grodex_eval.db.Snapshot`.
Two rules are enforced throughout:

1. **Explicit denominators.** A rate is `None` when its denominator is zero,
   never `0.0`. "No data" and "zero percent" are different findings.
2. **Section isolation.** `collect()` runs each section in isolation so an
   unexpected schema change degrades one section instead of the whole report.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

from .db import EXPECTED_TABLES, Snapshot
from .stats import count_by, rate, summarize

# Tool statuses that mean "this call did not produce a usable result".
TOOL_ERROR_STATUSES = ("failed", "indeterminate")

# Tools whose execution changes state outside the workspace or spends budget.
# The security section uses this list to separate "the gate was never stamped"
# from "the tool never needed the gate".
GUARDED_TOOLS: tuple[str, ...] = (
    "exec",
    "write_file",
    "edit_file",
    "apply_patch",
    "delegate_task",
)


def collect(snap: Snapshot, prices: dict | None = None) -> dict[str, Any]:
    """Run every metric section, isolating failures per section."""
    sections: list[tuple[str, Callable[[Snapshot], dict]]] = [
        ("meta", meta),
        ("overview", overview),
        ("turn_health", turn_health),
        ("model_usage", model_usage),
        ("tool_usage", tool_usage),
        ("approvals", approvals),
        ("context", context),
        ("memory", memory),
        ("reliability", reliability),
        ("capabilities", capabilities),
        ("security", security),
        ("integrity", integrity),
    ]
    report: dict[str, Any] = {}
    for name, fn in sections:
        try:
            report[name] = fn(snap)
        except Exception as exc:  # noqa: BLE001 - surfaced into the report
            report[name] = {"error": f"{type(exc).__name__}: {exc}"}
    _fill_cross_section(report)
    report["cost"] = cost(report, prices or {})
    return report


def _fill_cross_section(report: dict[str, Any]) -> None:
    """Attach metrics that need two sections (e.g. per-turn normalisation)."""
    turns = (report.get("turn_health") or {}).get("turns") or 0
    if not turns:
        return
    for section, numerator in (("approvals", "approval_requests"), ("memory", "retrievals")):
        block = report.get(section)
        if not isinstance(block, dict) or "error" in block:
            continue
        key = "requests_per_turn" if section == "approvals" else "retrievals_per_turn"
        block[key] = rate(block.get(numerator), turns)


# ---------------------------------------------------------------------------
# meta / overview
# ---------------------------------------------------------------------------


def meta(snap: Snapshot) -> dict[str, Any]:
    import datetime as _dt
    import os

    row_counts = {}
    for table in EXPECTED_TABLES:
        row_counts[table] = snap.count(table)
    missing = [t for t in EXPECTED_TABLES if not snap.has_table(t)]
    return {
        "db_path": snap.path,
        "db_bytes": os.path.getsize(snap.path) if os.path.exists(snap.path) else None,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "window": snap.window.label,
        "window_since": snap.window.since,
        "window_until": snap.window.until,
        "row_counts": row_counts,
        "missing_tables": missing,
    }


def overview(snap: Snapshot) -> dict[str, Any]:
    sessions = snap.count("sessions")
    turns = snap.count("turns")
    events = snap.count("telemetry_events")
    models = [
        {"provider": r[0], "model": r[1], "calls": int(r[2])}
        for r in snap.select(
            "model_attempts",
            "provider, model, COUNT(*)",
            tail="GROUP BY provider, model ORDER BY COUNT(*) DESC",
        )
    ]
    first = snap.scalar(
        "SELECT MIN(COALESCE(started_at, finished_at)) FROM sessions"
    )
    last = snap.scalar("SELECT MAX(occurred_at) FROM telemetry_events")
    returns = {
        "sessions": sessions,
        "turns": turns,
        "events": events,
        "turns_per_session": rate(turns, sessions),
        "models": models,
        "first_session_at": first,
        "last_activity_at": last,
    }
    if returns["turns_per_session"] is not None:
        returns["turns_per_session"] = float(returns["turns_per_session"])
    return returns


# ---------------------------------------------------------------------------
# turn health — the headline "did the agent finish the job" signal
# ---------------------------------------------------------------------------


def turn_health(snap: Snapshot) -> dict[str, Any]:
    total = snap.count("turns")
    status = {k[0]: v for k, v in snap.group_counts("turns", "status").items()}
    reasons = {(k[0] or "-"): v for k, v in snap.group_counts("turns", "termination_reason").items()}
    completed = status.get("completed", 0)

    def reason(name: str) -> int:
        return reasons.get(name, 0)

    durations = snap.series("turns", "duration_ms")
    steps = snap.series("turns", "steps")
    model_calls = snap.series("turns", "model_calls")
    tool_calls = snap.series("turns", "tool_calls")
    compactions = snap.series("turns", "compactions")

    with_compaction = sum(1 for v in compactions if v > 0)
    return {
        "turns": total,
        "status": status,
        "termination_reasons": reasons,
        # A turn that ends on `final_answer` is the only unambiguous success.
        "final_answer_rate": rate(reason("final_answer"), total),
        "repair_exhausted_rate": rate(reason("repair_exhausted"), total),
        "sampling_error_rate": rate(reason("sampling_error"), total),
        "cancel_rate": rate(status.get("cancelled"), total),
        "completed_rate": rate(completed, total),
        "still_running": status.get("running", 0),
        "duration_ms": summarize(durations),
        "steps_per_turn": summarize(steps),
        "model_calls_per_turn": summarize(model_calls),
        "tool_calls_per_turn": summarize(tool_calls),
        "turns_with_compaction": with_compaction,
        "compaction_turn_rate": rate(with_compaction, total),
    }


# ---------------------------------------------------------------------------
# model usage
# ---------------------------------------------------------------------------


def model_usage(snap: Snapshot) -> dict[str, Any]:
    cols = (
        "provider, model, status, error_class, duration_ms, first_token_ms, attempts,"
        " input_tokens, cached_input_tokens, cache_creation_tokens,"
        " output_tokens, reasoning_tokens, total_tokens"
    )
    # `http_status` is missing from trimmed test fixtures and older projections.
    has_http_status = "http_status" in snap.columns("model_attempts")
    if has_http_status:
        cols += ", http_status"
    rows = snap.select("model_attempts", cols)
    buckets: dict[tuple[str, str], dict[str, list]] = {}
    for r in rows:
        key = (r["provider"] or "-", r["model"] or "-")
        b = buckets.setdefault(
            key,
            {
                "calls": 0,
                "ok": 0,
                "error": 0,
                "running": 0,
                "errors": {},
                "error_http_status": {},
                "duration": [],
                "ttft": [],
                "multi_attempt": 0,
                "max_attempts": 0,
                "input": 0,
                "cached": 0,
                "cache_creation": 0,
                "output": 0,
                "reasoning": 0,
                "total": 0,
            },
        )
        b["calls"] += 1
        st = r["status"] or "unknown"
        if st == "ok":
            b["ok"] += 1
        elif st == "error":
            b["error"] += 1
            cls = r["error_class"] or "unclassified"
            b["errors"][cls] = b["errors"].get(cls, 0) + 1
            hs = r["http_status"] if has_http_status else None
            if hs is not None:
                hs_key = str(int(hs))
                b["error_http_status"][hs_key] = b["error_http_status"].get(hs_key, 0) + 1
        elif st == "running":
            b["running"] += 1
        if r["duration_ms"] is not None:
            b["duration"].append(float(r["duration_ms"]))
        if r["first_token_ms"] is not None:
            b["ttft"].append(float(r["first_token_ms"]))
        attempts = int(r["attempts"] or 1)
        if attempts > 1:
            b["multi_attempt"] += 1
        b["max_attempts"] = max(b["max_attempts"], attempts)
        for field_name, col in (
            ("input", "input_tokens"),
            ("cached", "cached_input_tokens"),
            ("cache_creation", "cache_creation_tokens"),
            ("output", "output_tokens"),
            ("reasoning", "reasoning_tokens"),
            ("total", "total_tokens"),
        ):
            b[field_name] += int(r[col] or 0)

    models = []
    for (provider, model), b in sorted(buckets.items(), key=lambda kv: -kv[1]["calls"]):
        finished = b["ok"] + b["error"]
        models.append(
            {
                "provider": provider,
                "model": model,
                "calls": b["calls"],
                "ok": b["ok"],
                "error": b["error"],
                "running": b["running"],
                "error_rate": rate(b["error"], finished),
                "errors": b["errors"],
                "errors_by_http_status": b["error_http_status"],
                "ttft_ms": summarize(b["ttft"]),
                "duration_ms": summarize(b["duration"]),
                "cache_hit_rate": rate(b["cached"], b["input"]),
                "calls_needing_retry": b["multi_attempt"],
                "max_attempts": b["max_attempts"],
                "tokens": {
                    "input": b["input"],
                    "cached_input": b["cached"],
                    "cache_creation": b["cache_creation"],
                    "output": b["output"],
                    "reasoning": b["reasoning"],
                    "total": b["total"],
                },
            }
        )

    totals = {
        "calls": sum(m["calls"] for m in models),
        "finished": sum(m["ok"] + m["error"] for m in models),
        "error": sum(m["error"] for m in models),
        "input": sum(m["tokens"]["input"] for m in models),
        "cached_input": sum(m["tokens"]["cached_input"] for m in models),
        "output": sum(m["tokens"]["output"] for m in models),
        "reasoning": sum(m["tokens"]["reasoning"] for m in models),
        "total": sum(m["tokens"]["total"] for m in models),
    }
    # Denominator excludes attempts still in flight, so an in-progress call
    # never dilutes the failure rate.
    totals["error_rate"] = rate(totals["error"], totals["finished"])
    totals["cache_hit_rate"] = rate(totals["cached_input"], totals["input"])
    return {"models": models, "totals": totals}


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


def tool_usage(snap: Snapshot) -> dict[str, Any]:
    rows = snap.select(
        "tool_executions",
        "tool_name, status, is_error, exit_code, duration_ms, approval_wait_ms,"
        " output_truncated",
    )
    buckets: dict[str, dict[str, Any]] = {}
    status_counts: dict[str, int] = {}
    for r in rows:
        name = r["tool_name"] or "-"
        status = r["status"] or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
        b = buckets.setdefault(
            name,
            {
                "calls": 0,
                "errors": 0,
                "duration": [],
                "pure_exec": [],
                "wait": [],
                "lifecycle": 0.0,
                "truncated": 0,
                "status": {},
                "inconsistent": 0,
                "orphan_wait": 0,
            },
        )
        b["calls"] += 1
        b["status"][status] = b["status"].get(status, 0) + 1
        if r["is_error"] == 1 or status in TOOL_ERROR_STATUSES:
            b["errors"] += 1
        if r["output_truncated"] == 1:
            b["truncated"] += 1

        dur = r["duration_ms"]
        wait = r["approval_wait_ms"]
        if dur is not None:
            # duration_ms spans prepared -> finished, so it ALREADY contains
            # the approval wait. Subtracting it recovers time actually executing;
            # adding the two would double-count the wait.
            dur_f = float(dur)
            wait_f = float(wait) if wait is not None else 0.0
            b["lifecycle"] += dur_f
            pure = dur_f - wait_f
            if pure < 0:
                # approved after the call already finished: a projection
                # inconsistency, not negative work.
                b["inconsistent"] += 1
                pure = 0.0
            b["pure_exec"].append(pure)
        elif wait is not None:
            # Waited for a decision but never executed (expired/stuck ticket).
            # The wait is real elapsed time, so it counts toward the lifecycle.
            b["orphan_wait"] += 1
            b["lifecycle"] += float(wait)
        if wait is not None:
            b["wait"].append(float(wait))

    tools = []
    for name, b in sorted(buckets.items(), key=lambda kv: -kv[1]["calls"]):
        tools.append(
            {
                "tool": name,
                "calls": b["calls"],
                "errors": b["errors"],
                "error_rate": rate(b["errors"], b["calls"]),
                "duration_ms": summarize(b["duration"]),
                "pure_exec_ms": summarize(b["pure_exec"]),
                "lifecycle_ms": b["lifecycle"],
                "approval_wait_ms": summarize(b["wait"]),
                "approvals_required": len(b["wait"]),
                "orphan_waits": b["orphan_wait"],
                "inconsistent_timing": b["inconsistent"],
                "truncated": b["truncated"],
                "status": dict(sorted(b["status"].items(), key=lambda kv: -kv[1])),
            }
        )

    total_calls = sum(t["calls"] for t in tools)
    total_errors = sum(t["errors"] for t in tools)
    lifecycle_ms = sum(t["lifecycle_ms"] for t in tools)
    wait_ms = sum(t["approval_wait_ms"]["sum"] or 0 for t in tools)
    pure_exec_ms = sum(t["pure_exec_ms"]["sum"] or 0 for t in tools)
    return {
        "calls": total_calls,
        "errors": total_errors,
        "error_rate": rate(total_errors, total_calls),
        "status": dict(sorted(status_counts.items(), key=lambda kv: -kv[1])),
        "tools": tools,
        # Total wall time tool calls occupied the agent, including approval
        # waits and calls that waited but never ran.
        "total_lifecycle_ms": lifecycle_ms,
        "total_pure_exec_ms": pure_exec_ms,
        "total_approval_wait_ms": wait_ms,
        # "Approval tax": share of the tool lifecycle spent waiting for a human.
        # The denominator is the lifecycle (which already contains the wait),
        # never lifecycle + wait.
        "approval_tax": rate(wait_ms, lifecycle_ms),
        "truncated_results": sum(t["truncated"] for t in tools),
        "orphan_waits": sum(t["orphan_waits"] for t in tools),
        "inconsistent_timing": sum(t["inconsistent_timing"] for t in tools),
    }


# ---------------------------------------------------------------------------
# approvals / security decisions
# ---------------------------------------------------------------------------


def approvals(snap: Snapshot) -> dict[str, Any]:
    decision_types = {
        k[0]: v for k, v in snap.group_counts("security_decisions", "decision_type").items()
    }
    resolutions = {
        (k[0] or "-"): v
        for k, v in snap.group_counts(
            "security_decisions",
            "decision",
            extra=["decision_type = 'approval_resolved'"],
        ).items()
    }
    approved = resolutions.get("approved", 0) + resolutions.get("approved_session", 0)
    rejected = resolutions.get("rejected", 0)
    expired = resolutions.get("expired", 0)
    narrowed = resolutions.get("narrowed", 0)
    resolved_total = sum(resolutions.values())

    per_tool = {
        k[0] or "-": v
        for k, v in snap.group_counts(
            "security_decisions",
            "tool_name",
            extra=["decision_type = 'approval_requested'"],
        ).items()
    }

    waits = snap.series("tool_executions", "approval_wait_ms")
    grants = snap.count("telemetry_events", extra=["kind = 'session_grant_created'"])
    requests = decision_types.get("approval_requested", 0)
    return {
        "decision_types": decision_types,
        "resolutions": resolutions,
        "approval_requests": requests,
        "resolved": resolved_total,
        "approved": approved,
        "rejected": rejected,
        "expired": expired,
        "narrowed": narrowed,
        "rejection_rate": rate(rejected, resolved_total),
        "unresolved_requests": max(requests - resolved_total, 0),
        "requests_per_turn": None,  # filled by the report when turns are known
        "session_grants": grants,
        "tools_most_approved": dict(sorted(per_tool.items(), key=lambda kv: -kv[1])[:10]),
        "approval_wait_ms": summarize(waits),
    }


# ---------------------------------------------------------------------------
# security / authorization surface
# ---------------------------------------------------------------------------


def security(snap: Snapshot) -> dict[str, Any]:
    """Authorization volume, and how far one human decision propagates.

    The question that matters about an agent's gate is not "did it deny
    anything" — a gate that denies nothing may be well-tuned, or may simply
    never have been asked. It is "how much does one approval buy": a gate that
    prompts per call carries a different risk from one that prompts once and
    then auto-allows everything of that kind afterwards, even at identical
    denial counts.
    """
    decision_types = {
        k[0]: v for k, v in snap.group_counts("security_decisions", "decision_type").items()
    }
    resolutions = {
        (k[0] or "-"): v
        for k, v in snap.group_counts(
            "security_decisions", "decision", extra=["decision_type = 'approval_resolved'"]
        ).items()
    }
    approved_single = resolutions.get("approved", 0)
    approved_session = resolutions.get("approved_session", 0)
    rejected = resolutions.get("rejected", 0)
    expired = resolutions.get("expired", 0)
    resolved_total = sum(resolutions.values())

    prompts = decision_types.get("approval_requested", 0)
    # Executions the gate let through without raising a fresh prompt.
    auto_allowed = snap.count("telemetry_events", extra=["kind = 'tool_approved'"])

    # How many sessions hold at least one session-wide grant. Guarded on the
    # column so a trimmed fixture degrades instead of raising.
    sessions_with_grants = None
    if "session_id" in snap.columns("security_decisions"):
        sessions_with_grants = int(
            snap.scalar(
                "SELECT COUNT(DISTINCT session_id) FROM security_decisions "
                "WHERE decision_type = 'approval_resolved' "
                "AND decision = 'approved_session' AND session_id IS NOT NULL"
            )
            or 0
        )

    # Executions carrying no approval stamp: the gate recorded no decision.
    ungated: dict[str, int] = {}
    if "approved_at" in snap.columns("tool_executions"):
        for r in snap.rows(
            "SELECT tool_name, COUNT(*) AS n FROM tool_executions "
            "WHERE approved_at IS NULL GROUP BY tool_name"
        ):
            ungated[str(r["tool_name"] or "-")] = int(r["n"])

    return {
        "decision_types": decision_types,
        "resolutions": resolutions,
        "approval_prompts": prompts,
        "resolved": resolved_total,
        "approved": approved_single + approved_session,
        "approved_single": approved_single,
        "approved_session": approved_session,
        "rejected": rejected,
        "expired": expired,
        "rejection_rate": rate(rejected, resolved_total),
        # Share of resolutions that granted the whole session rather than the
        # single call under review.
        "session_grant_share": rate(approved_session, resolved_total),
        "sessions_with_session_grants": sessions_with_grants,
        # One human decision -> this many executions that saw no human.
        "auto_allowed_executions": auto_allowed,
        "approval_amplification": rate(auto_allowed, prompts),
        "leases_issued": decision_types.get("lease_issued", 0),
        "leases_consumed": decision_types.get("lease_consumed", 0),
        "capability_stale": decision_types.get("capability_stale", 0),
        "stale_capability_rejections": snap.count(
            "telemetry_events", extra=["kind = 'capability_rejected_stale'"]
        ),
        "ungated_executions": sum(ungated.values()),
        "guarded_ungated_executions": sum(
            n for tool, n in ungated.items() if tool in GUARDED_TOOLS
        ),
        "ungated_by_tool": dict(sorted(ungated.items(), key=lambda kv: -kv[1])[:10]),
    }


# ---------------------------------------------------------------------------
# context: prompt size + compaction
# ---------------------------------------------------------------------------


def context(snap: Snapshot) -> dict[str, Any]:
    prompt_rows = snap.select(
        "prompt_builds", "session_id, turn_id, context_item_count, estimated_input_tokens"
    )
    input_tokens = [float(r["estimated_input_tokens"]) for r in prompt_rows if r["estimated_input_tokens"] is not None]
    item_counts = [float(r["context_item_count"]) for r in prompt_rows if r["context_item_count"] is not None]

    comp_rows = snap.select(
        "compactions", "session_id, trigger, status, pre_item_count, candidate_item_count"
    )
    triggers: dict[str, int] = {}
    statuses: dict[str, int] = {}
    pre_counts: list[float] = []
    for r in comp_rows:
        triggers[r["trigger"] or "-"] = triggers.get(r["trigger"] or "-", 0) + 1
        statuses[r["status"] or "-"] = statuses.get(r["status"] or "-", 0) + 1
        if r["pre_item_count"] is not None:
            pre_counts.append(float(r["pre_item_count"]))

    turns = snap.count("turns")
    sessions = snap.count("sessions")
    return {
        "prompt_builds": len(prompt_rows),
        "prompt_input_tokens": summarize(input_tokens),
        "prompt_context_items": summarize(item_counts),
        "compactions": len(comp_rows),
        "compaction_triggers": triggers,
        "compaction_status": statuses,
        "compaction_pre_item_count": summarize(pre_counts),
        # How often the context window overflows far enough to force a rewrite.
        "compactions_per_100_turns": (len(comp_rows) / turns * 100) if turns else None,
        "sessions_with_compaction": len({r["session_id"] for r in comp_rows}),
        "sessions": sessions,
    }


# ---------------------------------------------------------------------------
# memory retrieval
# ---------------------------------------------------------------------------


def memory(snap: Snapshot) -> dict[str, Any]:
    rows = snap.select("memory_retrievals", "router_kind, selected_count, duration_ms, query_chars")
    by_router: dict[str, dict[str, Any]] = {}
    selected: list[float] = []
    durations: list[float] = []
    empty = 0
    for r in rows:
        kind = r["router_kind"] or "-"
        b = by_router.setdefault(kind, {"count": 0, "empty": 0, "selected": [], "duration": []})
        b["count"] += 1
        sel = int(r["selected_count"] or 0)
        b["selected"].append(float(sel))
        selected.append(float(sel))
        if sel == 0:
            b["empty"] += 1
            empty += 1
        if r["duration_ms"] is not None:
            b["duration"].append(float(r["duration_ms"]))
            durations.append(float(r["duration_ms"]))

    routers = [
        {
            "router_kind": kind,
            "count": b["count"],
            "empty_results": b["empty"],
            "empty_rate": rate(b["empty"], b["count"]),
            "selected_count": summarize(b["selected"]),
            "duration_ms": summarize(b["duration"]),
        }
        for kind, b in sorted(by_router.items(), key=lambda kv: -kv[1]["count"])
    ]
    return {
        "retrievals": len(rows),
        "routers": routers,
        "empty_results": empty,
        "empty_rate": rate(empty, len(rows)),
        "selected_count": summarize(selected),
        "duration_ms": summarize(durations),
        "retrievals_per_turn": None,  # filled by the report when turns are known
    }


# ---------------------------------------------------------------------------
# reliability / recovery
# ---------------------------------------------------------------------------


def reliability(snap: Snapshot) -> dict[str, Any]:
    error_events = {
        k[0]: v
        for k, v in snap.group_counts("telemetry_events", "kind", extra=["severity = 'error'"]).items()
    }
    # Same definitions as Grodex's v_recovery_anomalies view, but windowed.
    anomalies = {
        "open_turns": snap.count("turns", extra=["(finished_at IS NULL OR status = 'running')"]),
        "stuck_tools": snap.count(
            "tool_executions", extra=["started_at IS NOT NULL AND finished_at IS NULL"]
        ),
        "uncommitted_results": snap.count(
            "tool_executions",
            extra=["finished_at IS NOT NULL AND committed_at IS NULL AND status != 'indeterminate'"],
        ),
        "indeterminate_tools": snap.count("tool_executions", extra=["status = 'indeterminate'"]),
    }
    # A 4xx is the provider refusing the request we built: a client-side defect,
    # not an outage, so retrying cannot help. Track how often one also ends the
    # turn it happened in. Guarded on column presence for trimmed fixtures.
    ma_cols = snap.columns("model_attempts")
    http_4xx: dict[str, int] = {}
    turns_with_4xx = 0
    turns_with_4xx_killed = 0
    if "http_status" in ma_cols:
        pred, winp = snap.window.predicate("model_attempts")
        win_clause = f"AND {pred}" if pred else ""
        http_4xx = {
            str(int(r["http_status"])): int(r["n"])
            for r in snap.select(
                "model_attempts",
                "http_status, COUNT(*) AS n",
                extra=["http_status BETWEEN 400 AND 499"],
                tail="GROUP BY http_status",
            )
            if r["http_status"] is not None
        }
        if "turn_id" in ma_cols:
            turns_with_4xx = int(
                snap.scalar(
                    "SELECT COUNT(DISTINCT turn_id) FROM model_attempts "
                    f"WHERE http_status BETWEEN 400 AND 499 AND turn_id IS NOT NULL {win_clause}",
                    winp,
                )
                or 0
            )
            turns_with_4xx_killed = int(
                snap.scalar(
                    "SELECT COUNT(*) FROM ("
                    "  SELECT DISTINCT turn_id FROM model_attempts "
                    f"  WHERE http_status BETWEEN 400 AND 499 AND turn_id IS NOT NULL {win_clause}"
                    ") b JOIN turns t ON t.turn_id = b.turn_id "
                    "WHERE t.termination_reason = 'sampling_error'",
                    winp,
                )
                or 0
            )
    return {
        "error_severity_events": sum(error_events.values()),
        "error_events_by_kind": error_events,
        "anomalies": anomalies,
        # `tool_indeterminate` is the journal event behind a lost tool result.
        "tool_indeterminate_events": snap.count("telemetry_events", extra=["kind = 'tool_indeterminate'"]),
        "capability_stale_decisions": snap.count(
            "security_decisions", extra=["decision_type = 'capability_stale'"]
        ),
        "http_4xx_by_status": http_4xx,
        "turns_with_http_4xx": turns_with_4xx,
        "turns_with_http_4xx_killed": turns_with_4xx_killed,
    }


# ---------------------------------------------------------------------------
# capability surfaces (subagents / skills / mcp)
# ---------------------------------------------------------------------------


def capabilities(snap: Snapshot) -> dict[str, Any]:
    subagent_rows = snap.select("subagent_runs", "status")
    subagent_status = count_by(subagent_rows, "status")
    delegate_calls = snap.count("tool_executions", extra=["tool_name = 'delegate_task'"])
    skill_rows = snap.select("skill_activations", "skill_name")
    skills = count_by(skill_rows, "skill_name")
    mcp_rows = snap.select("mcp_lifecycle", "server_name, phase, status")
    mcp_servers = count_by(mcp_rows, "server_name")
    return {
        "subagent_runs": len(subagent_rows),
        "subagent_status": subagent_status,
        "delegate_task_calls": delegate_calls,
        # If these disagree, the projection is missing rows for delegated work.
        "subagent_projection_gap": delegate_calls - len(subagent_rows),
        "skill_activations": len(skill_rows),
        "skills": dict(list(skills.items())[:15]),
        "mcp_events": len(mcp_rows),
        "mcp_servers": mcp_servers,
    }


# ---------------------------------------------------------------------------
# projection integrity — "will a reader choke on this row"
# ---------------------------------------------------------------------------

# Columns the projection's own readers declare non-nullable. A NULL here does
# not skew a metric, it raises `Invalid column type Null` and takes down the
# surface that reads it — observed in the wild as the desktop per-turn
# drill-down dying on `model_attempts.provider`.
#
# Checked deliberately UNWINDOWED: a malformed row is a property of the
# database, not of the reporting window. Scoping this to a window would hide
# exactly the rows written before it, which are the ones already breaking the
# reader.
REQUIRED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("model_attempts", "provider"),
    ("model_attempts", "model"),
    ("turns", "turn_id"),
    ("turns", "session_id"),
)


def integrity(snap: Snapshot) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    violations = 0
    for table, column in REQUIRED_COLUMNS:
        if not snap.has_table(table) or column not in snap.columns(table):
            checks.append(
                {
                    "table": table,
                    "column": column,
                    "nulls": None,
                    "note": "column absent from projection",
                }
            )
            continue
        nulls = int(snap.scalar(f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL") or 0)
        violations += nulls
        checks.append({"table": table, "column": column, "nulls": nulls})

    # A model attempt whose turn is gone is unreadable in the other direction:
    # the drill-down joins on turn_id and would silently drop it.
    orphans = None
    if snap.has_table("model_attempts") and "turn_id" in snap.columns("model_attempts"):
        orphans = int(
            snap.scalar(
                "SELECT COUNT(*) FROM model_attempts a WHERE a.turn_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.turn_id = a.turn_id)"
            )
            or 0
        )

    return {
        "null_violations": violations,
        "null_checks": checks,
        "violating_columns": [f"{c['table']}.{c['column']}" for c in checks if c["nulls"]],
        "orphan_model_attempts": orphans,
    }


# ---------------------------------------------------------------------------
# cost (optional: only when a price table is supplied)
# ---------------------------------------------------------------------------


def cost(report: dict[str, Any], prices: dict[str, Any]) -> dict[str, Any]:
    """Estimate spend from token counts and a user-supplied price table.

    Prices are intentionally NOT hardcoded: inventing a rate for a model we
    cannot verify would make the number look authoritative while being wrong.
    """
    models = report.get("model_usage", {}).get("models") or []
    if not prices:
        return {
            "priced": False,
            "note": "no price table supplied (--prices); token counts are reported without cost",
        }
    per_model = []
    total = 0.0
    currency = "USD"
    for m in models:
        rate_card = (prices.get(m["provider"]) or {}).get(m["model"])
        if not rate_card:
            per_model.append({"provider": m["provider"], "model": m["model"], "cost": None,
                              "note": "no price entry"})
            continue
        currency = rate_card.get("currency", currency)
        tok = m["tokens"]
        billable_input = max(tok["input"] - tok["cached_input"], 0)
        value = (
            billable_input / 1e6 * float(rate_card.get("input_per_mtok", 0.0))
            + tok["cached_input"] / 1e6 * float(rate_card.get("cached_input_per_mtok", 0.0))
            + tok["output"] / 1e6 * float(rate_card.get("output_per_mtok", 0.0))
        )
        total += value
        per_model.append({"provider": m["provider"], "model": m["model"], "cost": value})
    return {"priced": True, "currency": currency, "total": total, "per_model": per_model}
