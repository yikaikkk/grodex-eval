"""Metric tests against a synthetic telemetry database.

The fixture mirrors the column subset the analyzer reads, so these tests are
also a schema-compatibility contract: if Grodex changes a projection column,
the fixture and the analyzer have to move together.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from grodex_eval.db import Window, open_snapshot
from grodex_eval.insights import Thresholds, derive_insights
from grodex_eval.metrics import collect

SCHEMA = """
CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT
);
CREATE TABLE turns (
  turn_id TEXT PRIMARY KEY, session_id TEXT, started_at TEXT, finished_at TEXT,
  status TEXT, termination_reason TEXT, duration_ms INTEGER, steps INTEGER,
  model_calls INTEGER, tool_calls INTEGER, compactions INTEGER
);
CREATE TABLE model_attempts (
  attempt_id TEXT PRIMARY KEY, provider TEXT, model TEXT, status TEXT, error_class TEXT,
  duration_ms INTEGER, first_token_ms INTEGER, attempts INTEGER,
  input_tokens INTEGER, cached_input_tokens INTEGER, cache_creation_tokens INTEGER,
  output_tokens INTEGER, reasoning_tokens INTEGER, total_tokens INTEGER,
  started_at TEXT, finished_at TEXT, turn_id TEXT, http_status INTEGER
);
CREATE TABLE tool_executions (
  session_id TEXT, call_id TEXT, turn_id TEXT, tool_name TEXT, status TEXT,
  is_error INTEGER, exit_code INTEGER, duration_ms INTEGER, approval_wait_ms INTEGER,
  output_truncated INTEGER, prepared_at TEXT, started_at TEXT, finished_at TEXT,
  committed_at TEXT, PRIMARY KEY (session_id, call_id)
);
CREATE TABLE security_decisions (
  decision_id TEXT PRIMARY KEY, decision_type TEXT, decision TEXT, tool_name TEXT,
  occurred_at TEXT
);
CREATE TABLE prompt_builds (
  prompt_id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT,
  context_item_count INTEGER, estimated_input_tokens INTEGER, built_at TEXT
);
CREATE TABLE compactions (
  compaction_id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT, trigger TEXT,
  status TEXT, pre_item_count INTEGER, candidate_item_count INTEGER,
  started_at TEXT, finished_at TEXT, committed_at TEXT
);
CREATE TABLE memory_retrievals (
  retrieval_id TEXT PRIMARY KEY, router_kind TEXT, selected_count INTEGER,
  duration_ms INTEGER, query_chars INTEGER, occurred_at TEXT
);
CREATE TABLE telemetry_events (
  event_id TEXT PRIMARY KEY, kind TEXT, severity TEXT, occurred_at TEXT
);
CREATE TABLE subagent_runs (
  session_id TEXT, task_id TEXT, status TEXT, started_at TEXT, finished_at TEXT,
  PRIMARY KEY (session_id, task_id)
);
CREATE TABLE skill_activations (
  activation_id TEXT PRIMARY KEY, skill_name TEXT, loaded_at TEXT
);
CREATE TABLE mcp_lifecycle (
  event_id TEXT PRIMARY KEY, server_name TEXT, phase TEXT, status TEXT, occurred_at TEXT
);
"""


def build_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)

    conn.execute("INSERT INTO sessions VALUES ('s1','2026-01-02T00:00:00+00:00',NULL)")
    conn.execute("INSERT INTO sessions VALUES ('s2','2026-01-03T00:00:00+00:00',NULL)")

    # 10 in-window turns: 6 final_answer, 3 repair_exhausted, 1 cancelled,
    # plus 1 out-of-window turn used by the window-filter test.
    turns = []
    for i in range(6):
        turns.append((f"t{i}", "s1", "2026-01-02T00:00:00+00:00", "2026-01-02T00:01:00+00:00",
                      "completed", "final_answer", 60_000, 3, 3, 2, 0))
    for i in range(6, 9):
        turns.append((f"t{i}", "s1", "2026-01-02T00:00:00+00:00", "2026-01-02T00:02:00+00:00",
                      "completed", "repair_exhausted", 120_000, 5, 5, 4, 1))
    turns.append(("t9", "s2", "2026-01-03T00:00:00+00:00", "2026-01-03T00:00:10+00:00",
                  "cancelled", "cancelled", 10_000, 1, 1, 0, 0))
    turns.append(("old", "s2", "2025-01-01T00:00:00+00:00", "2025-01-01T00:00:01+00:00",
                  "completed", "final_answer", 1_000, 1, 1, 0, 0))
    conn.executemany("INSERT INTO turns VALUES (?,?,?,?,?,?,?,?,?,?,?)", turns)

    # Model: 2 ok (with cache hits), 1 error, 1 still running.
    # error_rate denominator must be ok+error = 3 -> 1/3.
    # a3 is the HTTP 400 rejection (thinking-mode reasoning_content dropped).
    attempts = [
        ("a1", "p", "m", "ok", None, 1000, 100, 1, 100, 50, 0, 10, 0, 110, "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:01+00:00", "t0", None),
        ("a2", "p", "m", "ok", None, 2000, 200, 1, 100, 50, 0, 10, 0, 110, "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:02+00:00", "t0", None),
        ("a3", "p", "m", "error", "api_error", 500, None, 2, None, None, None, None, None, None, "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:01+00:00", "t7", 400),
        ("a4", "p", "m", "running", None, None, None, 1, None, None, None, None, None, None, "2026-01-02T00:00:00+00:00", None, "t0", None),
    ]
    conn.executemany("INSERT INTO model_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", attempts)

    # Tools. `duration_ms` spans prepared -> finished, so it CONTAINS the
    # approval wait: c1 waited 900ms then ran 100ms, c2 waited 100ms then ran
    # 400ms. Lifecycle = 1000+500+50 = 1550, wait = 1000, pure exec = 550.
    tools = [
        ("s1", "c1", "t0", "read_file", "committed", 0, 0, 1000, 900, 0, "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:01+00:00", "2026-01-02T00:00:02+00:00", "2026-01-02T00:00:02+00:00"),
        ("s1", "c2", "t0", "exec", "committed", 0, 0, 500, 100, 0, "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:01+00:00", "2026-01-02T00:00:02+00:00", "2026-01-02T00:00:02+00:00"),
        ("s1", "c3", "t1", "exec", "failed", 1, 1, 50, None, 0, "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:01+00:00", "2026-01-02T00:00:02+00:00", None),
        ("s1", "c4", "t2", "exec", "indeterminate", 0, None, None, None, 0, "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:03+00:00", None, None),
    ]
    conn.executemany("INSERT INTO tool_executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tools)

    conn.executemany(
        "INSERT INTO security_decisions VALUES (?,?,?,?,?)",
        [
            ("d1", "approval_requested", None, "exec", "2026-01-02T00:00:00+00:00"),
            ("d2", "approval_requested", None, "exec", "2026-01-02T00:00:00+00:00"),
            ("d3", "approval_requested", None, "read_file", "2026-01-02T00:00:00+00:00"),
            ("d4", "approval_resolved", "approved", "exec", "2026-01-02T00:00:00+00:00"),
            ("d5", "approval_resolved", "approved", "exec", "2026-01-02T00:00:00+00:00"),
            ("d6", "approval_resolved", "rejected", "read_file", "2026-01-02T00:00:00+00:00"),
        ],
    )

    conn.executemany(
        "INSERT INTO prompt_builds VALUES (?,?,?,?,?,?)",
        [
            ("p1", "s1", "t0", 10, 1_000, "2026-01-02T00:00:00+00:00"),
            ("p2", "s1", "t0", 20, 3_000, "2026-01-02T00:00:00+00:00"),
        ],
    )
    conn.execute(
        "INSERT INTO compactions VALUES ('k1','s1','t6','token_budget','committed',100,40,"
        "'2026-01-02T00:00:00+00:00','2026-01-02T00:00:01+00:00','2026-01-02T00:00:01+00:00')"
    )

    conn.executemany(
        "INSERT INTO memory_retrievals VALUES (?,?,?,?,?,?)",
        [
            ("r1", "3way", 2, 100, 40, "2026-01-02T00:00:00+00:00"),
            ("r2", "3way", 0, 200, 40, "2026-01-02T00:00:00+00:00"),
            ("r3", "hybrid", 0, 1, 40, "2026-01-02T00:00:00+00:00"),
            ("r4", "hybrid", 0, 1, 40, "2026-01-02T00:00:00+00:00"),
        ],
    )

    conn.executemany(
        "INSERT INTO telemetry_events VALUES (?,?,?,?)",
        [
            ("e1", "tool_indeterminate", "info", "2026-01-02T00:00:00+00:00"),
            ("e2", "session_grant_created", "info", "2026-01-02T00:00:00+00:00"),
            ("e3", "boom", "error", "2026-01-02T00:00:00+00:00"),
        ],
    )
    conn.commit()
    conn.close()


def build_tool_db(path: Path, rows: list[tuple]) -> None:
    """Build a DB containing only tool_executions rows."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO tool_executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


class MetricsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._tmp.name) / "telemetry.db"
        build_db(cls.db_path)
        # Windowed to the 10 in-window turns; the 2025 turn only appears in
        # the explicit all-time window test.
        cls.snap = open_snapshot(cls.db_path, Window.from_args(since="2026-01-01"))
        cls.report = collect(cls.snap)

    @classmethod
    def tearDownClass(cls):
        cls.snap.close()
        cls._tmp.cleanup()

    # -- window -------------------------------------------------------------
    def test_window_excludes_old_rows(self):
        scoped = open_snapshot(self.db_path, Window.from_args(since="2026-01-01"))
        try:
            report = collect(scoped)
        finally:
            scoped.close()
        self.assertEqual(report["turn_health"]["turns"], 10)
        # The default class-level snapshot is already windowed to the same 10.
        self.assertEqual(self.report["turn_health"]["turns"], 10)
        all_time = open_snapshot(self.db_path, Window())
        try:
            everything = collect(all_time)
        finally:
            all_time.close()
        self.assertEqual(everything["turn_health"]["turns"], 11)

    def test_until_is_exclusive(self):
        scoped = open_snapshot(self.db_path, Window.from_args(until="2026-01-03"))
        try:
            report = collect(scoped)
        finally:
            scoped.close()
        self.assertEqual(report["turn_health"]["turns"], 10)

    # -- turn health --------------------------------------------------------
    def test_turn_health_rates(self):
        turns = self.report["turn_health"]
        self.assertAlmostEqual(turns["final_answer_rate"], 6 / 10)
        self.assertAlmostEqual(turns["repair_exhausted_rate"], 3 / 10)
        self.assertAlmostEqual(turns["cancel_rate"], 1 / 10)
        self.assertEqual(turns["termination_reasons"]["final_answer"], 6)

    def test_turns_with_compaction(self):
        self.assertAlmostEqual(self.report["turn_health"]["compaction_turn_rate"], 3 / 10)

    # -- model --------------------------------------------------------------
    def test_model_error_rate_ignores_running_attempts(self):
        totals = self.report["model_usage"]["totals"]
        self.assertEqual(totals["calls"], 4)
        # denominator is ok+error = 3, so the running attempt does not dilute it
        self.assertAlmostEqual(totals["error_rate"], 1 / 3)

    def test_cache_hit_rate(self):
        self.assertAlmostEqual(self.report["model_usage"]["totals"]["cache_hit_rate"], 0.5)

    def test_retry_counter(self):
        model = self.report["model_usage"]["models"][0]
        self.assertEqual(model["calls_needing_retry"], 1)
        self.assertEqual(model["max_attempts"], 2)

    # -- tools --------------------------------------------------------------
    def test_approval_tax(self):
        tools = self.report["tool_usage"]
        self.assertAlmostEqual(tools["total_approval_wait_ms"], 1000)
        self.assertAlmostEqual(tools["total_lifecycle_ms"], 1550)
        self.assertAlmostEqual(tools["total_pure_exec_ms"], 550)
        # Duration already contains the wait, so the denominator is the
        # lifecycle itself -- NOT lifecycle + wait (which would double-count).
        self.assertAlmostEqual(tools["approval_tax"], 1000 / 1550)

    def test_lifecycle_decomposes_into_wait_plus_exec(self):
        tools = self.report["tool_usage"]
        self.assertAlmostEqual(
            tools["total_lifecycle_ms"],
            tools["total_pure_exec_ms"] + tools["total_approval_wait_ms"],
        )

    def test_tool_error_rate_counts_failed_and_indeterminate(self):
        tools = self.report["tool_usage"]
        self.assertEqual(tools["calls"], 4)
        self.assertEqual(tools["errors"], 2)  # one `failed`, one `indeterminate`
        self.assertAlmostEqual(tools["error_rate"], 0.5)

    # -- approvals ----------------------------------------------------------
    def test_approval_breakdown(self):
        approvals = self.report["approvals"]
        self.assertEqual(approvals["approval_requests"], 3)
        self.assertEqual(approvals["rejected"], 1)
        self.assertAlmostEqual(approvals["rejection_rate"], 1 / 3)
        self.assertEqual(approvals["session_grants"], 1)
        self.assertEqual(approvals["tools_most_approved"]["exec"], 2)

    # -- context / memory ---------------------------------------------------
    def test_prompt_token_summary(self):
        prompt = self.report["context"]["prompt_input_tokens"]
        self.assertEqual(prompt["count"], 2)
        self.assertAlmostEqual(prompt["mean"], 2000)

    def test_memory_empty_rate(self):
        memory = self.report["memory"]
        self.assertEqual(memory["retrievals"], 4)
        self.assertAlmostEqual(memory["empty_rate"], 0.75)

    # -- reliability --------------------------------------------------------
    def test_anomalies(self):
        anomalies = self.report["reliability"]["anomalies"]
        self.assertEqual(anomalies["indeterminate_tools"], 1)
        self.assertEqual(anomalies["stuck_tools"], 1)  # started, never finished
        self.assertEqual(anomalies["uncommitted_results"], 1)  # c3 has no committed_at
        self.assertEqual(anomalies["open_turns"], 0)

    # -- cost ---------------------------------------------------------------
    def test_cost_requires_prices(self):
        self.assertFalse(self.report["cost"]["priced"])

    def test_cost_with_price_table(self):
        prices = {"p": {"m": {"input_per_mtok": 1.0, "cached_input_per_mtok": 0.5, "output_per_mtok": 2.0}}}
        priced = collect(self.snap, prices)
        # billable input = 200 - 100 = 100 -> 0.0001 ; cached 100 -> 0.00005 ;
        # output 20 -> 0.00004
        self.assertAlmostEqual(priced["cost"]["total"], 0.0001 + 0.00005 + 0.00004)


class InsightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._tmp.name) / "telemetry.db"
        build_db(cls.db_path)
        # Windowed to the 10 in-window turns; the 2025 turn only appears in
        # the explicit all-time window test.
        cls.snap = open_snapshot(cls.db_path, Window.from_args(since="2026-01-01"))
        cls.report = collect(cls.snap)
        cls.findings = derive_insights(cls.report)

    @classmethod
    def tearDownClass(cls):
        cls.snap.close()
        cls._tmp.cleanup()

    def _titles(self):
        return [f["title"] for f in self.findings]

    def test_flags_repair_exhausted(self):
        self.assertIn("repair loop exhausts budget on many turns", self._titles())

    def test_flags_indeterminate_tools(self):
        self.assertIn("indeterminate tool results", self._titles())

    def test_flags_approval_dominance(self):
        self.assertIn("human approval latency dominates tool time", self._titles())

    def test_findings_are_severity_ordered(self):
        order = {"critical": 0, "warn": 1, "info": 2}
        severities = [order[f["severity"]] for f in self.findings]
        self.assertEqual(severities, sorted(severities))

    def test_every_finding_carries_evidence(self):
        for f in self.findings:
            self.assertTrue(f["evidence"], f"finding without evidence: {f['title']}")

    def test_thresholds_are_tunable(self):
        relaxed = derive_insights(
            self.report,
            Thresholds(repair_exhausted_rate=0.99, approval_tax=0.99, tool_error_rate=0.99,
                       prompt_mean_tokens=1e12, memory_empty_rate=0.99,
                       model_error_rate=0.99, sampling_error_rate=0.99),
        )
        self.assertNotIn("repair loop exhausts budget on many turns", [f["title"] for f in relaxed])

    def test_flags_provider_4xx_rejections(self):
        title = "provider rejects our requests with HTTP 4xx"
        self.assertIn(title, self._titles())
        finding = next(f for f in self.findings if f["title"] == title)
        self.assertEqual(finding["severity"], "critical")
        self.assertEqual(finding["evidence"]["by_status"], {"400": 1})
        self.assertEqual(self.report["model_usage"]["models"][0]["errors_by_http_status"], {"400": 1})

    def test_http_4xx_finding_names_the_turns_it_kills(self):
        report = {
            "model_usage": {
                "totals": {"finished": 100, "error_rate": 0.05},
                "models": [{"model": "m", "errors_by_http_status": {"400": 5}}],
            },
            "reliability": {
                "http_4xx_by_status": {"400": 5},
                "turns_with_http_4xx": 5,
                "turns_with_http_4xx_killed": 4,
            },
        }
        finding = next(
            f for f in derive_insights(report)
            if f["title"] == "provider rejects our requests with HTTP 4xx"
        )
        self.assertIn("4/5 turns", finding["detail"])
        self.assertIn("sampling_error", finding["detail"])
        self.assertEqual(finding["evidence"]["killed_turns"], 4)


class ToolLifecycleEdgeTests(unittest.TestCase):
    """Paths that only show up on real, messy telemetry.

    `duration_ms` spans prepared -> finished, so it already contains
    `approval_wait_ms`. Two exceptions exist in live data and both must be
    handled without corrupting the totals.
    """

    TS = "2026-01-02T00:00:00+00:00"

    def _collect(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            build_tool_db(db, rows)
            snap = open_snapshot(db, Window())
            try:
                return collect(snap)["tool_usage"]
            finally:
                snap.close()

    def test_expired_ticket_waits_without_executing(self):
        # e2 waited 400ms for a decision and then never ran: the wait is real
        # elapsed time and must still count toward the lifecycle.
        tools = self._collect(
            [
                ("sx", "e1", "tx", "exec", "committed", 0, 0, 300, 250, 0, self.TS, self.TS, self.TS, self.TS),
                ("sx", "e2", "tx", "exec", "prepared", 0, None, None, 400, 0, self.TS, None, None, None),
            ]
        )
        self.assertEqual(tools["orphan_waits"], 1)
        self.assertAlmostEqual(tools["total_approval_wait_ms"], 650)
        self.assertAlmostEqual(tools["total_lifecycle_ms"], 700)  # 300 + 400
        self.assertAlmostEqual(tools["total_pure_exec_ms"], 50)  # only e1 ran

    def test_wait_longer_than_duration_is_flagged_not_negative(self):
        # e3 reports a 500ms wait inside a 100ms duration -- impossible if the
        # wait were inside the span. Clamp it and flag it instead of emitting a
        # negative "exec" time.
        tools = self._collect(
            [
                ("sx", "e3", "tx", "exec", "committed", 0, 0, 100, 500, 0, self.TS, None, self.TS, None),
            ]
        )
        self.assertEqual(tools["inconsistent_timing"], 1)
        self.assertAlmostEqual(tools["total_pure_exec_ms"], 0)
        self.assertAlmostEqual(tools["total_lifecycle_ms"], 100)

    def test_approval_tax_never_double_counts_the_wait(self):
        tools = self._collect(
            [
                ("sx", "e1", "tx", "exec", "committed", 0, 0, 1000, 1000, 0, self.TS, self.TS, self.TS, self.TS),
            ]
        )
        # Entirely wait: the old `wait/(wait+duration)` formula would have
        # reported 50% here.
        self.assertAlmostEqual(tools["approval_tax"], 1.0)


class ReadOnlyTests(unittest.TestCase):
    def test_database_is_opened_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "telemetry.db"
            build_db(db_path)
            snap = open_snapshot(db_path)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    snap.conn.execute("CREATE TABLE should_not_exist (x)")
            finally:
                snap.close()


if __name__ == "__main__":
    unittest.main()
