"""Render a metrics report as Markdown (and a compact console summary)."""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .stats import human_float, human_int, human_ms


def _cells(values: Iterable[Any]) -> str:
    return " | ".join("-" if v is None else str(v) for v in values)


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for row in rows:
        lines.append("| " + _cells(row) + " |")
    return "\n".join(lines)


def _pct(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value * 100:.{digits}f}%"


def _stat(summary: dict[str, Any] | None, key: str = "p50") -> str:
    if not summary or summary.get(key) is None:
        return "-"
    # A percentile over 1-2 samples is not a median; say so instead of
    # printing a number that invites a wrong conclusion.
    if not summary.get("enough", True):
        return f"n/a (n={summary.get('count', 0)})"
    return human_ms(summary[key])


def render_markdown(report: dict[str, Any], findings: list[dict[str, Any]], top_n: int = 15) -> str:
    meta = report.get("meta", {})
    overview = report.get("overview", {})
    turns = report.get("turn_health", {})
    models = report.get("model_usage", {})
    tools = report.get("tool_usage", {})
    approvals = report.get("approvals", {})
    context = report.get("context", {})
    memory = report.get("memory", {})
    reliability = report.get("reliability", {})
    capabilities = report.get("capabilities", {})
    security = report.get("security", {})
    integrity = report.get("integrity", {})
    cost = report.get("cost", {})

    out: list[str] = []
    out.append("# Grodex telemetry baseline")
    out.append("")
    out.append(f"- generated: `{meta.get('generated_at')}`")
    out.append(f"- database: `{meta.get('db_path')}` ({human_int((meta.get('db_bytes') or 0) / 1024)} KiB)")
    out.append(f"- window: **{meta.get('window')}**")
    out.append(
        f"- sessions: **{overview.get('sessions')}**, turns: **{overview.get('turns')}**, "
        f"events: **{human_int(overview.get('events'))}**"
    )
    if overview.get("last_activity_at"):
        out.append(f"- last activity: `{overview['last_activity_at']}`")
    out.append("")

    # -- findings ----------------------------------------------------------
    out.append("## Findings")
    out.append("")
    if not findings:
        out.append("_No rule fired. Either the run is healthy or there is not enough data._")
    else:
        icons = {"critical": "CRITICAL", "warn": "WARN", "info": "INFO"}
        out.append(_table(
            ["severity", "finding", "evidence"],
            [
                (
                    icons.get(f["severity"], f["severity"]),
                    f["title"],
                    ", ".join(f"`{k}`={_fmt_evidence(v)}" for k, v in f["evidence"].items()) or "-",
                )
                for f in findings
            ],
        ))
        out.append("")
        for f in findings:
            out.append(f"### [{icons.get(f['severity'], f['severity'])}] {f['title']}")
            out.append("")
            out.append(f["detail"])
            out.append("")
    out.append("")

    # -- turn health -------------------------------------------------------
    out.append("## Turn health")
    out.append("")
    out.append(_table(
        ["metric", "value"],
        [
            ("turns", human_int(turns.get("turns"))),
            ("final_answer rate", _pct(turns.get("final_answer_rate"))),
            ("repair_exhausted rate", _pct(turns.get("repair_exhausted_rate"))),
            ("sampling_error rate", _pct(turns.get("sampling_error_rate"))),
            ("cancelled rate", _pct(turns.get("cancel_rate"))),
            ("still running", human_int(turns.get("still_running"))),
            ("turn duration p50 / p90", f"{_stat(turns.get('duration_ms'), 'p50')} / {_stat(turns.get('duration_ms'), 'p90')}"),
            ("steps per turn (mean / p90)", f"{human_float((turns.get('steps_per_turn') or {}).get('mean'))} / {human_float((turns.get('steps_per_turn') or {}).get('p90'))}"),
            ("tool calls per turn (mean / p90)", f"{human_float((turns.get('tool_calls_per_turn') or {}).get('mean'))} / {human_float((turns.get('tool_calls_per_turn') or {}).get('p90'))}"),
            ("turns with compaction", f"{human_int(turns.get('turns_with_compaction'))} ({_pct(turns.get('compaction_turn_rate'))})"),
        ],
    ))
    out.append("")
    if turns.get("termination_reasons"):
        out.append("**Termination reasons**")
        out.append("")
        out.append(_table(
            ["termination_reason", "count", "share"],
            [
                (reason, human_int(count), _pct((count / turns["turns"]) if turns.get("turns") else None))
                for reason, count in turns["termination_reasons"].items()
            ],
        ))
        out.append("")

    # -- model -------------------------------------------------------------
    out.append("## Model usage")
    out.append("")
    model_rows = []
    for m in models.get("models", []):
        model_rows.append((
            f"{m['provider']}/{m['model']}",
            human_int(m["calls"]),
            _pct(m["error_rate"]),
            _stat(m.get("ttft_ms")),
            _stat(m.get("duration_ms")),
            _pct(m.get("cache_hit_rate")),
            human_int(m["tokens"]["input"]),
            human_int(m["tokens"]["output"]),
            human_int(m["tokens"]["reasoning"]),
            human_int(m.get("calls_needing_retry")),
        ))
    out.append(_table(
        ["model", "calls", "error rate", "TTFT p50", "latency p50", "cache hit", "input tok", "output tok", "reasoning tok", "retried calls"],
        model_rows,
    ))
    out.append("")
    totals = models.get("totals", {})
    out.append(
        f"Totals: {human_int(totals.get('calls'))} calls, {human_int(totals.get('total'))} tokens "
        f"(input {human_int(totals.get('input'))}, cached {human_int(totals.get('cached_input'))}, "
        f"output {human_int(totals.get('output'))}, reasoning {human_int(totals.get('reasoning'))}), "
        f"cache hit {_pct(totals.get('cache_hit_rate'))}."
    )
    out.append("")

    # -- tools -------------------------------------------------------------
    out.append("## Tools")
    out.append("")
    out.append(
        f"{human_int(tools.get('calls'))} calls, {human_int(tools.get('errors'))} failures "
        f"({_pct(tools.get('error_rate'))}). Tool lifecycle {human_ms(tools.get('total_lifecycle_ms'))} "
        f"(pure exec {human_ms(tools.get('total_pure_exec_ms'))}), approval wait "
        f"{human_ms(tools.get('total_approval_wait_ms'))} "
        f"(**approval tax {_pct(tools.get('approval_tax'))}**). "
        f"Truncated results: {human_int(tools.get('truncated_results'))}."
    )
    out.append("")
    rows = []
    for t in tools.get("tools", [])[:top_n]:
        rows.append((
            t["tool"],
            human_int(t["calls"]),
            _pct(t["error_rate"]),
            _stat(t.get("pure_exec_ms"), "p50"),
            _stat(t.get("pure_exec_ms"), "p90"),
            _stat(t.get("approval_wait_ms"), "p50"),
            human_int(t["approvals_required"]),
            human_int(t["truncated"]),
        ))
    out.append(_table(
        ["tool", "calls", "error rate", "exec p50", "exec p90", "appr wait p50", "approvals", "truncated"],
        rows,
    ))
    out.append("")
    if tools.get("status"):
        out.append("Status distribution: " + ", ".join(f"`{k}`={v}" for k, v in tools["status"].items()))
        out.append("")

    # -- approvals ---------------------------------------------------------
    out.append("## Approvals")
    out.append("")
    out.append(_table(
        ["metric", "value"],
        [
            ("approval requests", human_int(approvals.get("approval_requests"))),
            ("resolved", human_int(approvals.get("resolved"))),
            ("approved (incl. session grant)", human_int(approvals.get("approved"))),
            ("rejected", human_int(approvals.get("rejected"))),
            ("expired", human_int(approvals.get("expired"))),
            ("narrowed", human_int(approvals.get("narrowed"))),
            ("rejection rate", _pct(approvals.get("rejection_rate"))),
            ("session grants created", human_int(approvals.get("session_grants"))),
            ("unresolved requests", human_int(approvals.get("unresolved_requests"))),
            ("approval wait p50 / p90", f"{_stat(approvals.get('approval_wait_ms'), 'p50')} / {_stat(approvals.get('approval_wait_ms'), 'p90')}"),
        ],
    ))
    out.append("")
    if approvals.get("tools_most_approved"):
        out.append("Most-approved tools: " + ", ".join(
            f"`{k}`={v}" for k, v in approvals["tools_most_approved"].items()
        ))
        out.append("")

    # -- security ----------------------------------------------------------
    out.append("## Security / authorization")
    out.append("")
    out.append(_table(
        ["metric", "value"],
        [
            ("approval prompts", human_int(security.get("approval_prompts"))),
            ("resolved decisions", human_int(security.get("resolved"))),
            ("approved (single call)", human_int(security.get("approved_single"))),
            ("approved (whole session)", human_int(security.get("approved_session"))),
            ("session-wide share of approvals", _pct(security.get("session_grant_share"))),
            ("sessions holding a session grant", human_int(security.get("sessions_with_session_grants"))),
            ("rejected / expired", f"{human_int(security.get('rejected'))} / {human_int(security.get('expired'))}"),
            ("rejection rate", _pct(security.get("rejection_rate"))),
            ("auto-allowed executions", human_int(security.get("auto_allowed_executions"))),
            ("executions per human decision", human_float(security.get("approval_amplification"))),
            ("leases issued / consumed", f"{human_int(security.get('leases_issued'))} / {human_int(security.get('leases_consumed'))}"),
            ("stale capability rejections", human_int(security.get("stale_capability_rejections"))),
            ("executions with no approval stamp", human_int(security.get("ungated_executions"))),
            ("...of which guarded tools", human_int(security.get("guarded_ungated_executions"))),
        ],
    ))
    out.append("")
    if security.get("decision_types"):
        out.append("Decision types: " + ", ".join(
            f"`{k}`={v}" for k, v in security["decision_types"].items()
        ))
        out.append("")

    # -- context -----------------------------------------------------------
    out.append("## Context & compaction")
    out.append("")
    pt = context.get("prompt_input_tokens") or {}
    ci = context.get("prompt_context_items") or {}
    out.append(_table(
        ["metric", "value"],
        [
            ("prompt builds", human_int(context.get("prompt_builds"))),
            ("estimated input tokens (mean / p50)", f"{human_int(pt.get('mean'))} / {human_int(pt.get('p50'))}"),
            ("estimated input tokens (p90 / max)", f"{human_int(pt.get('p90'))} / {human_int(pt.get('max'))}"),
            ("context items (mean / p90)", f"{human_float(ci.get('mean'))} / {human_float(ci.get('p90'))}"),
            ("compactions", human_int(context.get("compactions"))),
            ("compactions per 100 turns", human_float(context.get("compactions_per_100_turns"))),
            ("sessions with compaction", f"{human_int(context.get('sessions_with_compaction'))} / {human_int(context.get('sessions'))}"),
            ("triggers", str(context.get("compaction_triggers") or {})),
        ],
    ))
    out.append("")

    # -- memory ------------------------------------------------------------
    out.append("## Memory retrieval")
    out.append("")
    out.append(_table(
        ["metric", "value"],
        [
            ("retrievals", human_int(memory.get("retrievals"))),
            ("empty results", f"{human_int(memory.get('empty_results'))} ({_pct(memory.get('empty_rate'))})"),
            ("selected items (mean / p50)", f"{human_float((memory.get('selected_count') or {}).get('mean'))} / {human_float((memory.get('selected_count') or {}).get('p50'))}"),
            ("retrieval latency p50 / p90", f"{_stat(memory.get('duration_ms'), 'p50')} / {_stat(memory.get('duration_ms'), 'p90')}"),
        ],
    ))
    out.append("")
    if memory.get("routers"):
        out.append(_table(
            ["router", "count", "empty rate", "mean selected", "latency p50"],
            [
                (r["router_kind"], human_int(r["count"]), _pct(r["empty_rate"]),
                 human_float((r.get("selected_count") or {}).get("mean")),
                 _stat(r.get("duration_ms"), "p50"))
                for r in memory["routers"]
            ],
        ))
        out.append("")

    # -- reliability -------------------------------------------------------
    out.append("## Reliability")
    out.append("")
    anomalies = reliability.get("anomalies", {})
    out.append(_table(
        ["metric", "value"],
        [
            ("error-severity events", human_int(reliability.get("error_severity_events"))),
            ("open turns", human_int(anomalies.get("open_turns"))),
            ("stuck tools", human_int(anomalies.get("stuck_tools"))),
            ("uncommitted results", human_int(anomalies.get("uncommitted_results"))),
            ("indeterminate tools", human_int(anomalies.get("indeterminate_tools"))),
            ("tool_indeterminate journal events", human_int(reliability.get("tool_indeterminate_events"))),
            ("capability_stale decisions", human_int(reliability.get("capability_stale_decisions"))),
        ],
    ))
    out.append("")
    if reliability.get("error_events_by_kind"):
        out.append("Errors by event kind: " + ", ".join(
            f"`{k}`={v}" for k, v in reliability["error_events_by_kind"].items()
        ))
        out.append("")

    # -- capability surfaces ----------------------------------------------
    out.append("## Subagents, skills, MCP")
    out.append("")
    out.append(_table(
        ["metric", "value"],
        [
            ("subagent runs", human_int(capabilities.get("subagent_runs"))),
            ("delegate_task calls", human_int(capabilities.get("delegate_task_calls"))),
            ("subagent projection gap", human_int(capabilities.get("subagent_projection_gap"))),
            ("subagent status", str(capabilities.get("subagent_status") or {})),
            ("skill activations", human_int(capabilities.get("skill_activations"))),
            ("mcp events", human_int(capabilities.get("mcp_events"))),
        ],
    ))
    out.append("")
    if capabilities.get("skills"):
        out.append("Top skills: " + ", ".join(f"`{k}`={v}" for k, v in capabilities["skills"].items()))
        out.append("")

    # -- cost --------------------------------------------------------------
    out.append("## Cost")
    out.append("")
    if cost.get("priced"):
        out.append(_table(
            ["model", "estimated cost"],
            [
                (f"{m['provider']}/{m['model']}", human_float(m.get("cost"), 4))
                for m in cost.get("per_model", [])
            ],
        ))
        out.append("")
        out.append(f"**Total: {human_float(cost.get('total'), 4)} {cost.get('currency', 'USD')}** (from a user-supplied price table).")
    else:
        out.append(cost.get("note", "no price table supplied"))
    out.append("")

    # -- integrity ---------------------------------------------------------
    # Unwindowed by design: see metrics.integrity. A malformed row is a
    # property of the database, not of the reporting window.
    out.append("## Projection integrity")
    out.append("")
    integrity_rows = [
        (
            f"{c['table']}.{c['column']} IS NULL",
            "column absent" if c["nulls"] is None else human_int(c["nulls"]),
        )
        for c in integrity.get("null_checks", [])
    ]
    integrity_rows.append(
        ("model_attempts with no matching turn", human_int(integrity.get("orphan_model_attempts")))
    )
    out.append(_table(["check", "rows"], integrity_rows))
    out.append("")

    # -- appendix ----------------------------------------------------------
    out.append("## Appendix: projection coverage")
    out.append("")
    counts = meta.get("row_counts", {})
    out.append(_table(
        ["table", "rows"],
        [(name, human_int(n)) for name, n in counts.items()],
    ))
    out.append("")
    return "\n".join(out)


def _fmt_evidence(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}:{_fmt_evidence(v)}" for k, v in list(value.items())[:5]) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_fmt_evidence(v) for v in value[:5]) + "]"
    return str(value)


def render_console(report: dict[str, Any], findings: list[dict[str, Any]]) -> str:
    """Short terminal summary — the Markdown report is the artifact."""
    overview = report.get("overview", {})
    turns = report.get("turn_health", {})
    tools = report.get("tool_usage", {})
    models = report.get("model_usage", {}).get("totals", {})
    security = report.get("security", {})
    lines = [
        f"window: {report.get('meta', {}).get('window')}",
        f"sessions={overview.get('sessions')} turns={overview.get('turns')} events={overview.get('events')}",
        f"final_answer={_pct(turns.get('final_answer_rate'))} "
        f"repair_exhausted={_pct(turns.get('repair_exhausted_rate'))} "
        f"sampling_error={_pct(turns.get('sampling_error_rate'))}",
        f"model calls={models.get('calls')} error_rate={_pct(models.get('error_rate'))} "
        f"cache_hit={_pct(models.get('cache_hit_rate'))}",
        f"tool calls={tools.get('calls')} error_rate={_pct(tools.get('error_rate'))} "
        f"approval_tax={_pct(tools.get('approval_tax'))}",
        f"security: session_wide_approvals={_pct(security.get('session_grant_share'))} "
        f"executions_per_decision={human_float(security.get('approval_amplification'))}",
        "",
        f"findings: {len(findings)}",
    ]
    for f in findings:
        lines.append(f"  [{f['severity'].upper():8}] {f['title']}")
    return "\n".join(lines)
