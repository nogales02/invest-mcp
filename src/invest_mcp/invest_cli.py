"""Thin, well-behaved wrapper around the ``invest`` command line tool.

Only introspection / validation calls live here (they are fast and synchronous).
Model *runs* are launched by :mod:`invest_mcp.execution.runner`, which needs to
own the process for cancellation and log streaming.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from invest_mcp.config import get_settings


class InvestCliError(RuntimeError):
    """Raised when the invest CLI exits unexpectedly or emits unparseable output."""


def _env() -> dict[str, str]:
    """Force UTF-8 I/O; the bundled CLI crashes on cp1252 consoles."""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    settings = get_settings()
    if settings.locale:
        env.setdefault("LANG", settings.locale)
    return env


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    exe = str(get_settings().resolved_invest_exe)
    return subprocess.run(
        [exe, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_env(),
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# invest --version
# ---------------------------------------------------------------------------
def version() -> str:
    cp = _run(["--version"])
    return (cp.stdout or cp.stderr).strip()


# ---------------------------------------------------------------------------
# invest list
# ---------------------------------------------------------------------------
_LIST_RE = re.compile(r"^\s+(\S+)\s+(?:\(([^)]*)\)\s+)?(.+?)\s*$")


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    aliases: tuple[str, ...]
    title: str

    def as_dict(self) -> dict:
        return {"model_id": self.model_id, "aliases": list(self.aliases), "title": self.title}


def list_models() -> list[ModelInfo]:
    cp = _run(["list"])
    if cp.returncode != 0:
        raise InvestCliError(f"`invest list` failed: {cp.stderr.strip() or cp.stdout.strip()}")
    models: list[ModelInfo] = []
    for line in cp.stdout.splitlines():
        if not line.strip() or line.strip().endswith(":"):
            continue  # header line "Available models:"
        m = _LIST_RE.match(line)
        if not m:
            continue
        model_id, aliases_raw, title = m.groups()
        aliases = tuple(a.strip() for a in (aliases_raw or "").split(",") if a.strip())
        models.append(ModelInfo(model_id=model_id, aliases=aliases, title=title.strip()))
    if not models:
        raise InvestCliError(f"Could not parse `invest list` output:\n{cp.stdout}")
    return models


# ---------------------------------------------------------------------------
# invest getspec <model> --json
# ---------------------------------------------------------------------------
def getspec(model_id: str) -> dict:
    cp = _run(["getspec", model_id, "--json"])
    if cp.returncode != 0:
        raise InvestCliError(
            f"`invest getspec {model_id}` failed: {cp.stderr.strip() or cp.stdout.strip()}"
        )
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise InvestCliError(f"getspec returned invalid JSON: {exc}\n{cp.stdout[:500]}") from exc


# ---------------------------------------------------------------------------
# invest validate <datastack.json> --json
# ---------------------------------------------------------------------------
def validate_datastack(datastack_path: Path) -> list[dict]:
    """Return InVEST validation warnings as ``[{"args": [...], "message": str}, ...]``.

    An empty list means InVEST found no problems.
    """
    cp = _run(["validate", "--json", str(datastack_path)])
    text = cp.stdout.strip()
    # invest exits non-zero when it finds validation problems; that is not a
    # CLI failure, so only treat unparseable output as an error.
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise InvestCliError(
            f"`invest validate` produced no JSON (exit {cp.returncode}):\n"
            f"{text or cp.stderr.strip()}"
        )
    results = payload.get("validation_results", [])
    out: list[dict] = []
    for entry in results:
        try:
            keys, message = entry
        except (ValueError, TypeError):
            out.append({"args": [], "message": str(entry)})
            continue
        out.append({"args": list(keys), "message": message})
    return out
