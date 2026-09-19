"""Command line entry point for grodex-eval."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .db import DEFAULT_DB, EXPECTED_TABLES, Window, open_snapshot
from .insights import Thresholds, derive_insights
from .metrics import collect
from .report import render_console, render_markdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grodex-eval",
        description="Evaluation harness for Grodex. Phase 0: telemetry baseline.",
    )
    parser.add_argument("--version", action="version", version=f"grodex-eval {__version__}")
    sub = parser.add_subparsers(dest="command")

    tel = sub.add_parser("telemetry", help="scan the telemetry projection and report baseline metrics")
    tel.add_argument("--db", default=str(DEFAULT_DB), help=f"telemetry.db path (default: {DEFAULT_DB})")
    scope = tel.add_argument_group("time window")
    scope.add_argument("--days", type=float, help="only consider the last N days")
    scope.add_argument("--since", help="start bound, ISO8601 or YYYY-MM-DD (UTC)")
    scope.add_argument("--until", help="end bound, ISO8601 or YYYY-MM-DD (UTC, exclusive)")
    out = tel.add_argument_group("output")
    out.add_argument("--md", help="write the Markdown report to this path")
    out.add_argument("--json", dest="json_path", help="write the raw metrics as JSON to this path")
    out.add_argument("--print-md", action="store_true", help="also print the Markdown report to stdout")
    out.add_argument("--prices", help="JSON price table for cost estimation (optional)")
    out.add_argument("--top", type=int, default=15, help="max rows per table (default 15)")

    tables = sub.add_parser("tables", help="show projection coverage for the telemetry database")
    tables.add_argument("--db", default=str(DEFAULT_DB), help=f"telemetry.db path (default: {DEFAULT_DB})")
    return parser


def _load_prices(path: str | None) -> dict:
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cmd_telemetry(args: argparse.Namespace) -> int:
    window = Window.from_args(since=args.since, until=args.until, days=args.days)
    prices = _load_prices(args.prices)
    try:
        snap = open_snapshot(args.db, window=window)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        report = collect(snap, prices)
    finally:
        snap.close()

    findings = derive_insights(report)
    report["findings"] = findings
    markdown = render_markdown(report, findings, top_n=args.top)

    if args.md:
        Path(args.md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md).write_text(markdown, encoding="utf-8")
    if args.json_path:
        Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_path).write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
    if args.print_md:
        print(markdown)
    else:
        print(render_console(report, findings))
        if args.md:
            print(f"\nmarkdown report: {args.md}")
        if args.json_path:
            print(f"json metrics:    {args.json_path}")
    return 0


def cmd_tables(args: argparse.Namespace) -> int:
    snap = open_snapshot(args.db)
    try:
        print(f"database: {snap.path}")
        for table in EXPECTED_TABLES:
            if not snap.has_table(table):
                print(f"  {table:22} MISSING")
                continue
            print(f"  {table:22} {snap.count(table):>8}")
    finally:
        snap.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in (None, "telemetry"):
        if args.command is None:
            # Default to the telemetry report rather than printing help.
            args = parser.parse_args(["telemetry"])
        return cmd_telemetry(args)
    if args.command == "tables":
        return cmd_tables(args)
    parser.print_help()
    return 1
