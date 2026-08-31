"""Server-side entry to the geospatial preflight.

Runs in the MCP server process (plain Python, no GDAL). Builds the payload from a
model spec + args and shells out to the ``invest-geo`` conda env worker.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from invest_mcp.config import Settings, geo_subprocess_env
from invest_mcp.models.spec_translate import spatial_arg_specs

_TIMEOUT_S = 300
_SUMMARY_TIMEOUT_S = 900

_RASTER_SUFFIXES = {".tif", ".tiff", ".vrt"}
_SKIP_DIRS = {"taskgraph_cache", "_taskgraph_working_dir"}


def build_payload(model_spec: dict, args: dict) -> dict:
    specs = spatial_arg_specs(model_spec)
    spatial_inputs = []
    for name, meta in specs.items():
        value = args.get(name)
        if value in (None, "", []):
            continue
        spatial_inputs.append({"arg": name, "path": str(value), **meta})
    return {
        "spatial_inputs": spatial_inputs,
        "different_projections_ok": bool(model_spec.get("different_projections_ok")),
        "validate_spatial_overlap": bool(model_spec.get("validate_spatial_overlap")),
    }


def run_preflight(model_spec: dict, args: dict, settings: Settings) -> dict:
    """Return the worker's result dict. Raises RuntimeError only if the geo env
    itself is missing (so callers can treat that as 'skipped')."""
    payload = build_payload(model_spec, args)
    if not payload["spatial_inputs"]:
        return {"ok": True, "checks": [], "layers": {},
                "note": "model has no spatial file inputs set"}

    geo_python = settings.resolved_geo_python  # RuntimeError if absent
    try:
        proc = subprocess.run(
            [str(geo_python), "-m", "invest_mcp.geo.preflight"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=geo_subprocess_env(geo_python),
            timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "checks": [
            {"level": "error", "code": "preflight_timeout",
             "message": f"Preflight exceeded {_TIMEOUT_S}s.", "args": []}
        ], "layers": {}}

    if proc.returncode != 0 and not proc.stdout.strip():
        return {"ok": False, "checks": [
            {"level": "error", "code": "preflight_worker_error",
             "message": (proc.stderr or "worker exited non-zero").strip()[-2000:], "args": []}
        ], "layers": {}}

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "checks": [
            {"level": "error", "code": "preflight_bad_output",
             "message": f"worker returned non-JSON. stdout={proc.stdout[:800]!r} "
                        f"stderr={proc.stderr[:800]!r}", "args": []}
        ], "layers": {}}


# ---------------------------------------------------------------------------
# result summarisation (zonal stats + preview) -- pure planning here, GDAL work
# happens in invest_mcp.geo.summarize inside the invest-geo env.
# ---------------------------------------------------------------------------
def output_meta_map(model_spec: dict) -> dict[str, dict]:
    """Map ``relative output path`` -> ``{id, about, units}`` from a MODEL_SPEC."""
    out: dict[str, dict] = {}
    for oid, o in (model_spec.get("outputs") or {}).items():
        rel = str(o.get("path", oid)).replace("\\", "/").lstrip("./")
        out[rel] = {"id": oid, "about": (o.get("about") or "").strip(),
                    "units": o.get("units")}
    return out


def plan_rasters(
    workspace: str,
    meta_by_relpath: dict[str, dict],
    *,
    include_intermediate: bool = False,
    explicit: list[str] | None = None,
) -> list[dict]:
    """Decide which output rasters to summarise and attach their spec metadata.

    ``explicit`` (relative paths or bare filenames) overrides the auto scan. The
    return order follows MODEL_SPEC output order, then any extras alphabetically.
    """
    ws = Path(workspace)
    found: dict[str, Path] = {}
    for p in sorted(ws.rglob("*")):
        if p.is_dir() or p.suffix.lower() not in _RASTER_SUFFIXES:
            continue
        rel = str(p.relative_to(ws)).replace("\\", "/")
        parts = set(rel.split("/"))
        if parts & _SKIP_DIRS:
            continue
        found[rel] = p

    if explicit:
        wanted = {e.replace("\\", "/").lstrip("./") for e in explicit}
        keep = {
            rel: p for rel, p in found.items()
            if rel in wanted or Path(rel).name in wanted
        }
    elif include_intermediate:
        keep = found
    else:
        keep = {rel: p for rel, p in found.items()
                if "intermediate_outputs/" not in rel + "/"
                and not rel.startswith("intermediate")}
        keep = keep or found  # models that write straight into intermediate dirs

    spec_order = list(meta_by_relpath)
    ordered = sorted(
        keep,
        key=lambda rel: (spec_order.index(rel) if rel in spec_order else 10_000, rel),
    )
    rasters: list[dict] = []
    for rel in ordered:
        meta = meta_by_relpath.get(rel, {})
        rasters.append({
            "path": str(keep[rel]),
            "relpath": rel,
            "label": meta.get("id") or Path(rel).stem,
            "units": meta.get("units"),
            "about": meta.get("about"),
        })
    return rasters


def run_summary(
    workspace: str,
    model_spec: dict,
    settings: Settings,
    *,
    aoi_path: str | None = None,
    out_dir: str | None = None,
    include_intermediate: bool = False,
    rasters: list[str] | None = None,
    make_preview: bool = True,
    max_zonal_features: int = 200,
) -> dict:
    """Summarise a finished run's output rasters. Raises RuntimeError only if the
    invest-geo env itself is missing (callers treat that as 'unavailable')."""
    meta = output_meta_map(model_spec)
    planned = plan_rasters(workspace, meta, include_intermediate=include_intermediate,
                           explicit=rasters)
    if not planned:
        return {"ok": True, "rasters": [], "aoi": None, "preview": None,
                "note": "no output rasters found in the workspace"}

    primary = next((r["path"] for r in planned
                    if "intermediate" not in r["relpath"]), planned[0]["path"])
    payload = {
        "rasters": [{k: r[k] for k in ("path", "label", "units", "about")} for r in planned],
        "aoi_path": aoi_path,
        "out_dir": out_dir or str(Path(workspace).parent / "summary"),
        "primary": primary,
        "make_preview": bool(make_preview),
        "max_zonal_features": int(max_zonal_features),
    }

    geo_python = settings.resolved_geo_python  # RuntimeError if absent
    try:
        proc = subprocess.run(
            [str(geo_python), "-m", "invest_mcp.geo.summarize"],
            input=json.dumps(payload),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=geo_subprocess_env(geo_python), timeout=_SUMMARY_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"summary exceeded {_SUMMARY_TIMEOUT_S}s"}

    if proc.returncode != 0 and not proc.stdout.strip():
        return {"ok": False,
                "error": (proc.stderr or "summary worker exited non-zero").strip()[-2000:]}
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False,
                "error": f"worker returned non-JSON. stdout={proc.stdout[:800]!r} "
                         f"stderr={proc.stderr[:800]!r}"}
    result["planned_rasters"] = [r["relpath"] for r in planned]
    return result


# ---------------------------------------------------------------------------
# scenario comparison (baseline vs alternative) -- pure planning here; the
# raster differencing + delta stats run in invest_mcp.geo.compare inside the
# invest-geo env.
# ---------------------------------------------------------------------------
def plan_comparison(
    baseline_ws: str,
    scenario_ws: str,
    meta_by_relpath: dict[str, dict],
    *,
    include_intermediate: bool = False,
    explicit: list[str] | None = None,
) -> dict:
    """Pair up output rasters that exist in *both* run workspaces (matched by
    relative path), and list the ones present in only one."""
    base = {r["relpath"]: r for r in plan_rasters(
        baseline_ws, meta_by_relpath,
        include_intermediate=include_intermediate, explicit=explicit)}
    scen = {r["relpath"]: r for r in plan_rasters(
        scenario_ws, meta_by_relpath,
        include_intermediate=include_intermediate, explicit=explicit)}

    pairs: list[dict] = []
    for rel, brow in base.items():
        srow = scen.get(rel)
        if srow is None:
            continue
        meta = meta_by_relpath.get(rel, {})
        pairs.append({
            "relpath": rel,
            "label": meta.get("id") or brow.get("label"),
            "units": meta.get("units") or brow.get("units"),
            "about": meta.get("about") or brow.get("about"),
            "baseline_path": brow["path"],
            "scenario_path": srow["path"],
        })
    return {
        "pairs": pairs,
        "only_in_baseline": sorted(set(base) - set(scen)),
        "only_in_scenario": sorted(set(scen) - set(base)),
    }


def run_comparison(
    baseline_ws: str,
    scenario_ws: str,
    model_spec: dict,
    settings: Settings,
    *,
    aoi_path: str | None = None,
    out_dir: str | None = None,
    include_intermediate: bool = False,
    rasters: list[str] | None = None,
    make_preview: bool = True,
    max_zonal_features: int = 200,
) -> dict:
    """Difference a baseline run's outputs against an alternative-scenario run.
    Raises RuntimeError only if the invest-geo env itself is missing (callers
    treat that as 'unavailable')."""
    meta = output_meta_map(model_spec)
    plan = plan_comparison(baseline_ws, scenario_ws, meta,
                           include_intermediate=include_intermediate,
                           explicit=rasters)
    if not plan["pairs"]:
        return {"ok": True, "pairs": [], "aoi": None, "preview": None,
                "only_in_baseline": plan["only_in_baseline"],
                "only_in_scenario": plan["only_in_scenario"],
                "note": "no output rasters are present in both workspaces"}

    primary = next((p["relpath"] for p in plan["pairs"]
                    if "intermediate" not in p["relpath"]), plan["pairs"][0]["relpath"])
    payload = {
        "pairs": plan["pairs"],
        "aoi_path": aoi_path,
        "out_dir": out_dir or str(Path(scenario_ws).parent / "compare"),
        "primary_relpath": primary,
        "make_preview": bool(make_preview),
        "max_zonal_features": int(max_zonal_features),
    }

    geo_python = settings.resolved_geo_python  # RuntimeError if absent
    try:
        proc = subprocess.run(
            [str(geo_python), "-m", "invest_mcp.geo.compare"],
            input=json.dumps(payload),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=geo_subprocess_env(geo_python), timeout=_SUMMARY_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"comparison exceeded {_SUMMARY_TIMEOUT_S}s"}

    if proc.returncode != 0 and not proc.stdout.strip():
        return {"ok": False,
                "error": (proc.stderr or "compare worker exited non-zero").strip()[-2000:]}
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False,
                "error": f"worker returned non-JSON. stdout={proc.stdout[:800]!r} "
                         f"stderr={proc.stderr[:800]!r}"}
    result.setdefault("only_in_baseline", plan["only_in_baseline"])
    result.setdefault("only_in_scenario", plan["only_in_scenario"])
    result["compared_rasters"] = [p["relpath"] for p in plan["pairs"]]
    return result


# ---------------------------------------------------------------------------
# deterministic data prep (reproject / clip / align / watershed delineation) --
# pure dispatch here; the GDAL work runs in a worker module inside the
# invest-geo env.
# ---------------------------------------------------------------------------
_PREP_TIMEOUT_S = 1800
_HYDRO_TIMEOUT_S = 3600
_FETCH_TIMEOUT_S = 1800


def _run_geo_worker(module: str, payload: dict, settings: Settings, *,
                    timeout: int, label: str) -> dict:
    """Shell out to ``python -m <module>`` in the invest-geo env, JSON over
    stdin/stdout. Raises RuntimeError only if that env is missing (callers treat
    it as 'unavailable')."""
    geo_python = settings.resolved_geo_python  # RuntimeError if absent
    try:
        proc = subprocess.run(
            [str(geo_python), "-m", module],
            input=json.dumps(payload),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=geo_subprocess_env(geo_python), timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"{label} exceeded {timeout}s"}

    if proc.returncode != 0 and not proc.stdout.strip():
        return {"ok": False,
                "error": (proc.stderr or f"{label} worker exited non-zero").strip()[-2000:]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False,
                "error": f"worker returned non-JSON. stdout={proc.stdout[:800]!r} "
                         f"stderr={proc.stderr[:800]!r}"}


def _run_prep(payload: dict, settings: Settings) -> dict:
    return _run_geo_worker("invest_mcp.geo.prep", payload, settings,
                           timeout=_PREP_TIMEOUT_S, label="prep op")


def run_reproject(src: str, dst: str, target_crs: str, settings: Settings, *,
                  resampling: str = "nearest", resolution: list[float] | None = None,
                  kind: str = "auto") -> dict:
    return _run_prep({"op": "reproject", "src": src, "dst": dst,
                      "target_crs": target_crs, "resampling": resampling,
                      "resolution": resolution, "kind": kind}, settings)


def run_clip(src: str, dst: str, aoi: str, settings: Settings, *,
             kind: str = "auto", all_touched: bool = False) -> dict:
    return _run_prep({"op": "clip", "src": src, "dst": dst, "aoi": aoi,
                      "kind": kind, "all_touched": all_touched}, settings)


def run_align_stack(rasters: list[dict], settings: Settings, *,
                    reference: str | None = None, target_crs: str | None = None,
                    resolution: list[float] | None = None,
                    extent: list[float] | None = None,
                    resampling: str = "nearest") -> dict:
    return _run_prep({"op": "align_stack", "rasters": rasters,
                      "reference": reference, "target_crs": target_crs,
                      "resolution": resolution, "extent": extent,
                      "resampling": resampling}, settings)


def run_raster_classes(src: str, settings: Settings, *, max_classes: int = 1000) -> dict:
    return _run_prep({"op": "raster_classes", "src": src,
                      "max_classes": max_classes}, settings)


def run_fetch_dem(dst_path: str, settings: Settings, *, source: str = "cop30",
                  bbox_wgs84: list[float] | None = None, aoi_path: str | None = None,
                  clip_to_aoi: bool = True, buffer_deg: float = 0.05,
                  target_crs: str | None = None,
                  target_resolution: list[float] | None = None,
                  resampling: str = "bilinear",
                  keep_intermediate: bool = False) -> dict:
    return _run_geo_worker("invest_mcp.geo.fetch", {
        "op": "dem", "source": source, "dst_path": dst_path,
        "bbox_wgs84": bbox_wgs84, "aoi_path": aoi_path,
        "clip_to_aoi": bool(clip_to_aoi), "buffer_deg": buffer_deg,
        "target_crs": target_crs, "target_resolution": target_resolution,
        "resampling": resampling, "keep_intermediate": bool(keep_intermediate),
    }, settings, timeout=_FETCH_TIMEOUT_S, label="DEM fetch")


def run_fetch_landcover(dst_path: str, settings: Settings, *,
                        source: str = "worldcover", year: int = 2021,
                        bbox_wgs84: list[float] | None = None,
                        aoi_path: str | None = None, clip_to_aoi: bool = True,
                        buffer_deg: float = 0.05, target_crs: str | None = None,
                        target_resolution: list[float] | None = None,
                        resampling: str = "nearest",
                        keep_intermediate: bool = False) -> dict:
    return _run_geo_worker("invest_mcp.geo.fetch", {
        "op": "landcover", "source": source, "year": year, "dst_path": dst_path,
        "bbox_wgs84": bbox_wgs84, "aoi_path": aoi_path,
        "clip_to_aoi": bool(clip_to_aoi), "buffer_deg": buffer_deg,
        "target_crs": target_crs, "target_resolution": target_resolution,
        "resampling": resampling, "keep_intermediate": bool(keep_intermediate),
    }, settings, timeout=_FETCH_TIMEOUT_S, label="land-cover fetch")


def run_fetch_climate(dst_path: str, variable: str, settings: Settings, *,
                      source: str = "worldclim", period: str = "monthly",
                      months: list[int] | None = None, resolution: str = "10m",
                      bbox_wgs84: list[float] | None = None,
                      aoi_path: str | None = None, clip_to_aoi: bool = True,
                      buffer_deg: float = 0.05, target_crs: str | None = None,
                      target_resolution: list[float] | None = None,
                      resampling: str = "bilinear",
                      keep_intermediate: bool = False) -> dict:
    return _run_geo_worker("invest_mcp.geo.climate", {
        "variable": variable, "source": source, "period": period,
        "months": months, "resolution": resolution, "dst_path": dst_path,
        "bbox_wgs84": bbox_wgs84, "aoi_path": aoi_path,
        "clip_to_aoi": bool(clip_to_aoi), "buffer_deg": buffer_deg,
        "target_crs": target_crs, "target_resolution": target_resolution,
        "resampling": resampling, "keep_intermediate": bool(keep_intermediate),
    }, settings, timeout=_FETCH_TIMEOUT_S, label="climate fetch")


def run_fetch_soil(dst_path: str, variable: str, settings: Settings, *,
                   source: str = "soilgrids", depth: str = "0-5cm",
                   stat: str = "mean",
                   bbox_wgs84: list[float] | None = None,
                   aoi_path: str | None = None, clip_to_aoi: bool = True,
                   buffer_deg: float = 0.05, target_crs: str | None = None,
                   target_resolution: list[float] | None = None,
                   resampling: str | None = None,
                   keep_intermediate: bool = False) -> dict:
    return _run_geo_worker("invest_mcp.geo.soil", {
        "variable": variable, "source": source, "depth": depth, "stat": stat,
        "dst_path": dst_path, "bbox_wgs84": bbox_wgs84, "aoi_path": aoi_path,
        "clip_to_aoi": bool(clip_to_aoi), "buffer_deg": buffer_deg,
        "target_crs": target_crs, "target_resolution": target_resolution,
        "resampling": resampling, "keep_intermediate": bool(keep_intermediate),
    }, settings, timeout=_FETCH_TIMEOUT_S, label="soil fetch")


def run_delineate_watersheds(dem_path: str, outlets_path: str, dst_path: str,
                             settings: Settings, *,
                             threshold_flow_accumulation: float = 1000,
                             snap_distance_px: int = 10,
                             fill_pits: bool = True,
                             keep_intermediate: bool = False) -> dict:
    return _run_geo_worker("invest_mcp.geo.hydro", {
        "dem_path": dem_path,
        "outlets_path": outlets_path,
        "dst_path": dst_path,
        "threshold_flow_accumulation": threshold_flow_accumulation,
        "snap_distance_px": snap_distance_px,
        "fill_pits": fill_pits,
        "keep_intermediate": keep_intermediate,
    }, settings, timeout=_HYDRO_TIMEOUT_S, label="watershed delineation")
