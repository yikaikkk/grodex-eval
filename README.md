# Grodex eval

Evaluation harness for the [Grodex](../grodex) agent.

**This repo never modifies Grodex.** It does not import Grodex code, does not
link against its crates, and opens `telemetry.db` strictly read-only. Grodex
is treated as a black box that happens to leave a rich audit trail behind.

## Status

| Phase | Scope | State |
|---|---|---|
| **0** | Telemetry baseline — derive quality metrics from the existing SQLite projection | **done** |
| 1 | Task runner (drive the agent headlessly, grade the result) | in progress — harness written, upstream bug found (see *Upstream defects*) |
| 2 | Fault injection (crash/resume, provider failover, sandbox, permissions) | not started |
| 3 | External benchmark adapters (SWE-bench, Terminal-Bench, ...) | not started |

## Phase 0: what it does

Grodex already projects every lifecycle event into `~/.grodex/telemetry.db`
(`turns`, `model_attempts`, `tool_executions`, `security_decisions`,
`compactions`, `memory_retrievals`, ...). Phase 0 turns those rows into the
numbers you need before you start changing anything:

* **Turn health** — `final_answer` vs `repair_exhausted` vs `sampling_error`
  vs `cancelled`, and steps/tool-calls per turn.
* **Model usage** — TTFT, latency, error classes, retries, token totals,
  prompt-cache hit rate.
* **Tools** — per-tool error rate and latency, plus the **approval tax**
  (share of the tool lifecycle spent waiting for a human).
* **Approvals** — request/resolve/reject/expire/narrow counts and which tools
  demand the most attention.
* **Context** — prompt size distribution and compaction frequency.
* **Memory** — retrieval latency, empty-result rate, per-router breakdown.
* **Reliability** — indeterminate / stuck / uncommitted tool executions and
  error-severity events.
* **Findings** — a small, explicit rule set that turns the metrics into a
  ranked list of "fix this first", each finding carrying its evidence and the
  threshold that fired.

Two design rules worth keeping:

1. A rate is `None`, never `0.0`, when its denominator is empty. "No data"
   and "zero percent" are different findings.
2. No costs are invented. Token counts are always reported; money is only
   computed when you supply a price table via `--prices`.

## Usage

No install, no dependencies — stdlib Python 3.11+.

```bash
# short summary in the terminal + artifacts on disk
python3 -m grodex_eval telemetry --md reports/baseline.md --json reports/baseline.json

# only the last 7 days
python3 -m grodex_eval telemetry --days 7

# explicit window (UTC, `until` is exclusive)
python3 -m grodex_eval telemetry --since 2026-09-01 --until 2026-09-20

# full markdown to stdout
python3 -m grodex_eval telemetry --print-md

# projection coverage / schema sanity check
python3 -m grodex_eval tables
```

Useful flags: `--db PATH` (default `~/.grodex/telemetry.db`), `--top N`
(rows per table), `--prices prices.json`.

### Price table format

```json
{
  "deepseek": {
    "some-model": {
      "input_per_mtok": 0.27,
      "cached_input_per_mtok": 0.07,
      "output_per_mtok": 1.10,
      "currency": "USD"
    }
  }
}
```

## Metrics tests

```bash
python3 -m unittest discover -s tests -t .
```

The tests build a synthetic telemetry DB mirroring the real projection
columns, so they double as a **schema-compatibility contract**: if Grodex
renames a projection column, the fixture and `grodex_eval/metrics.py` have to
move together. There is also a test asserting the DB is opened read-only.

## Layout

```
grodex_eval/
├── db.py         read-only SQLite access, time windows (julianday-based)
├── stats.py      percentiles, rate(), small aggregations
├── metrics.py    one pure function per metric section
├── insights.py   threshold rules -> ranked findings
├── report.py     Markdown + console rendering
└── cli.py        argument parsing and output wiring
tests/            synthetic-DB tests (unittest)
reports/          generated artifacts, gitignored (baseline.md / baseline.json)
```

## Upstream defects found by this harness

Driving `grodex serve` against a real provider surfaced a bug the telemetry
baseline had already ranked `[CRITICAL] provider rejects our requests with
HTTP 4xx`. The two views agree: 23 of 1003 model calls answered 4xx, and in
all 23 cases the turn ended `termination_reason: sampling_error`.

**Root cause.** DeepSeek thinking mode requires `reasoning_content` to be
echoed back on any assistant message carrying `tool_calls`. Grodex attaches it
only when the string is non-empty — `grodex-sampler/src/client.rs`
(`build_chat_body` / `map_chat_item`) gates on `.is_empty()`. On a step that
reports `reasoning_tokens: 0` the field is dropped and the provider answers
deterministically:

```
400 The reasoning_content in the thinking mode must be passed back to the API
```

Grodex then records `api_error` and kills the turn mid-plan, discarding the
tool results it had already paid for.

**Evidence.** Three independent probes: (a) a logging proxy captured the exact
request bodies Grodex sends; (b) replaying a captured body *with* the field
returned 200 while the same body *without* it returned the identical 400
(streaming on or off made no difference), isolating the omitted field as the
trigger; (c) the telemetry correlation above.

**Workaround.** Pin the sandbox to a non-thinking model in its own
`config.toml` (`model = "deepseek-chat"`). Smoke test: 3/3 turns green, 0
errors. Note that `GRODEX_MODEL` / `GRODEX_API_ENDPOINT` **cannot** be used to
reach this — `grodex-cli/src/runtime.rs` resolves config over env with
`.or_else`, so the file always wins.

**Probes.** `scripts/http_probe_proxy.py` (logging forward proxy),
`scripts/replay_body.py` (ablation replay), `scripts/probe_reasoning.py`
(matrix probe), `scripts/smoke_acp.py` (headless ACP smoke test).

## Notes on interpretation

* The telemetry DB is written by a **live** agent. Snapshot the report
  timestamp when comparing runs, and prefer `--days` over "all time" for
  regression tracking.
* Single-run numbers are noisy at these sample sizes. Treat rates as
  directional and re-run after changes with the same window.
* `subagent_runs` and `mcp_lifecycle` are empty in some databases. That is a
  finding in itself (see the projection-gap rule), not a tool failure.
