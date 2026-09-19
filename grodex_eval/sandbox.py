"""Isolated runtime sandbox for driving Grodex without touching real state.

Running `grodex serve` with the default environment would write to the user's
live ``~/.grodex/`` — memory.db, sessions/, telemetry.db and logs. That is
unacceptable for an eval harness (it both pollutes real data and makes runs
non-reproducible), and we cannot change Grodex to add an isolation flag.

Grodex resolves all of those paths from ``dirs::home_dir()`` plus a few
``GRODEX_*`` overrides, so a subprocess env is enough:

    HOME               -> <sandbox>/home     (config.toml + sessions/)
    GRODEX_MEMORY_DB   -> <sandbox>/memory.db
    GRODEX_TELEMETRY_DB-> <sandbox>/telemetry.db
    GRODEX_LOG_DIR     -> <sandbox>/logs

The real ``config.toml`` is copied in because the model endpoint and API key
live there. It is copied at runtime into a gitignored directory, never
committed.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

REAL_GRODEX_HOME = Path.home() / ".grodex"

# Secrets we never want echoed into reports.
_SENSITIVE_KEYS = ("api_key", "token", "secret", "password")


@dataclass
class Sandbox:
    """A directory tree plus env overrides that fully isolate one eval run."""

    root: Path
    home: Path
    workspace: Path

    @property
    def grodex_home(self) -> Path:
        return self.home / ".grodex"

    @property
    def memory_db(self) -> Path:
        return self.root / "memory.db"

    @property
    def telemetry_db(self) -> Path:
        return self.root / "telemetry.db"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def sessions(self) -> Path:
        return self.grodex_home / "sessions"

    def env(self, extra: dict | None = None) -> dict:
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(self.home),
                "GRODEX_MEMORY_DB": str(self.memory_db),
                "GRODEX_TELEMETRY_DB": str(self.telemetry_db),
                "GRODEX_LOG_DIR": str(self.logs),
            }
        )
        if extra:
            env.update(extra)
        return env

    def teardown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def scrub_config_text(text: str) -> str:
    """Redact secret-looking values, for safe inclusion in reports/logs."""
    out = []
    for line in text.splitlines():
        stripped = line.strip().lower()
        if any(k in stripped for k in _SENSITIVE_KEYS) and "=" in line:
            key = line.split("=", 1)[0]
            out.append(f"{key}= <redacted>")
        else:
            out.append(line)
    return "\n".join(out)


def create_sandbox(
    root: str | Path,
    source_config: Path | None = None,
    copy_workspace_from: str | Path | None = None,
) -> Sandbox:
    """Materialise an isolated sandbox rooted at `root`."""
    root = Path(root).expanduser().resolve()
    home = root / "home"
    grodex_home = home / ".grodex"
    workspace = root / "workspace"
    for d in (grodex_home / "sessions", workspace, root / "logs"):
        d.mkdir(parents=True, exist_ok=True)

    src = Path(source_config) if source_config else REAL_GRODEX_HOME / "config.toml"
    dst = grodex_home / "config.toml"
    if src.exists():
        shutil.copy2(src, dst)
        dst.chmod(0o600)
    else:
        # Minimal offline-safe config so the server can at least boot.
        dst.write_text('provider = "deepseek"\nmodel = "deepseek-v4-flash"\n', encoding="utf-8")

    sb = Sandbox(root=root, home=home, workspace=workspace)
    if copy_workspace_from:
        _copy_into(Path(copy_workspace_from), workspace)
    return sb


def _copy_into(src: Path, dst: Path) -> None:
    """Copy `src` contents into `dst`, skipping VCS/build junk."""
    skip = {".git", "target", "node_modules", "__pycache__", ".venv", "venv"}
    for item in src.iterdir():
        if item.name in skip:
            continue
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*skip))
        else:
            shutil.copy2(item, target)
