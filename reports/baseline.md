# Grodex telemetry baseline

- generated: `2026-09-19T13:55:34.129305+00:00`
- database: `/Users/zhengguilin/.grodex/telemetry.db` (28,124 KiB)
- window: **all time**
- sessions: **28**, turns: **253**, events: **13,482**
- last activity: `2026-09-19T13:55:33.979667+00:00`

## Findings

| severity | finding | evidence |
|---|---|---|
| CRITICAL | indeterminate tool results | `count`=3, `journal_events`=84 |
| CRITICAL | provider rejects our requests with HTTP 4xx | `count`=23, `rate`=0.02293, `threshold`=0.005, `by_status`={400:23}, `by_model`={deepseek-v4-flash:{400:23}}, `affected_turns`=23, `killed_turns`=23 |
| CRITICAL | repair loop exhausts budget on many turns | `rate`=0.3755, `threshold`=0.1, `turns`=95 |
| WARN | average prompt is very large | `mean_tokens`=5.766e+04, `threshold`=5e+04 |
| WARN | delegated work is missing from the subagent projection | `gap`=28 |
| WARN | human approval latency dominates tool time | `rate`=0.8448, `threshold`=0.25, `top_tools`={exec:201, read_file:71, grep:33, delegate_task:28, glob:16} |
| WARN | memory retrieval usually returns nothing | `rate`=0.9486, `threshold`=0.5 |
| WARN | model error rate above threshold | `rate`=0.02293, `threshold`=0.02, `error_classes`={deepseek-v4-flash:{api_error:23}} |
| WARN | router 'hybrid_rrf' never selects anything | `router`=hybrid_rrf, `count`=33 |
| WARN | tool executions that started and never finished | `count`=25 |
| WARN | tool failure rate above threshold | `rate`=0.0253, `threshold`=0.02, `worst_tools`=[{tool:read_artifact, rate:1, errors:1}, {tool:web_fetch, rate:0.3333, errors:5}, {tool:delegate_task, rate:0.1071, errors:3}, {tool:write_file, rate:0.09091, errors:3}, {tool:edit_file, rate:0.05714, errors:2}] |
| WARN | turn-level sampling errors | `rate`=0.09091, `threshold`=0.02 |
| INFO | approval tickets expired | `count`=1 |
| INFO | high cancellation rate | `rate`=0.1265, `threshold`=0.1 |
| INFO | turns unfinished at snapshot time | `count`=7 |

### [CRITICAL] indeterminate tool results

3 tool executions ended indeterminate — the agent could not decide whether the side effect happened, which is the one state crash recovery must never guess at.

### [CRITICAL] provider rejects our requests with HTTP 4xx

23/1003 finished model calls (2.3%) were rejected by the provider ({'400': 23}). A 4xx is a request-construction bug, not a transient failure, so retries cannot recover it. 23/23 turns that hit one terminated on sampling_error — the turn dies mid-plan with its tool results already spent.

### [CRITICAL] repair loop exhausts budget on many turns

95/253 turns ended with repair_exhausted instead of a final answer. The repair sampling path is a fallback for malformed model output; a rate this high means either the model keeps emitting invalid tool calls, or the repair trigger fires on ordinary question-answering turns.

### [WARN] average prompt is very large

Mean estimated input is 57,662 tokens per model call (max 108,730), across 105 context items. Large prompts inflate cost, TTFT, and compaction frequency.

### [WARN] delegated work is missing from the subagent projection

delegate_task was called 28 times but subagent_runs holds 0 rows. Either the projection never records subagent lifecycle, or delegated runs are being dropped.

### [WARN] human approval latency dominates tool time

84.5% of the tool lifecycle is spent waiting for approval decisions (372 requests). This is a harness/UX cost, not a model cost.

### [WARN] memory retrieval usually returns nothing

240/253 retrievals selected 0 items.

### [WARN] model error rate above threshold

23/1016 attempts failed.

### [WARN] router 'hybrid_rrf' never selects anything

33 retrievals via this router returned 0 items every time.

### [WARN] tool executions that started and never finished

25 calls are stuck running.

### [WARN] tool failure rate above threshold

25/988 tool calls failed.

### [WARN] turn-level sampling errors

23/253 turns terminated on sampling_error.

### [INFO] approval tickets expired

1 approvals expired without a decision.

### [INFO] high cancellation rate

12.6% of turns were cancelled (human-initiated or superseded).

### [INFO] turns unfinished at snapshot time

7 turns are still marked running; if the agent is live this is expected.


## Turn health

| metric | value |
|---|---|
| turns | 253 |
| final_answer rate | 37.5% |
| repair_exhausted rate | 37.5% |
| sampling_error rate | 9.1% |
| cancelled rate | 12.6% |
| still running | 7 |
| turn duration p50 / p90 | 14.56s / 109.46s |
| steps per turn (mean / p90) | 4.08 / 9.00 |
| tool calls per turn (mean / p90) | 3.74 / 12.00 |
| turns with compaction | 20 (7.9%) |

**Termination reasons**

| termination_reason | count | share |
|---|---|---|
| - | 7 | 2.8% |
| cancelled | 32 | 12.6% |
| final_answer | 95 | 37.5% |
| repair_exhausted | 95 | 37.5% |
| sampling_error | 23 | 9.1% |
| step_budget_exhausted | 1 | 0.4% |

## Model usage

| model | calls | error rate | TTFT p50 | latency p50 | cache hit | input tok | output tok | reasoning tok | retried calls |
|---|---|---|---|---|---|---|---|---|---|
| deepseek/deepseek-v4-flash | 1,015 | 2.3% | 747ms | 4.11s | 92.1% | 53,214,734 | 940,357 | 595,540 | 0 |
| -/- | 1 | 0.0% | n/a (n=1) | n/a (n=1) | 1.2% | 51,592 | 774 | 389 | 0 |

Totals: 1,016 calls, 54,207,457 tokens (input 53,266,326, cached 49,008,640, output 941,131, reasoning 595,929), cache hit 92.0%.

## Tools

988 calls, 25 failures (2.5%). Tool lifecycle 2560.21s (pure exec 399.64s), approval wait 2162.83s (**approval tax 84.5%**). Truncated results: 0.

| tool | calls | error rate | exec p50 | exec p90 | appr wait p50 | approvals | truncated |
|---|---|---|---|---|---|---|---|
| exec | 514 | 1.4% | 94ms | 587ms | 1.88s | 201 | 0 |
| read_file | 229 | 0.9% | 26ms | 39ms | 2.27s | 71 | 0 |
| grep | 110 | 1.8% | 34ms | 173ms | 3.33s | 33 | 0 |
| edit_file | 35 | 5.7% | 22ms | 30ms | 22.98s | 4 | 0 |
| write_file | 33 | 9.1% | 26ms | 42ms | 2.61s | 14 | 0 |
| delegate_task | 28 | 10.7% | 7.70s | 13.86s | 2.34s | 28 | 0 |
| glob | 18 | 0.0% | 34ms | 69ms | 1.51s | 16 | 0 |
| web_fetch | 15 | 33.3% | 199ms | 1.18s | n/a (n=1) | 1 | 0 |
| list_agents | 2 | 0.0% | n/a (n=2) | n/a (n=2) | n/a (n=2) | 2 | 0 |
| apply_patch | 2 | 0.0% | n/a (n=2) | n/a (n=2) | n/a (n=1) | 1 | 0 |
| load_skill | 1 | 0.0% | n/a (n=1) | n/a (n=1) | n/a (n=1) | 1 | 0 |
| read_artifact | 1 | 100.0% | n/a (n=1) | n/a (n=1) | - | 0 | 0 |

Status distribution: `committed`=969, `failed`=6, `succeeded`=4, `indeterminate`=3, `prepared`=3, `running`=3

## Approvals

| metric | value |
|---|---|
| approval requests | 372 |
| resolved | 392 |
| approved (incl. session grant) | 387 |
| rejected | 4 |
| expired | 1 |
| narrowed | 0 |
| rejection rate | 1.0% |
| session grants created | 88 |
| unresolved requests | 0 |
| approval wait p50 / p90 | 2.13s / 8.48s |

Most-approved tools: `exec`=201, `read_file`=71, `grep`=33, `delegate_task`=28, `glob`=16, `write_file`=14, `edit_file`=4, `list_agents`=2, `apply_patch`=1, `load_skill`=1

## Context & compaction

| metric | value |
|---|---|
| prompt builds | 1,015 |
| estimated input tokens (mean / p50) | 57,662 / 59,077 |
| estimated input tokens (p90 / max) | 97,320 / 108,730 |
| context items (mean / p90) | 105.10 / 220.20 |
| compactions | 23 |
| compactions per 100 turns | 9.09 |
| sessions with compaction | 5 / 28 |
| triggers | {'token_budget': 23} |

## Memory retrieval

| metric | value |
|---|---|
| retrievals | 253 |
| empty results | 240 (94.9%) |
| selected items (mean / p50) | 0.15 / 0.00 |
| retrieval latency p50 / p90 | 2ms / 3.95s |

| router | count | empty rate | mean selected | latency p50 |
|---|---|---|---|---|
| 3way_hybrid_rrf | 220 | 94.1% | 0.18 | 2ms |
| hybrid_rrf | 33 | 100.0% | 0.00 | 0ms |

## Reliability

| metric | value |
|---|---|
| error-severity events | 46 |
| open turns | 7 |
| stuck tools | 25 |
| uncommitted results | 0 |
| indeterminate tools | 3 |
| tool_indeterminate journal events | 84 |
| capability_stale decisions | 1 |

Errors by event kind: `tool_finished`=16, `tool_result_committed`=30

## Subagents, skills, MCP

| metric | value |
|---|---|
| subagent runs | 0 |
| delegate_task calls | 28 |
| subagent projection gap | 28 |
| subagent status | {} |
| skill activations | 89 |
| mcp events | 0 |

Top skills: `aurora-article-publisher`=89

## Cost

no price table supplied (--prices); token counts are reported without cost

## Appendix: projection coverage

| table | rows |
|---|---|
| sessions | 28 |
| turns | 253 |
| model_attempts | 1,016 |
| tool_executions | 987 |
| security_decisions | 2,102 |
| prompt_builds | 1,015 |
| compactions | 23 |
| subagent_runs | 0 |
| skill_activations | 89 |
| memory_retrievals | 253 |
| mcp_lifecycle | 0 |
| telemetry_events | 13,477 |
