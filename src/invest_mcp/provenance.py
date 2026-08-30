"""Per-run reproducibility manifest.

Written to ``<job>/provenance.json`` when a run finishes. Captures enough to
re-execute the run later and to detect if an input changed underneath us.
"""

from __future__ import annotations

import hashlib
import json
import platform
from datetime import datetime, timezone
from pathlib import Path

from invest_mcp import __version__
from invest_mcp.config import Settings

_HASH_MAX_BYTES = 2 * 1024 * 1024 * 1024  # skip hashing files larger than 2 GiB


def _sha256(path: Path) -> str | None:
    try:
        if path.stat().st_size > _HASH_MAX_BYTES:
            return "skipped:too-large"
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def write_provenance(job, settings: Settings, input_paths: dict[str, str]) -> None:
    try:
        invest_version = _read_invest_version(settings)
    except Exception:
        invest_version = "unknown"

    datastack = {}
    try:
        datastack = json.loads(Path(job.datastack_path).read_text(encoding="utf-8"))
    except Exception:
        pass

    manifest = {
        "job_id": job.id,
        "model_id": job.model_id,
        "status": job.status,
        "returncode": job.returncode,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "ended_at": job.ended_at,
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "invest_mcp_version": __version__,
        "invest_version": invest_version,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "datastack": datastack,
        "inputs": [
            {"arg": arg, "path": p, "sha256": _sha256(Path(p))}
            for arg, p in sorted(input_paths.items())
        ],
        "log_path": job.log_path,
        "workspace": job.workspace,
    }
    Path(job.provenance_path).write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _read_invest_version(settings: Settings) -> str:
    import subprocess

    cp = subprocess.run(
        [str(settings.resolved_invest_exe), "--version"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    return (cp.stdout or cp.stderr).strip()
