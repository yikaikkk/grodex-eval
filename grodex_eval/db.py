"""Read-only access to the Grodex telemetry database.

The database belongs to the running agent, so this module is strictly
read-only: we open with SQLite's `mode=ro` URI and additionally set
`PRAGMA query_only`. Nothing here ever writes.

Timestamp handling
------------------
Grodex writes RFC3339 UTC strings (`2026-09-19T11:58:06.268241+00:00`).
Those are not lexicographically comparable across differing fractional
precision, so all range filtering goes through `julianday()`, which SQLite
parses correctly for this format.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

DEFAULT_DB = Path.home() / ".grodex" / "telemetry.db"

# The column that carries "when did this happen" for each projection table.
# Tool executions may fall back through the lifecycle if the earlier
# timestamps were never projected.
TIME_EXPR: dict[str, str] = {
    "sessions": "COALESCE(started_at, finished_at)",
    "turns": "COALESCE(started_at, finished_at)",
    "model_attempts": "COALESCE(started_at, finished_at)",
    "tool_executions": "COALESCE(prepared_at, started_at, finished_at)",
    "security_decisions": "occurred_at",
    "compactions": "COALESCE(started_at, finished_at)",
    "memory_retrievals": "occurred_at",
    "prompt_builds": "built_at",
    "skill_activations": "loaded_at",
    "subagent_runs": "COALESCE(started_at, finished_at)",
    "mcp_lifecycle": "occurred_at",
    "telemetry_events": "occurred_at",
}

# Tables the baseline report expects. Missing ones are reported explicitly
# rather than silently skewing a rate.
EXPECTED_TABLES: tuple[str, ...] = (
    "sessions",
    "turns",
    "model_attempts",
    "tool_executions",
    "security_decisions",
    "prompt_builds",
    "compactions",
    "subagent_runs",
    "skill_activations",
    "memory_retrievals",
    "mcp_lifecycle",
    "telemetry_events",
)


def _parse_iso(value: str) -> datetime:
    """Parse a user-supplied bound. `YYYY-MM-DD` is treated as UTC midnight."""
    text = value.strip()
    if len(text) == 10:  # bare date
        text = text + "T00:00:00+00:00"
    # Python 3.11+ understands the trailing "+00:00" offset.
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class Window:
    """Half-open [since, until) time window, stored as RFC3339 UTC strings."""

    since: str | None = None
    until: str | None = None
    label: str = "all"

    @classmethod
    def from_args(
        cls,
        since: str | None = None,
        until: str | None = None,
        days: float | None = None,
        now: datetime | None = None,
    ) -> "Window":
        now = now or datetime.now(timezone.utc)
        if days is not None:
            since = (now - timedelta(days=days)).isoformat()
        since_s = _parse_iso(since).isoformat() if since else None
        until_s = _parse_iso(until).isoformat() if until else None
        if days is not None and since:
            label = f"last {days:g}d"
        elif since and until:
            label = f"{since_s[:10]} .. {until_s[:10]}"
        elif since:
            label = f"since {since_s[:10]}"
        elif until:
            label = f"until {until_s[:10]}"
        else:
            label = "all time"
        return cls(since=since_s, until=until_s, label=label)

    def predicate(self, table: str) -> tuple[str, list[Any]]:
        """Return a SQL predicate + params scoping `table` to this window."""
        expr = TIME_EXPR.get(table)
        if expr is None or (self.since is None and self.until is None):
            return "", []
        parts: list[str] = []
        params: list[Any] = []
        if self.since is not None:
            parts.append(f"julianday({expr}) >= julianday(?)")
            params.append(self.since)
        if self.until is not None:
            parts.append(f"julianday({expr}) < julianday(?)")
            params.append(self.until)
        return " AND ".join(parts), params


@dataclass
class Snapshot:
    """A read-only handle plus windowed query helpers."""

    conn: sqlite3.Connection
    path: str
    window: Window = field(default_factory=Window)
    _tables: set[str] = field(default_factory=set)

    # -- schema introspection -------------------------------------------------
    def tables(self) -> set[str]:
        if not self._tables:
            rows = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
            self._tables = {str(r[0]) for r in rows}
        return self._tables

    def has_table(self, name: str) -> bool:
        return name in self.tables()

    def columns(self, table: str) -> set[str]:
        if not self.has_table(table):
            return set()
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r[1]) for r in rows}

    # -- query helpers --------------------------------------------------------
    def rows(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, list(params)).fetchall()

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, list(params)).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.one(sql, params)
        if row is None or row[0] is None:
            return default
        return row[0]

    def count(self, table: str, extra: Sequence[str] = (), params: Sequence[Any] = ()) -> int:
        if not self.has_table(table):
            return 0
        return int(self.select(table, "COUNT(*)", extra=extra, params=params)[0][0])

    def select(
        self,
        table: str,
        cols: str = "*",
        extra: Sequence[str] = (),
        params: Sequence[Any] = (),
        tail: str = "",
    ) -> list[sqlite3.Row]:
        """SELECT with the window predicate ANDed onto caller-supplied filters."""
        pred, win_params = self.window.predicate(table)
        parts = [p for p in [pred, *extra] if p]
        sql = f"SELECT {cols} FROM {table}"
        if parts:
            sql += " WHERE " + " AND ".join(parts)
        if tail:
            sql += " " + tail
        return self.rows(sql, list(params) + win_params)

    def series(self, table: str, column: str, extra: Sequence[str] = (), params: Sequence[Any] = ()) -> list[float]:
        """Fetch one numeric column (dropping NULLs) for percentile math."""
        rows = self.select(table, column, extra=extra, params=params)
        return [float(r[0]) for r in rows if r[0] is not None]

    def group_counts(self, table: str, *columns: str, extra: Sequence[str] = (), params: Sequence[Any] = ()) -> dict[tuple, int]:
        """GROUP BY helper returning {(c1, c2): count}."""
        if not self.has_table(table):
            return {}
        col_list = ", ".join(columns)
        rows = self.select(
            table,
            f"{col_list}, COUNT(*)",
            extra=extra,
            params=params,
            tail=f"GROUP BY {col_list}",
        )
        out: dict[tuple, int] = {}
        for row in rows:
            out[tuple(row[:-1])] = int(row[-1])
        return out

    def close(self) -> None:
        self.conn.close()


def open_snapshot(path: str | os.PathLike[str] | None = None, window: Window | None = None) -> Snapshot:
    """Open the telemetry DB read-only.

    If SQLite cannot open the live file read-only (a WAL database whose
    sidecar files are not reachable by this process), we fall back to copying
    the database into a temp dir. The original file is never modified either
    way.
    """
    db_path = Path(path or DEFAULT_DB).expanduser()
    if not db_path.exists():
        raise FileNotFoundError(f"telemetry database not found: {db_path}")
    try:
        conn = _connect_ro(db_path)
    except sqlite3.OperationalError:
        conn = _connect_copy(db_path)
    return Snapshot(conn=conn, path=str(db_path), window=window or Window())


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # Belt and braces: even if the URI flag were ignored, refuse writes.
    conn.execute("PRAGMA query_only = 1")
    conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    return conn


def _connect_copy(db_path: Path) -> sqlite3.Connection:
    tmpdir = Path(tempfile.mkdtemp(prefix="grodex-eval-"))
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db_path) + suffix)
        if src.exists():
            shutil.copy2(src, tmpdir / (db_path.name + suffix))
    conn = sqlite3.connect(str(tmpdir / db_path.name), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    return conn
