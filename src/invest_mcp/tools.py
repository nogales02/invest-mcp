"""MCP tool surface for InVEST (v0.1 -- the "low-hanging fruit" verbs).

    inventory      list_invest_models, describe_invest_model
    validation     validate_invest_args
    execution      run_invest_model, get_invest_job, get_invest_job_logs,
                   list_invest_jobs, cancel_invest_job
    results        list_invest_job_artifacts
    admin          invest_env, allow_input_dir

Everything returns plain JSON-able dicts so the client gets structured output.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from invest_mcp import invest_cli
from invest_mcp.config import get_settings
from invest_mcp.execution.jobs import TERMINAL, JobStore
from invest_mcp.execution.runner import JobRunner, _tail
from invest_mcp.geo import client as geo_client
from invest_mcp.models import registry
from invest_mcp.models import spec_translate
from invest_mcp.workspace import artifacts
from invest_mcp.workspace.sandbox import SandboxError, allow_dir, resolve_input_path

_SETTINGS = get_settings()
_STORE = JobStore(_SETTINGS)
_RUNNER = JobRunner(_SETTINGS, _STORE)


# ---------------------------------------------------------------------------
# admin
# ---------------------------------------------------------------------------
def invest_env() -> dict[str, Any]:
    """Report the InVEST executable, version and server paths. Use this first to
    confirm the server can reach InVEST."""
    try:
        exe = str(_SETTINGS.resolved_invest_exe)
        ver = invest_cli.version()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    try:
        geo_python: str | None = str(_SETTINGS.resolved_geo_python)
    except RuntimeError:
        geo_python = None
    return {
        "ok": True,
        "invest_exe": exe,
        "invest_version": ver,
        "geo_python": geo_python,
        "geo_preflight_available": geo_python is not None,
        "data_root": str(_SETTINGS.data_root),
        "jobs_dir": str(_SETTINGS.jobs_dir),
        "allowed_input_roots": [str(p) for p in _SETTINGS.allowed_roots()],
        "max_concurrent_jobs": _SETTINGS.max_concurrent_jobs,
    }


def allow_input_dir(path: str) -> dict[str, Any]:
    """Trust an extra folder as a source of InVEST input files for the rest of
    this server session."""
    try:
        added = allow_dir(path)
    except SandboxError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "added": str(added),
            "allowed_input_roots": [str(p) for p in _SETTINGS.allowed_roots()]}


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------
def list_invest_models() -> dict[str, Any]:
    """List every InVEST model available in this install (id, aliases, title)."""
    return {"models": [m.as_dict() for m in registry.list_models()]}


def describe_invest_model(model_id: str) -> dict[str, Any]:
    """Full briefing for one model: purpose, a JSON Schema for its `args`, the
    input files it needs and the outputs it produces."""
    canonical = registry.resolve_model_id(model_id)
    spec = registry.get_spec(canonical)
    return {
        "model_id": canonical,
        "title": spec.get("model_title"),
        "userguide": spec.get("userguide"),
        "about": spec.get("about"),
        "validate_spatial_overlap": spec.get("validate_spatial_overlap"),
        "different_projections_ok": spec.get("different_projections_ok"),
        "briefing_markdown": spec_translate.human_briefing(spec),
        "args_schema": spec_translate.spec_to_args_schema(spec),
        "input_path_args": spec_translate.path_args(spec),
        "outputs": spec.get("outputs", {}),
    }


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def validate_invest_args(model_id: str, args: dict) -> dict[str, Any]:
    """Dry-run check before spending compute: schema-level required-field check,
    input-path sandbox check, and InVEST's own `invest validate`."""
    canonical = registry.resolve_model_id(model_id)
    spec = registry.get_spec(canonical)
    user_args = {k: v for k, v in (args or {}).items() if k != "workspace_dir"}

    schema = spec_translate.spec_to_args_schema(spec)
    missing = [
        r for r in schema.get("required", [])
        if user_args.get(r) in (None, "", [])
    ]

    sandbox_issues: list[dict] = []
    roots = _SETTINGS.allowed_roots()
    for arg, kind in spec_translate.path_args(spec).items():
        val = user_args.get(arg)
        if val in (None, "", []):
            continue
        try:
            resolve_input_path(str(val), roots)
        except SandboxError as exc:
            sandbox_issues.append({"arg": arg, "type": kind, "problem": str(exc)})

    # InVEST's structural validation (columns, projections, spatial overlap...).
    tmp_dir = _SETTINGS.data_root / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"validate-{uuid.uuid4().hex[:8]}.json"
    tmp.write_text(
        json.dumps({"model_id": canonical, "args": {**user_args, "workspace_dir": str(tmp_dir / "ws")}}),
        encoding="utf-8",
    )
    try:
        invest_warnings = invest_cli.validate_datastack(tmp)
    except Exception as exc:  # noqa: BLE001
        invest_warnings = [{"args": [], "message": f"(invest validate could not run: {exc})"}]
    finally:
        tmp.unlink(missing_ok=True)

    # Geospatial preflight (CRS / projection / overlap / pixel size). Best effort:
    # if the invest-geo env is missing we report it as skipped, not a failure.
    geo_skipped: str | None = None
    geo_result: dict = {"ok": True, "checks": []}
    if not sandbox_issues:  # pointless to open files we already know are bad
        try:
            geo_result = geo_client.run_preflight(spec, user_args, _SETTINGS)
        except RuntimeError as exc:
            geo_skipped = str(exc)

    geo_ok = bool(geo_result.get("ok", True))
    ok = not missing and not sandbox_issues and not invest_warnings and geo_ok
    return {
        "ok": ok,
        "model_id": canonical,
        "missing_required": missing,
        "sandbox_issues": sandbox_issues,
        "invest_warnings": invest_warnings,
        "geo_preflight": (
            {"skipped": geo_skipped} if geo_skipped
            else {"ok": geo_ok, "checks": geo_result.get("checks", []),
                  "note": geo_result.get("note")}
        ),
    }


def preflight_geo(model_id: str, args: dict) -> dict[str, Any]:
    """Deep geospatial check of a parameter set: every spatial input's CRS,
    whether projected CRSs are used where required, whether the layers overlap,
    and pixel-size sanity. Needs the `invest-geo` conda env."""
    canonical = registry.resolve_model_id(model_id)
    spec = registry.get_spec(canonical)
    user_args = {k: v for k, v in (args or {}).items() if k != "workspace_dir"}

    roots = _SETTINGS.allowed_roots()
    for arg in spec_translate.spatial_arg_specs(spec):
        val = user_args.get(arg)
        if val in (None, "", []):
            continue
        try:
            resolve_input_path(str(val), roots)
        except SandboxError as exc:
            return {"ok": False, "error": f"input path rejected: {exc}"}

    try:
        result = geo_client.run_preflight(spec, user_args, _SETTINGS)
    except RuntimeError as exc:
        return {"ok": False, "env_missing": True, "error": str(exc)}
    result["model_id"] = canonical
    return result


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------
def run_invest_model(model_id: str, args: dict, wait_seconds: int = 0) -> dict[str, Any]:
    """Start an InVEST model run. Returns a job_id immediately. Set
    `wait_seconds` to block briefly for fast models (e.g. carbon)."""
    try:
        job = _RUNNER.submit(model_id, dict(args or {}))
    except SandboxError as exc:
        return {"ok": False, "error": f"input path rejected: {exc}"}
    except registry.UnknownModelError as exc:
        return {"ok": False, "error": str(exc)}

    if wait_seconds and wait_seconds > 0:
        job = _RUNNER.wait(job.id, min(wait_seconds, 3600))

    out = job.public_dict()
    out["ok"] = True
    out["hint"] = "poll get_invest_job(job_id) until status is terminal"
    return out


def get_invest_job(job_id: str) -> dict[str, Any]:
    """Status of one run. When finished, includes an artifact summary and, on
    failure, the tail of the log."""
    job = _STORE.get(job_id)
    if job is None:
        return {"ok": False, "error": f"No such job: {job_id}"}
    out = job.public_dict()
    out["ok"] = True
    if job.status in TERMINAL:
        cat = artifacts.catalog(job.workspace)
        out["artifacts_summary"] = cat.get("summary", {})
        out["invest_logs"] = cat.get("invest_logs", [])
    if job.status == "failed":
        out["log_tail"] = _tail(job.log_path, 60)
    return out


def get_invest_job_logs(job_id: str, tail_lines: int = 200) -> dict[str, Any]:
    """Return the captured stdout/stderr of a run (last `tail_lines` lines)."""
    job = _STORE.get(job_id)
    if job is None:
        return {"ok": False, "error": f"No such job: {job_id}"}
    try:
        lines = Path(job.log_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    tail = lines[-tail_lines:] if tail_lines and tail_lines > 0 else lines
    return {"ok": True, "job_id": job_id, "log_path": job.log_path,
            "total_lines": len(lines), "lines": tail}


def list_invest_jobs(limit: int = 20) -> dict[str, Any]:
    """Most recent runs first."""
    return {"jobs": [j.public_dict() for j in _STORE.list(limit=limit)]}


def cancel_invest_job(job_id: str) -> dict[str, Any]:
    """Terminate a queued or running model run."""
    try:
        job = _RUNNER.cancel(job_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    out = job.public_dict()
    out["ok"] = True
    return out


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------
def list_invest_job_artifacts(job_id: str) -> dict[str, Any]:
    """Catalog every file a finished run produced (path, kind, size)."""
    job = _STORE.get(job_id)
    if job is None:
        return {"ok": False, "error": f"No such job: {job_id}"}
    cat = artifacts.catalog(job.workspace)
    cat["ok"] = True
    cat["job_id"] = job_id
    return cat


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
_TOOLS = [
    invest_env,
    allow_input_dir,
    list_invest_models,
    describe_invest_model,
    validate_invest_args,
    preflight_geo,
    run_invest_model,
    get_invest_job,
    get_invest_job_logs,
    list_invest_jobs,
    cancel_invest_job,
    list_invest_job_artifacts,
]


def register(server) -> None:
    for fn in _TOOLS:
        server.tool(structured_output=True)(fn)
