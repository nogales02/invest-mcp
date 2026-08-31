"""MCP tool surface for InVEST (v0.1 -- the "low-hanging fruit" verbs).

    inventory      list_invest_models, describe_invest_model
    validation     validate_invest_args
    execution      run_invest_model, get_invest_job, get_invest_job_logs,
                   list_invest_jobs, cancel_invest_job
    results        list_invest_job_artifacts, summarize_results,
                   compare_scenarios
    data prep      scaffold_project, project_readiness, fetch_dem,
                   fetch_landcover, reproject_layer, clip_to_aoi,
                   align_raster_stack, delineate_watersheds,
                   tables_from_template
    admin          invest_env, allow_input_dir

Plus MCP resources (invest://models, invest://model/{id}/cheatsheet,
invest://conventions, invest://data-sources) and prompts (prepare_and_run_model,
compare_land_use_scenarios) registered from invest_mcp.resources / .prompts.

Everything returns plain JSON-able dicts so the client gets structured output.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from invest_mcp import invest_cli
from invest_mcp.calibration import client as cal_client
from invest_mcp.config import get_settings
from invest_mcp.execution.jobs import TERMINAL, JobStore
from invest_mcp.execution.runner import JobRunner, _tail
from invest_mcp.geo import client as geo_client
from invest_mcp.models import registry
from invest_mcp.models import spec_translate
from invest_mcp.workspace import artifacts
from invest_mcp.workspace import biotable
from invest_mcp.workspace import project as project_layout
from invest_mcp.workspace import readiness as readiness_mod
from invest_mcp.workspace.sandbox import (
    SandboxError,
    allow_dir,
    resolve_input_path,
    resolve_output_path,
)

_SETTINGS = get_settings()
_STORE = JobStore(_SETTINGS)
_RUNNER = JobRunner(_SETTINGS, _STORE)
_CAL_RUNNER = cal_client.CalibrationRunner(_SETTINGS, _STORE)


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
    try:
        cal_python: str | None = str(_SETTINGS.resolved_cal_python)
    except RuntimeError:
        cal_python = None
    return {
        "ok": True,
        "invest_exe": exe,
        "invest_version": ver,
        "geo_python": geo_python,
        "geo_preflight_available": geo_python is not None,
        "cal_python": cal_python,
        "calibration_available": cal_python is not None,
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


def _read_invest_summary_csv(workspace: str) -> list[dict] | None:
    """InVEST writes a `raster_values_summary.csv` (label/total/units/filename)
    for several models -- surface it verbatim alongside our own stats."""
    p = Path(workspace) / "raster_values_summary.csv"
    if not p.is_file():
        return None
    import csv

    try:
        with p.open(encoding="utf-8-sig", newline="") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    except OSError:
        return None


def _fmt(n: Any, digits: int = 3) -> str:
    if n is None:
        return "n/a"
    try:
        return f"{float(n):,.{digits}g}" if abs(float(n)) < 1e-3 else f"{float(n):,.{digits}f}"
    except (TypeError, ValueError):
        return str(n)


def _summary_narrative(model_title: str, job_id: str, summary: dict,
                       invest_csv: list[dict] | None) -> str:
    lines = [f"**{model_title}** — job `{job_id}`"]
    rasters = summary.get("rasters", [])
    lines.append(f"\n{len(rasters)} output raster(s) summarised.")
    for r in rasters:
        if r.get("error"):
            lines.append(f"- `{r['name']}` — could not read: {r['error']}")
            continue
        s = r.get("stats", {})
        unit = f" {r['units']}" if r.get("units") else ""
        label = r.get("label") or r["name"]
        approx = " (approx, decimated)" if s.get("approx") else ""
        lines.append(
            f"- `{r['name']}` — {label}: "
            f"{s.get('valid_count', 0):,} valid / {s.get('pixel_count', 0):,} px{approx}; "
            f"mean {_fmt(s.get('mean'))}{unit}, range {_fmt(s.get('min'))}–{_fmt(s.get('max'))}{unit}, "
            f"sum {_fmt(s.get('sum'))}{unit}."
        )
    aoi = summary.get("aoi")
    if aoi and aoi.get("features"):
        lines.append(f"\nAOI zonal summary — `{Path(aoi['path']).name}`, "
                     f"{aoi['feature_count']} feature(s)"
                     + (" (truncated)" if aoi.get("truncated") else "") + ":")
        for f in aoi["features"][:15]:
            props = ", ".join(f"{k}={v}" for k, v in (f.get("properties") or {}).items())
            per = "; ".join(
                f"{rn} mean {_fmt(rs.get('mean'))} over {rs.get('valid_count', 0):,} px"
                for rn, rs in (f.get("rasters") or {}).items() if rs.get("valid_count")
            )
            lines.append(f"- feature {f['feature_index']}"
                         + (f" ({props})" if props else "") + f": {per or 'no overlap'}")
        for n in aoi.get("notes", []):
            lines.append(f"- note: {n}")
    elif aoi and aoi.get("error"):
        lines.append(f"\nAOI zonal summary failed: {aoi['error']}")
    if invest_csv:
        lines.append("\nInVEST's own `raster_values_summary.csv`:")
        for row in invest_csv:
            label = row.get("Raster") or row.get("raster_label") or "?"
            lines.append(f"- {label}: {row.get('Total', '?')} {row.get('Units', '')}".rstrip())
    prev = summary.get("preview")
    if prev and prev.get("path"):
        lines.append(f"\nPreview PNG: `{prev['path']}`")
    elif prev and prev.get("error"):
        lines.append(f"\nPreview PNG not rendered ({prev['error']}).")
    return "\n".join(lines)


def summarize_results(job_id: str, aoi_path: str = "", rasters: list[str] | None = None,
                      include_intermediate: bool = False,
                      make_preview: bool = True) -> dict[str, Any]:
    """Summarise a finished run's output rasters: per-raster descriptive stats
    (valid/nodata pixel counts, min/max/mean/std/sum, a 10-bin histogram),
    optional per-feature zonal stats over an AOI vector, InVEST's own
    `raster_values_summary.csv` if present, a natural-language digest, and a
    best-effort PNG preview of the primary raster. Writes a `summary.json`
    sidecar next to the job workspace. Needs the `invest-geo` conda env.

    `aoi_path`: absolute path to a polygon vector (must be under an allowed
      folder); reprojected to each raster's CRS automatically.
    `rasters`: limit to these output files (relative paths or bare names);
      default is every top-level output raster.
    `include_intermediate`: also summarise `intermediate_outputs/`.
    """
    job = _STORE.get(job_id)
    if job is None:
        return {"ok": False, "error": f"No such job: {job_id}"}
    if job.status not in TERMINAL:
        return {"ok": False, "error": f"Job is still {job.status}; wait for it to finish."}
    if job.status != "succeeded":
        return {"ok": False, "error": f"Job did not succeed (status: {job.status}); "
                                      "nothing to summarise."}

    if aoi_path:
        try:
            resolve_input_path(str(aoi_path), _SETTINGS.allowed_roots())
        except SandboxError as exc:
            return {"ok": False, "error": f"aoi_path rejected: {exc}"}

    try:
        spec = registry.get_spec(registry.resolve_model_id(job.model_id))
    except Exception:  # noqa: BLE001 - calibration jobs etc. have no InVEST spec
        spec = {}
    model_title = spec.get("model_title") or job.model_id

    try:
        summary = geo_client.run_summary(
            job.workspace, spec, _SETTINGS,
            aoi_path=aoi_path or None,
            out_dir=str(Path(job.workspace).parent / "summary"),
            include_intermediate=bool(include_intermediate),
            rasters=rasters or None,
            make_preview=bool(make_preview),
        )
    except RuntimeError as exc:
        return {"ok": False, "env_missing": True, "error": str(exc)}

    if not summary.get("ok"):
        return {"ok": False, "job_id": job_id, **summary}

    invest_csv = _read_invest_summary_csv(job.workspace)
    return {
        "ok": True,
        "job_id": job_id,
        "model_id": job.model_id,
        "model_title": model_title,
        "workspace": job.workspace,
        "planned_rasters": summary.get("planned_rasters", []),
        "rasters": summary.get("rasters", []),
        "aoi": summary.get("aoi"),
        "preview": summary.get("preview"),
        "invest_raster_values_summary": invest_csv,
        "sidecar_json": summary.get("sidecar_json"),
        "narrative": _summary_narrative(model_title, job_id, summary, invest_csv),
    }


def _compare_narrative(model_title: str, base_id: str, scen_id: str,
                       cmp: dict) -> str:
    lines = [f"**{model_title}** — baseline `{base_id}` vs scenario `{scen_id}`"]
    pairs = cmp.get("pairs", [])
    lines.append(f"\n{len(pairs)} matching output raster(s) compared "
                 "(diff = scenario - baseline).")
    for r in pairs:
        name = Path(r["relpath"]).name
        if r.get("error"):
            lines.append(f"- `{name}` — could not compare: {r['error']}")
            continue
        s = r.get("stats", {})
        unit = f" {r['units']}" if r.get("units") else ""
        label = r.get("label") or name
        bsum = s.get("overlap_baseline_sum", s.get("baseline_sum"))
        ssum = s.get("overlap_scenario_sum", s.get("scenario_sum"))
        pct = s.get("pct_change")
        pct_txt = f" ({pct:+.1f}%)" if pct is not None else ""
        note = (" [scenario resampled to baseline grid]"
                if r.get("resampled_scenario_to_baseline_grid") else "")
        lines.append(
            f"- `{name}` — {label}: total {_fmt(bsum)} → {_fmt(ssum)}{unit}, "
            f"Δ {_fmt(s.get('delta_sum'))}{unit}{pct_txt}; "
            f"{s.get('increased_px', 0):,} px ↑ / {s.get('decreased_px', 0):,} px ↓ / "
            f"{s.get('unchanged_px', 0):,} px =; "
            f"Δ mean {_fmt(s.get('delta_mean'))}{unit}, "
            f"Δ range {_fmt(s.get('delta_min'))}–{_fmt(s.get('delta_max'))}{unit}.{note}"
        )
    aoi = cmp.get("aoi")
    if aoi and aoi.get("features"):
        lines.append(f"\nAOI Δ by feature — `{Path(aoi['path']).name}`, "
                     f"{aoi['feature_count']} feature(s)"
                     + (" (truncated)" if aoi.get("truncated") else "") + ":")
        for f in aoi["features"][:15]:
            props = ", ".join(f"{k}={v}" for k, v in (f.get("properties") or {}).items())
            per = "; ".join(
                f"{rn} Δ mean {_fmt(rs.get('mean'))} (Δ sum {_fmt(rs.get('sum'))}) "
                f"over {rs.get('valid_count', 0):,} px"
                for rn, rs in (f.get("rasters") or {}).items() if rs.get("valid_count")
            )
            lines.append(f"- feature {f['feature_index']}"
                         + (f" ({props})" if props else "") + f": {per or 'no overlap'}")
        for n in aoi.get("notes", []):
            lines.append(f"- note: {n}")
    elif aoi and aoi.get("error"):
        lines.append(f"\nAOI Δ summary failed: {aoi['error']}")
    if cmp.get("only_in_baseline"):
        lines.append(f"\nOnly in baseline: {', '.join(cmp['only_in_baseline'])}")
    if cmp.get("only_in_scenario"):
        lines.append(f"Only in scenario: {', '.join(cmp['only_in_scenario'])}")
    prev = cmp.get("preview")
    if prev and prev.get("path"):
        lines.append(f"\nPreview PNG: `{prev['path']}`")
    elif prev and prev.get("error"):
        lines.append(f"\nPreview PNG not rendered ({prev['error']}).")
    return "\n".join(lines)


def compare_scenarios(baseline_job_id: str, scenario_job_id: str,
                      aoi_path: str = "", rasters: list[str] | None = None,
                      include_intermediate: bool = False,
                      make_preview: bool = True) -> dict[str, Any]:
    """Compare a finished baseline run against a finished alternative-scenario
    run of the *same* InVEST model — the core InVEST use case (trade-offs).

    For every output raster present in both runs it writes a `scenario −
    baseline` difference raster and reports: the total before and after, the
    delta and its % change, how many pixels rose vs fell, optional per-feature
    deltas over an AOI vector, a natural-language digest, and a best-effort
    diverging-colormap PNG preview of the main diff. Writes a `compare.json`
    sidecar. Needs the `invest-geo` conda env.

    `baseline_job_id` / `scenario_job_id`: two succeeded `run_invest_model`
      jobs of the same model (e.g. the model run with two different LULC maps).
    `aoi_path`: absolute path to a polygon vector under an allowed folder;
      reprojected to each raster's CRS automatically.
    `rasters`: limit to these outputs (relative paths or bare names);
      default is every top-level output raster the two runs share.
    `include_intermediate`: also diff `intermediate_outputs/`.
    """
    base = _STORE.get(baseline_job_id)
    scen = _STORE.get(scenario_job_id)
    if base is None:
        return {"ok": False, "error": f"No such job: {baseline_job_id}"}
    if scen is None:
        return {"ok": False, "error": f"No such job: {scenario_job_id}"}
    if base.id == scen.id:
        return {"ok": False, "error": "baseline and scenario are the same job."}
    for tag, j in (("baseline", base), ("scenario", scen)):
        if j.status not in TERMINAL:
            return {"ok": False,
                    "error": f"{tag} job is still {j.status}; wait for it to finish."}
        if j.status != "succeeded":
            return {"ok": False,
                    "error": f"{tag} job did not succeed (status: {j.status})."}
    if base.model_id != scen.model_id:
        return {"ok": False,
                "error": f"jobs are different models ({base.model_id!r} vs "
                         f"{scen.model_id!r}); comparison needs the same model."}

    if aoi_path:
        try:
            resolve_input_path(str(aoi_path), _SETTINGS.allowed_roots())
        except SandboxError as exc:
            return {"ok": False, "error": f"aoi_path rejected: {exc}"}

    try:
        spec = registry.get_spec(registry.resolve_model_id(base.model_id))
    except Exception:  # noqa: BLE001 - calibration jobs etc. have no InVEST spec
        spec = {}
    model_title = spec.get("model_title") or base.model_id

    try:
        cmp = geo_client.run_comparison(
            base.workspace, scen.workspace, spec, _SETTINGS,
            aoi_path=aoi_path or None,
            out_dir=str(Path(scen.workspace).parent / f"compare_vs_{base.id}"),
            include_intermediate=bool(include_intermediate),
            rasters=rasters or None,
            make_preview=bool(make_preview),
        )
    except RuntimeError as exc:
        return {"ok": False, "env_missing": True, "error": str(exc)}

    if not cmp.get("ok"):
        return {"ok": False, "baseline_job_id": baseline_job_id,
                "scenario_job_id": scenario_job_id, **cmp}

    return {
        "ok": True,
        "baseline_job_id": baseline_job_id,
        "scenario_job_id": scenario_job_id,
        "model_id": base.model_id,
        "model_title": model_title,
        "compared_rasters": cmp.get("compared_rasters", []),
        "only_in_baseline": cmp.get("only_in_baseline", []),
        "only_in_scenario": cmp.get("only_in_scenario", []),
        "pairs": cmp.get("pairs", []),
        "aoi": cmp.get("aoi"),
        "preview": cmp.get("preview"),
        "sidecar_json": cmp.get("sidecar_json"),
        "narrative": _compare_narrative(model_title, base.id, scen.id, cmp),
    }


# ---------------------------------------------------------------------------
# data preparation  (deterministic geo routines -- run in the invest-geo env)
# ---------------------------------------------------------------------------
def scaffold_project(root: str, name: str = "", target_crs: str = "",
                     aoi_path: str = "", overwrite: bool = False) -> dict[str, Any]:
    """Create the standard InVEST *project* folder layout at `root` plus a
    `project.json` manifest, and trust `root` as an input/output folder for the
    rest of this session.

    Layout: `data/raw` (immutable downloads), `data/processed` (derived:
    reprojected / clipped / aligned), `tables`, `datastacks`, `jobs`, `logs`.

    `root`: absolute path to the project directory (created if missing).
    `name`: label for the manifest (defaults to the folder name).
    `target_crs`: the CRS the case study will be worked in (e.g. "EPSG:32618");
      recorded in the manifest, not enforced here.
    `aoi_path`: absolute path to the area-of-interest polygon; recorded in the
      manifest. Must already sit under an allowed folder.
    `overwrite`: rewrite an existing `project.json` (directories are always
      left in place).
    """
    root_p = Path(root).expanduser()
    if not root_p.is_absolute():
        return {"ok": False, "error": "root must be an absolute path"}
    try:
        root_p.mkdir(parents=True, exist_ok=True)
        root_p = root_p.resolve()
    except OSError as exc:
        return {"ok": False, "error": f"could not create {root!r}: {exc}"}

    allow_dir(root_p)  # trust it for reads + writes this session

    aoi_abs = ""
    if aoi_path:
        try:
            aoi_abs = str(resolve_input_path(str(aoi_path), _SETTINGS.allowed_roots()))
        except SandboxError as exc:
            return {"ok": False, "error": f"aoi_path rejected: {exc}"}

    try:
        info = project_layout.scaffold(
            root_p, name=name or root_p.name,
            target_crs=target_crs or None, aoi_path=aoi_abs or None,
            overwrite=bool(overwrite),
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "root": str(root_p),
        "allowed_input_roots": [str(p) for p in _SETTINGS.allowed_roots()],
        **info,
    }


def reproject_layer(src_path: str, dst_path: str, target_crs: str,
                    resampling: str = "nearest",
                    resolution: list[float] | None = None) -> dict[str, Any]:
    """Reproject one raster or vector to `target_crs`, writing `dst_path`.

    `target_crs`: EPSG code (e.g. "EPSG:32618"), WKT or proj string.
    `resampling`: raster only -- `nearest` (default; use for categorical data
      such as land cover), `bilinear` / `cubic` / `average` (continuous data),
      etc.
    `resolution`: raster only -- optional `[x, y]` target pixel size in the
      target CRS units; omit to keep the native resolution.
    Both paths must sit under an allowed folder (see `scaffold_project` /
    `allow_input_dir`). Needs the `invest-geo` conda env.
    """
    if not target_crs or not str(target_crs).strip():
        return {"ok": False, "error": "target_crs is required"}
    roots = _SETTINGS.allowed_roots()
    try:
        src = str(resolve_input_path(src_path, roots))
        dst = str(resolve_output_path(dst_path, roots))
    except SandboxError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        res = geo_client.run_reproject(src, dst, str(target_crs), _SETTINGS,
                                       resampling=resampling or "nearest",
                                       resolution=resolution)
    except RuntimeError as exc:
        return {"ok": False, "env_missing": True, "error": str(exc)}
    return {"ok": bool(res.get("ok")), "src": src, "dst": dst, **res}


def clip_to_aoi(src_path: str, dst_path: str, aoi_path: str,
                all_touched: bool = False) -> dict[str, Any]:
    """Clip one raster or vector to the AOI polygon in `aoi_path`, writing
    `dst_path`.

    The AOI is reprojected to the layer's CRS automatically. Rasters are cropped
    to the AOI bounding box and masked outside the polygon; `all_touched=True`
    keeps every pixel the polygon boundary touches. All three paths must sit
    under an allowed folder. Needs the `invest-geo` conda env.
    """
    roots = _SETTINGS.allowed_roots()
    try:
        src = str(resolve_input_path(src_path, roots))
        aoi = str(resolve_input_path(aoi_path, roots))
        dst = str(resolve_output_path(dst_path, roots))
    except SandboxError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        res = geo_client.run_clip(src, dst, aoi, _SETTINGS,
                                  all_touched=bool(all_touched))
    except RuntimeError as exc:
        return {"ok": False, "env_missing": True, "error": str(exc)}
    return {"ok": bool(res.get("ok")), "src": src, "dst": dst, "aoi": aoi, **res}


def align_raster_stack(rasters: list[dict], reference_path: str = "",
                       target_crs: str = "", resolution: list[float] | None = None,
                       extent: list[float] | None = None,
                       resampling: str = "nearest") -> dict[str, Any]:
    """Put several rasters on one identical grid -- same CRS, pixel size, extent
    and pixel alignment -- so InVEST can stack them.

    `rasters`: list of `{"src": <input path>, "dst": <output path>}`.
    The target grid comes **either** from `reference_path` (an existing raster
    whose grid is copied exactly) **or** from `target_crs` + `resolution`
    `[x, y]` + `extent` `[minx, miny, maxx, maxy]` given together.
    `resampling`: `nearest` (default) for categorical layers; `bilinear` /
      `cubic` / `average` for continuous ones. Applied to every raster -- run the
      tool twice if a stack mixes categorical and continuous layers.
    All paths must sit under an allowed folder. Needs the `invest-geo` conda env.
    """
    if not isinstance(rasters, list) or not rasters:
        return {"ok": False,
                "error": "rasters must be a non-empty list of {'src','dst'} objects"}
    roots = _SETTINGS.allowed_roots()
    norm: list[dict] = []
    try:
        for i, item in enumerate(rasters):
            if not isinstance(item, dict) or "src" not in item or "dst" not in item:
                return {"ok": False, "error": f"rasters[{i}] needs 'src' and 'dst'"}
            norm.append({
                "src": str(resolve_input_path(str(item["src"]), roots)),
                "dst": str(resolve_output_path(str(item["dst"]), roots)),
            })
        ref = str(resolve_input_path(reference_path, roots)) if reference_path else None
    except SandboxError as exc:
        return {"ok": False, "error": str(exc)}

    if not ref and not (target_crs and resolution and extent):
        return {"ok": False,
                "error": "provide reference_path, or all of target_crs + "
                         "resolution + extent"}
    try:
        res = geo_client.run_align_stack(
            norm, _SETTINGS, reference=ref,
            target_crs=(str(target_crs) or None), resolution=resolution,
            extent=extent, resampling=resampling or "nearest")
    except RuntimeError as exc:
        return {"ok": False, "env_missing": True, "error": str(exc)}
    return {"ok": bool(res.get("ok")), "reference": ref, **res}


def delineate_watersheds(dem_path: str, outlets_path: str, dst_path: str,
                         threshold_flow_accumulation: float = 1000,
                         snap_distance_px: int = 10, fill_pits: bool = True,
                         keep_intermediate: bool = False) -> dict[str, Any]:
    """Cut watershed polygons upstream of a set of outlet points, using the D8
    routing chain from `pygeoprocessing` -- the same engine InVEST uses, so the
    basins line up with how SDR / NDR / SWY route internally.

    Pipeline: fill pits -> D8 flow direction -> flow accumulation -> stream
    network (`threshold_flow_accumulation` pixels) -> snap each outlet to the
    nearest stream pixel within `snap_distance_px` -> delineate.

    `dem_path`: a projected DEM (metres). `outlets_path`: a point vector; it is
    reprojected to the DEM's CRS automatically. `dst_path`: where to write the
    watersheds vector (`.gpkg` / `.shp` / `.geojson`). Intermediates go in
    `<dst_stem>_hydro/` next to it and are deleted unless `keep_intermediate`.
    `snap_distance_px=0` disables snapping. All paths must sit under an allowed
    folder. Needs the `invest-geo` conda env.
    """
    roots = _SETTINGS.allowed_roots()
    try:
        dem = str(resolve_input_path(dem_path, roots))
        outlets = str(resolve_input_path(outlets_path, roots))
        dst = str(resolve_output_path(dst_path, roots))
    except SandboxError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        thr = float(threshold_flow_accumulation)
        snap = int(snap_distance_px)
    except (TypeError, ValueError):
        return {"ok": False, "error": "threshold_flow_accumulation and "
                                      "snap_distance_px must be numbers"}
    if thr <= 0:
        return {"ok": False, "error": "threshold_flow_accumulation must be > 0"}
    if snap < 0:
        return {"ok": False, "error": "snap_distance_px must be >= 0"}

    try:
        res = geo_client.run_delineate_watersheds(
            dem, outlets, dst, _SETTINGS,
            threshold_flow_accumulation=thr, snap_distance_px=snap,
            fill_pits=bool(fill_pits), keep_intermediate=bool(keep_intermediate))
    except RuntimeError as exc:
        return {"ok": False, "env_missing": True, "error": str(exc)}
    return {"ok": bool(res.get("ok")), "dem": dem, "outlets": outlets, "dst": dst, **res}


def fetch_dem(dst_path: str, aoi_path: str = "", bbox: list[float] | None = None,
              target_crs: str = "", target_resolution: list[float] | None = None,
              clip_to_aoi: bool = True, buffer_deg: float = 0.05,
              resampling: str = "bilinear", source: str = "cop30",
              keep_intermediate: bool = False) -> dict[str, Any]:
    """Download a DEM for an area of interest and land it as a GeoTIFF.

    Source (`source="cop30"`, the only one wired up): Copernicus DEM GLO-30
    (~30 m, near-global) from the public AWS bucket `copernicus-dem-30m` -- no
    credentials. This contacts `copernicus-dem-30m.s3.amazonaws.com` only.

    Give the area as `aoi_path` (a vector; its bounds drive the download and,
    with `clip_to_aoi`, the raster is masked to the polygon) and/or `bbox` as
    `[minx, miny, maxx, maxy]` in **lon/lat degrees (EPSG:4326)**. `buffer_deg`
    pads the bounds first. `target_crs` / `target_resolution` reproject the
    result (e.g. onto the project CRS, `resampling` default `bilinear`). Ocean /
    out-of-coverage tiles are skipped and listed in `tiles_missing`. Writes
    `dst_path` (must sit under an allowed folder). Needs the `invest-geo` env.
    """
    if not aoi_path and not bbox:
        return {"ok": False, "error": "provide aoi_path or bbox [minx,miny,maxx,maxy] (lon/lat)"}
    if bbox is not None and len(bbox) != 4:
        return {"ok": False, "error": "bbox must be [minx, miny, maxx, maxy] in lon/lat degrees"}
    roots = _SETTINGS.allowed_roots()
    try:
        dst = str(resolve_output_path(dst_path, roots))
        aoi = str(resolve_input_path(aoi_path, roots)) if aoi_path else None
    except SandboxError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        res = geo_client.run_fetch_dem(
            dst, _SETTINGS, source=source or "cop30",
            bbox_wgs84=[float(v) for v in bbox] if bbox else None,
            aoi_path=aoi, clip_to_aoi=bool(clip_to_aoi), buffer_deg=float(buffer_deg),
            target_crs=(str(target_crs) or None), target_resolution=target_resolution,
            resampling=resampling or "bilinear",
            keep_intermediate=bool(keep_intermediate))
    except RuntimeError as exc:
        return {"ok": False, "env_missing": True, "error": str(exc)}
    return {"ok": bool(res.get("ok")), "dst": dst, "aoi": aoi, **res}


def fetch_landcover(dst_path: str, aoi_path: str = "", bbox: list[float] | None = None,
                    year: int = 2021, target_crs: str = "",
                    target_resolution: list[float] | None = None,
                    clip_to_aoi: bool = True, buffer_deg: float = 0.05,
                    resampling: str = "nearest", source: str = "worldcover",
                    keep_intermediate: bool = False) -> dict[str, Any]:
    """Download a land-cover raster for an area of interest and land it as a
    GeoTIFF.

    Source (`source="worldcover"`, the only one wired up): **ESA WorldCover
    10 m** (`year=2020` or `2021`), 11 classes, from the public AWS bucket
    `esa-worldcover` -- no credentials. Contacts
    `esa-worldcover.s3.eu-central-1.amazonaws.com` only.

    Give the area as `aoi_path` (a vector; its bounds drive the download and,
    with `clip_to_aoi`, the raster is masked to the polygon) and/or `bbox` as
    `[minx, miny, maxx, maxy]` in **lon/lat degrees (EPSG:4326)**. `resampling`
    defaults to `nearest` -- land cover is categorical, keep it that way when
    reprojecting. The result carries a `class_legend` (value -> label) you can
    feed straight into `tables_from_template`. Writes `dst_path` (must be under
    an allowed folder). Needs the `invest-geo` env.
    """
    if not aoi_path and not bbox:
        return {"ok": False, "error": "provide aoi_path or bbox [minx,miny,maxx,maxy] (lon/lat)"}
    if bbox is not None and len(bbox) != 4:
        return {"ok": False, "error": "bbox must be [minx, miny, maxx, maxy] in lon/lat degrees"}
    if int(year) not in (2020, 2021):
        return {"ok": False, "error": "year must be 2020 or 2021 (ESA WorldCover)"}
    roots = _SETTINGS.allowed_roots()
    try:
        dst = str(resolve_output_path(dst_path, roots))
        aoi = str(resolve_input_path(aoi_path, roots)) if aoi_path else None
    except SandboxError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        res = geo_client.run_fetch_landcover(
            dst, _SETTINGS, source=source or "worldcover", year=int(year),
            bbox_wgs84=[float(v) for v in bbox] if bbox else None,
            aoi_path=aoi, clip_to_aoi=bool(clip_to_aoi), buffer_deg=float(buffer_deg),
            target_crs=(str(target_crs) or None), target_resolution=target_resolution,
            resampling=resampling or "nearest",
            keep_intermediate=bool(keep_intermediate))
    except RuntimeError as exc:
        return {"ok": False, "env_missing": True, "error": str(exc)}
    return {"ok": bool(res.get("ok")), "dst": dst, "aoi": aoi, **res}


def tables_from_template(model_id: str, lulc_path: str, dst_path: str,
                         table_arg: str = "", legend_path: str = "",
                         include_optional: bool = True,
                         max_classes: int = 1000) -> dict[str, Any]:
    """Write a skeleton biophysical / lookup table CSV for `model_id`: one row
    per unique land-cover code in `lulc_path`, with the header columns that
    model's table needs (straight from its MODEL_SPEC). Coefficient cells are
    left blank for you to fill.

    `table_arg`: which CSV input to template (e.g. `biophysical_table_path`).
      Default: auto-detect the one keyed by `lucode`; if the model has several,
      the error lists them so you can pick.
    `legend_path`: optional CSV whose first two columns are `code,label`; adds a
      `description` column pre-filled from it (InVEST ignores extra columns).
    `include_optional`: also emit columns that are only optionally required.
      Conditionally-required columns (e.g. NDR's `load_n` when `calc_n`) are
      always emitted and flagged in `column_help`.
    `[MONTH]` / `[SOIL_GROUP]` placeholder columns are expanded (`kc_1..kc_12`,
    `cn_a..cn_d`); other `[TOKEN]`s are left literal with a note.
    Needs the `invest-geo` env to read the raster's classes.
    """
    try:
        canonical = registry.resolve_model_id(model_id)
        spec = registry.get_spec(canonical)
    except registry.UnknownModelError as exc:
        return {"ok": False, "error": str(exc)}

    table_specs = spec_translate.table_arg_specs(spec)
    if not table_specs:
        return {"ok": False, "error": f"{canonical} takes no CSV table inputs"}

    if table_arg:
        if table_arg not in table_specs:
            return {"ok": False, "error": f"{canonical} has no CSV arg {table_arg!r}; "
                                          f"options: {sorted(table_specs)}"}
        chosen = table_arg
    else:
        lucode_keyed = [a for a, v in table_specs.items() if v["index_col"] == "lucode"]
        if len(lucode_keyed) == 1:
            chosen = lucode_keyed[0]
        elif not lucode_keyed:
            return {"ok": False,
                    "error": f"{canonical} has no land-cover-keyed table; its CSV "
                             f"inputs are keyed by "
                             f"{ {a: v['index_col'] for a, v in table_specs.items()} }. "
                             "Pass table_arg explicitly."}
        else:
            return {"ok": False,
                    "error": f"{canonical} has several land-cover-keyed tables "
                             f"({lucode_keyed}); pass table_arg to choose one."}

    key_col = table_specs[chosen]["index_col"] or "lucode"
    columns = table_specs[chosen]["columns"]

    roots = _SETTINGS.allowed_roots()
    try:
        lulc = str(resolve_input_path(lulc_path, roots))
        dst = str(resolve_output_path(dst_path, roots))
        legend = str(resolve_input_path(legend_path, roots)) if legend_path else ""
    except SandboxError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        res = geo_client.run_raster_classes(lulc, _SETTINGS, max_classes=int(max_classes))
    except RuntimeError as exc:
        return {"ok": False, "env_missing": True, "error": str(exc)}
    if not res.get("ok"):
        return {"ok": False, "model_id": canonical, **res}

    info = (res.get("outputs") or [{}])[0]
    classes = info.get("classes", [])
    lucodes = [c["value"] for c in classes]
    if not lucodes:
        return {"ok": False, "error": f"no class values found in {lulc}"}

    descriptions = None
    if legend:
        try:
            descriptions = biotable.parse_legend(Path(legend).read_text(encoding="utf-8-sig"))
        except OSError as exc:
            return {"ok": False, "error": f"could not read legend_path: {exc}"}

    tpl = biotable.build_template(key_col, columns, lucodes,
                                  descriptions=descriptions,
                                  include_optional=bool(include_optional))
    try:
        Path(dst).write_text(tpl["csv"], encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"could not write {dst}: {exc}"}

    n_req = sum(1 for h in tpl["column_help"].values() if h["requirement"] == "required")
    n_cond = sum(1 for h in tpl["column_help"].values()
                 if h["requirement"].startswith("required if"))
    narrative = (
        f"Wrote `{Path(dst).name}` for **{spec.get('model_title') or canonical}** "
        f"(`{chosen}`): {len(tpl['headers'])} columns "
        f"({n_req} required, {n_cond} conditional"
        + (", + optional" if include_optional else "")
        + f"), {len(lucodes)} rows — one per land-cover code "
        f"{', '.join(str(c) for c in lucodes[:12])}"
        + ("…" if len(lucodes) > 12 else "") + ". "
        + ("Class list truncated to the most common values. "
           if info.get("truncated") else "")
        + "Fill the blank coefficient cells (see `column_help`)."
    )
    return {
        "ok": True,
        "model_id": canonical,
        "table_arg": chosen,
        "dst": dst,
        "key_column": key_col,
        "headers": tpl["headers"],
        "row_count": len(lucodes),
        "classes": classes,
        "column_help": tpl["column_help"],
        "notes": tpl["notes"] + ([info["note"]] if info.get("note") else []),
        "narrative": narrative,
    }


def project_readiness(root: str, models: list[str] | None = None) -> dict[str, Any]:
    """Report which InVEST models a scaffolded project could attempt now and
    which required inputs are still missing.

    Scans `data/` and `tables/` under `root`, guesses each file's role from its
    name (`dem`, `lulc`, `watersheds`, `biophysical_table`, ...), and matches
    those against every model's *required* file inputs. This **supports** the
    choice of what to run -- it does not choose, and every guess is surfaced
    (`matched` / `ambiguous` / `missing`) for you to confirm. Numeric / option
    inputs are reported under `needs_values`, not treated as blockers.

    `models`: limit to these model ids/aliases; default is a shortlist of common
      models that are installed. Needs no `invest-geo` env.
    """
    root_p = Path(root).expanduser()
    if not root_p.is_absolute():
        return {"ok": False, "error": "root must be an absolute path"}
    root_p = root_p.resolve()

    manifest = project_layout.load(root_p)
    if manifest is None:
        return {"ok": False,
                "error": f"no project.json under {root_p}; run scaffold_project first"}

    if models:
        try:
            wanted = [registry.resolve_model_id(m) for m in models]
        except registry.UnknownModelError as exc:
            return {"ok": False, "error": str(exc)}
    else:
        installed = {m.model_id for m in registry.list_models()}
        wanted = [m for m in readiness_mod.DEFAULT_MODELS if m in installed]

    inventory = readiness_mod.scan_project(root_p)
    assessments: list[dict] = []
    for mid in wanted:
        try:
            spec = registry.get_spec(mid)
        except Exception as exc:  # noqa: BLE001
            assessments.append({"model_id": mid, "error": str(exc)})
            continue
        assessments.append(readiness_mod.assess_model(mid, spec, inventory))

    return {
        "ok": True,
        "root": str(root_p),
        "target_crs": manifest.get("target_crs"),
        "inventory": inventory,
        "assessments": assessments,
        "ready_to_attempt": [a["model_id"] for a in assessments if a.get("can_attempt")],
        "gaps_by_model": {
            a["model_id"]: [m["arg"] for m in a.get("missing", [])]
                           + [x["arg"] for x in a.get("ambiguous", [])]
            for a in assessments if not a.get("can_attempt") and not a.get("error")
        },
        "narrative": readiness_mod.narrative(inventory, assessments),
    }


# ---------------------------------------------------------------------------
# calibration  (AWY / SWY / SDR / NDR — engine shared with the Workbench plugin)
# ---------------------------------------------------------------------------
def _sandbox_calibration_inputs(observed_data_path: str, model_inputs: dict) -> list[dict]:
    roots = _SETTINGS.allowed_roots()
    issues: list[dict] = []
    for label, val in [("observed_data_path", observed_data_path),
                       *[(f"model_inputs.{k}", v) for k, v in (model_inputs or {}).items()
                         if isinstance(v, str) and (k.endswith("_path") or k.endswith("_table"))]]:
        if not val:
            continue
        try:
            resolve_input_path(str(val), roots)
        except SandboxError as exc:
            issues.append({"field": label, "problem": str(exc)})
    return issues


def validate_calibration_config(config: dict) -> dict[str, Any]:
    """Check a calibration config without running it: model/params/objective,
    observed-data columns, biophysical `Status_Cal_*` flags, factor caps, and
    that every input path is inside an allowed folder."""
    cfg = dict(config or {})
    # the server assigns the real workspace at submit time; fill a placeholder so
    # the validator doesn't flag it as missing.
    cfg.setdefault("workspace_dir", str(_SETTINGS.data_root / "_validate_cal"))
    sb = _sandbox_calibration_inputs(cfg.get("observed_data_path", ""),
                                     cfg.get("model_inputs", {}))
    try:
        res = _CAL_RUNNER.validate(cfg)
    except RuntimeError as exc:
        return {"ok": False, "env_missing": True, "error": str(exc), "sandbox_issues": sb}
    res.setdefault("issues", [])
    res["sandbox_issues"] = sb
    res["ok"] = res.get("ok", False) and not sb and not any(
        i.get("level") == "error" for i in res["issues"])
    return res


def run_calibration(model: str, parameters: dict, objective: str, optimizer: dict,
                    observed_data_path: str, model_inputs: dict,
                    results_suffix: str = "", make_plots: bool = True,
                    wait_seconds: int = 0) -> dict[str, Any]:
    """Start a calibration job for an InVEST hydrological model.

    `model`: AWY, SWY, SDR, NDR_N or NDR_P.
    `parameters`: {name: {"min": .., "max": .., "value": ..}} — keys per model:
      AWY   Z, Factor-Kc
      SWY   Alpha, Beta, Gamma, Factor-Kc_m
      SDR   sdr_max, Borselli-K_SDR, IC0, L_max, Factor-C, Factor-P
      NDR_N SubCri_Len_N, Sub_Eff_N, Borselli-K_NDR, Factor_Load_N, Factor_Eff_N
      NDR_P SubCri_Len_P, Sub_Eff_P, Borselli-K_NDR, Factor_Load_P, Factor_Eff_P
    `objective`: MSE | MAE | RMSE | RRMSE.
    `optimizer`: {"method": "DDS"|"LHS"|"SCE-UA", "n_simulations": >=10, "seed": ..}.
    `observed_data_path`: CSV with `ws_id` + a column named like the model.
    `model_inputs`: the InVEST inputs (absolute paths) + threshold_flow_accumulation.
    Returns a job_id; poll get_calibration_job(job_id)."""
    sb = _sandbox_calibration_inputs(observed_data_path, model_inputs)
    if sb:
        return {"ok": False, "error": "input path(s) rejected", "sandbox_issues": sb}

    config = {
        "model": str(model).upper(),
        "results_suffix": results_suffix or None,
        "optimizer": optimizer or {},
        "objective": objective,
        "parameters": parameters or {},
        "observed_data_path": observed_data_path,
        "model_inputs": model_inputs or {},
        "run_best": True,
        "make_plots": bool(make_plots),
    }
    try:
        job = _CAL_RUNNER.submit(config)
    except RuntimeError as exc:
        return {"ok": False, "env_missing": True, "error": str(exc)}

    if wait_seconds and wait_seconds > 0:
        job = _CAL_RUNNER.wait(job.id, min(wait_seconds, 3600)) or job

    out = job.public_dict()
    out["ok"] = True
    out["hint"] = "poll get_calibration_job(job_id) until status is terminal"
    return out


def get_calibration_job(job_id: str) -> dict[str, Any]:
    """Status of a calibration job. While running: last iterations. When done:
    best parameters, objective value, observed-vs-simulated table, parameter
    diagnostics and artifact paths."""
    job = _STORE.get(job_id)
    if job is None:
        return {"ok": False, "error": f"No such job: {job_id}"}
    out = job.public_dict()
    out["ok"] = True
    out["progress"] = cal_client.read_progress(job, tail=25)
    if job.status in TERMINAL:
        res = cal_client.read_result(job)
        if res:
            out["result"] = {k: res.get(k) for k in (
                "ok", "model", "objective_metric", "best_parameters", "best_objective",
                "obs_vs_sim", "diagnostics", "n_iterations", "warnings", "artifacts")}
            # dotty-plot data (per-param sampled values + best point) for the LLM
            # or user to plot; the JPG is best-effort (see shared-core plots).
            arts = res.get("artifacts") or {}
            out["result"]["dotty_data"] = [
                f for f in (arts.get("figures") or []) if str(f).endswith(".json")]
    if job.status == "failed":
        out["error_summary"] = job.error_summary
    return out


def cancel_calibration_job(job_id: str) -> dict[str, Any]:
    """Terminate a running calibration job."""
    job = _CAL_RUNNER.cancel(job_id)
    if job is None:
        return {"ok": False, "error": f"No such job: {job_id}"}
    out = job.public_dict()
    out["ok"] = True
    return out


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
    summarize_results,
    compare_scenarios,
    scaffold_project,
    project_readiness,
    fetch_dem,
    fetch_landcover,
    reproject_layer,
    clip_to_aoi,
    align_raster_stack,
    delineate_watersheds,
    tables_from_template,
    validate_calibration_config,
    run_calibration,
    get_calibration_job,
    cancel_calibration_job,
]


def register(server) -> None:
    for fn in _TOOLS:
        server.tool(structured_output=True)(fn)
