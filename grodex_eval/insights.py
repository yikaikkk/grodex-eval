"""Rule-based findings derived from an assembled metrics report.

The point of Phase 0 is not to print numbers — it is to answer "what should
we fix first?". These rules are deliberately few, explicit, and each one
carries the metric and threshold that fired, so a finding can always be
traced back to evidence and argued with.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SEVERITY_ORDER = {"critical": 0, "warn": 1, "info": 2}


@dataclass(frozen=True)
class Thresholds:
    """Tunable cut-offs for the baseline rules."""

    repair_exhausted_rate: float = 0.10
    sampling_error_rate: float = 0.02
    cancel_rate: float = 0.10
    model_error_rate: float = 0.02
    tool_error_rate: float = 0.02
    approval_tax: float = 0.25
    memory_empty_rate: float = 0.50
    router_always_empty_min_samples: int = 20
    prompt_mean_tokens: float = 50_000.0
    compactions_per_100_turns: float = 20.0
    cache_hit_rate_floor: float = 0.20
    http_4xx_share: float = 0.005


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def derive_insights(report: dict[str, Any], thresholds: Thresholds | None = None) -> list[dict[str, Any]]:
    """Return a severity-ordered list of findings."""
    t = thresholds or Thresholds()
    findings: list[dict[str, Any]] = []

    def add(severity: str, title: str, detail: str, **evidence: Any) -> None:
        findings.append(
            {"severity": severity, "title": title, "detail": detail, "evidence": evidence}
        )

    meta = report.get("meta", {})
    turns = report.get("turn_health", {})
    model = report.get("model_usage", {}).get("totals", {})
    tools = report.get("tool_usage", {})
    approvals = report.get("approvals", {})
    context = report.get("context", {})
    memory = report.get("memory", {})
    reliability = report.get("reliability", {})
    capabilities = report.get("capabilities", {})

    # -- coverage -----------------------------------------------------------
    missing = meta.get("missing_tables") or []
    if missing:
        add("warn", "telemetry tables missing", "Sections depending on them are empty.", tables=missing)

    # -- turn outcomes ------------------------------------------------------
    turn_total = turns.get("turns") or 0
    repair = turns.get("repair_exhausted_rate")
    if repair is not None and repair > t.repair_exhausted_rate:
        count = turns.get("termination_reasons", {}).get("repair_exhausted", 0)
        add(
            "critical",
            "repair loop exhausts budget on many turns",
            f"{count}/{turn_total} turns ended with repair_exhausted instead of a final answer. "
            "The repair sampling path is a fallback for malformed model output; a rate this high "
            "means either the model keeps emitting invalid tool calls, or the repair trigger fires "
            "on ordinary question-answering turns.",
            rate=repair,
            threshold=t.repair_exhausted_rate,
            turns=count,
        )
    sampling_err = turns.get("sampling_error_rate")
    if sampling_err is not None and sampling_err > t.sampling_error_rate:
        count = turns.get("termination_reasons", {}).get("sampling_error", 0)
        add(
            "warn",
            "turn-level sampling errors",
            f"{count}/{turn_total} turns terminated on sampling_error.",
            rate=sampling_err,
            threshold=t.sampling_error_rate,
        )
    cancel = turns.get("cancel_rate")
    if cancel is not None and cancel > t.cancel_rate:
        add(
            "info",
            "high cancellation rate",
            f"{_pct(cancel)} of turns were cancelled (human-initiated or superseded).",
            rate=cancel,
            threshold=t.cancel_rate,
        )
    if (turns.get("still_running") or 0) > 0:
        add(
            "info",
            "turns unfinished at snapshot time",
            f"{turns['still_running']} turns are still marked running; if the agent is live this is expected.",
            count=turns["still_running"],
        )

    # -- model --------------------------------------------------------------
    err = model.get("error_rate")
    if err is not None and err > t.model_error_rate:
        classes = {
            m["model"]: m.get("errors", {})
            for m in report.get("model_usage", {}).get("models", [])
            if m.get("errors")
        }
        add(
            "warn",
            "model error rate above threshold",
            f"{model.get('error')}/{model.get('calls')} attempts failed.",
            rate=err,
            threshold=t.model_error_rate,
            error_classes=classes,
        )
    cache = model.get("cache_hit_rate")
    # A 4xx means the provider refused the request Grodex built: a client-side
    # defect, not an outage, so retrying cannot help. In this baseline every
    # affected turn then died on sampling_error.
    http_4xx = reliability.get("http_4xx_by_status") or {}
    finished = model.get("finished") or 0
    if http_4xx and finished:
        total_4xx = sum(http_4xx.values())
        share = total_4xx / finished
        affected = reliability.get("turns_with_http_4xx") or 0
        killed = reliability.get("turns_with_http_4xx_killed") or 0
        by_model = {
            m["model"]: m.get("errors_by_http_status", {})
            for m in report.get("model_usage", {}).get("models", [])
            if m.get("errors_by_http_status")
        }
        detail = (
            f"{total_4xx}/{finished} finished model calls ({_pct(share)}) were rejected "
            f"by the provider ({http_4xx}). A 4xx is a request-construction bug, not a "
            "transient failure, so retries cannot recover it."
        )
        if affected:
            detail += (
                f" {killed}/{affected} turns that hit one terminated on "
                "sampling_error — the turn dies mid-plan with its tool results already spent."
            )
        add(
            "critical" if share >= t.http_4xx_share else "warn",
            "provider rejects our requests with HTTP 4xx",
            detail,
            count=total_4xx,
            rate=share,
            threshold=t.http_4xx_share,
            by_status=http_4xx,
            by_model=by_model,
            affected_turns=affected,
            killed_turns=killed,
        )
    if cache is not None and cache < t.cache_hit_rate_floor and (model.get("calls") or 0) >= 50:
        add(
            "info",
            "prompt cache is barely hit",
            f"Only {_pct(cache)} of input tokens were served from cache across {model['calls']} calls.",
            rate=cache,
            threshold=t.cache_hit_rate_floor,
        )

    # -- tools --------------------------------------------------------------
    tool_err = tools.get("error_rate")
    if tool_err is not None and tool_err > t.tool_error_rate:
        worst = sorted(
            (x for x in tools.get("tools", []) if x["errors"]),
            key=lambda x: -(x["error_rate"] or 0),
        )[:5]
        add(
            "warn",
            "tool failure rate above threshold",
            f"{tools.get('errors')}/{tools.get('calls')} tool calls failed.",
            rate=tool_err,
            threshold=t.tool_error_rate,
            worst_tools=[{"tool": w["tool"], "rate": w["error_rate"], "errors": w["errors"]} for w in worst],
        )
    anomalies = reliability.get("anomalies", {})
    if (anomalies.get("indeterminate_tools") or 0) > 0:
        add(
            "critical",
            "indeterminate tool results",
            f"{anomalies['indeterminate_tools']} tool executions ended indeterminate — the agent could not "
            "decide whether the side effect happened, which is the one state crash recovery must never guess at.",
            count=anomalies["indeterminate_tools"],
            journal_events=reliability.get("tool_indeterminate_events"),
        )
    if (anomalies.get("uncommitted_results") or 0) > 0:
        add(
            "warn",
            "tool results finished but never committed",
            f"{anomalies['uncommitted_results']} results are uncommitted; invariant 7 says results "
            "must be persisted before the next sampling step.",
            count=anomalies["uncommitted_results"],
        )
    if (anomalies.get("stuck_tools") or 0) > 0:
        add(
            "warn",
            "tool executions that started and never finished",
            f"{anomalies['stuck_tools']} calls are stuck running.",
            count=anomalies["stuck_tools"],
        )

    # -- approvals ----------------------------------------------------------
    tax = tools.get("approval_tax")
    if tax is not None and tax > t.approval_tax:
        add(
            "warn",
            "human approval latency dominates tool time",
            f"{_pct(tax)} of the tool lifecycle is spent waiting for approval decisions "
            f"({approvals.get('approval_requests')} requests). This is a harness/UX cost, not a model cost.",
            rate=tax,
            threshold=t.approval_tax,
            top_tools=approvals.get("tools_most_approved"),
        )
    if (approvals.get("expired") or 0) > 0:
        add(
            "info",
            "approval tickets expired",
            f"{approvals['expired']} approvals expired without a decision.",
            count=approvals["expired"],
        )

    # -- context ------------------------------------------------------------
    mean_tokens = (context.get("prompt_input_tokens") or {}).get("mean")
    if mean_tokens is not None and mean_tokens > t.prompt_mean_tokens:
        add(
            "warn",
            "average prompt is very large",
            f"Mean estimated input is {mean_tokens:,.0f} tokens per model call "
            f"(max {(context.get('prompt_input_tokens') or {}).get('max') or 0:,.0f}), "
            f"across {(context.get('prompt_context_items') or {}).get('mean') or 0:.0f} context items. "
            "Large prompts inflate cost, TTFT, and compaction frequency.",
            mean_tokens=mean_tokens,
            threshold=t.prompt_mean_tokens,
        )
    comp_rate = context.get("compactions_per_100_turns")
    if comp_rate is not None and comp_rate > t.compactions_per_100_turns:
        add(
            "warn",
            "context compactions are frequent",
            f"{context.get('compactions')} compactions over {turns.get('turns')} turns "
            f"({comp_rate:.1f} per 100 turns); triggers={context.get('compaction_triggers')}.",
            rate=comp_rate,
            threshold=t.compactions_per_100_turns,
        )

    # -- memory -------------------------------------------------------------
    empty_rate = memory.get("empty_rate")
    if empty_rate is not None and empty_rate > t.memory_empty_rate:
        add(
            "warn",
            "memory retrieval usually returns nothing",
            f"{memory.get('empty_results')}/{memory.get('retrievals')} retrievals selected 0 items.",
            rate=empty_rate,
            threshold=t.memory_empty_rate,
        )
    for r in memory.get("routers", []):
        if (
            r["count"] >= t.router_always_empty_min_samples
            and r["empty_rate"] == 1.0
        ):
            add(
                "warn",
                f"router '{r['router_kind']}' never selects anything",
                f"{r['count']} retrievals via this router returned 0 items every time.",
                router=r["router_kind"],
                count=r["count"],
            )

    # -- capability projection ---------------------------------------------
    gap = capabilities.get("subagent_projection_gap")
    if gap and gap > 0:
        add(
            "warn",
            "delegated work is missing from the subagent projection",
            f"delegate_task was called {capabilities.get('delegate_task_calls')} times but "
            f"subagent_runs holds {capabilities.get('subagent_runs')} rows. Either the projection "
            "never records subagent lifecycle, or delegated runs are being dropped.",
            gap=gap,
        )

    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["title"]))
    return findings
