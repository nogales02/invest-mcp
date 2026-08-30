"""MCP tool surface for InVEST (v0.1 -- the "low-hanging fruit" verbs).

    inventory      list_invest_models, describe_invest_model
    validation     validate_invest_args
    execution      run_invest_model, get_invest_job, get_invest_job_logs,
                   list_invest_jobs, cancel_invest_job
    results        list_invest_job_artifacts, summarize_results,
                   compare_scenarios
    admin          invest_env, allow_input_dir

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
from invest_mcp.workspace.sandbox import SandboxError, allow_dir, resolve_input_path

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
    validate_calibration_config,
    run_calibration,
    get_calibration_job,
    cancel_calibration_job,
]


def register(server) -> None:
    for fn in _TOOLS:
        server.tool(structured_output=True)(fn)
