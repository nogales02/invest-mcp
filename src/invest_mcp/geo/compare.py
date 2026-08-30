"""Scenario-comparison worker -- RUNS IN THE ``invest-geo`` CONDA ENV.

Invoked by :mod:`invest_mcp.geo.client` as::

    <invest-geo>/python.exe -m invest_mcp.geo.compare  < payload.json  > result.json

Given pairs of matching output rasters from a *baseline* run and an *alternative*
("scenario") run of the same InVEST model, it writes one difference raster
(``scenario - baseline``) per pair and reports how each output shifted: the
total before and after, the delta and its % change, how many pixels rose vs
fell, per-feature deltas over an AOI vector, and a best-effort diverging-colormap
preview of the main diff. This is the point of InVEST -- quantifying the
trade-off between two land-use / management scenarios.

Input payload (stdin, JSON)::

    {
      "pairs": [
        {"relpath": "c_storage_bas.tif", "label": "Baseline carbon storage",
         "units": "t/ha", "about": "...",
         "baseline_path": "C:/.../jobs/<base>/workspace/c_storage_bas.tif",
         "scenario_path": "C:/.../jobs/<scen>/workspace/c_storage_bas.tif"},
        ...
      ],
      "aoi_path": "C:/.../watershed.shp" | null,
      "out_dir": "C:/.../jobs/<scen>/compare_vs_<base>",
      "primary_relpath": "c_storage_bas.tif" | null,
      "make_preview": true,
      "max_zonal_features": 200
    }

Output (stdout, JSON): per-pair delta stats, optional per-feature zonal deltas
over the AOI, and the preview. The same dict is also written to
``<out_dir>/compare.json`` as a sidecar.

Only stdlib is imported at module load; GDAL-backed libs (and the sibling
``summarize`` helpers) are imported lazily so a ``.venv`` without GDAL can still
import this module without executing it.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

# GeoTIFF profile keys copied from the baseline that can break a fresh write
# (e.g. a tiled block larger than a small diff raster) -- dropped before writing.
_UNSAFE_PROFILE_KEYS = ("blockxsize", "blockysize", "tiled", "interleave",
                        "photometric")


# ---------------------------------------------------------------------------
# align the scenario raster onto the baseline grid, then difference
# ---------------------------------------------------------------------------
def _load_pair(base_path: str, scen_path: str):
    """Return ``(base_masked, scen_on_base_grid_masked, base_profile, resampled)``.

    The scenario band is resampled onto the baseline's grid only when the grids
    actually differ (CRS / size / transform); an exact match is read straight
    through so identical inputs difference to exactly zero.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    with rasterio.open(base_path) as base:
        base_band = base.read(1, masked=True).astype("float64")
        profile = base.profile.copy()
        b_transform, b_crs = base.transform, base.crs
        b_h, b_w = base.height, base.width

    with rasterio.open(scen_path) as scen:
        same_grid = (
            scen.width == b_w and scen.height == b_h
            and scen.transform.almost_equals(b_transform)
            and (scen.crs == b_crs or scen.crs is None or b_crs is None)
        )
        if same_grid:
            scen_band = scen.read(1, masked=True).astype("float64")
            resampled = False
        else:
            src = scen.read(1, masked=True).astype("float64").filled(np.nan)
            dst = np.full((b_h, b_w), np.nan, dtype="float64")
            reproject(
                source=src, destination=dst,
                src_transform=scen.transform, src_crs=scen.crs,
                dst_transform=b_transform, dst_crs=b_crs,
                src_nodata=np.nan, dst_nodata=np.nan,
                resampling=Resampling.bilinear,
            )
            scen_band = dst
            resampled = True

    return (np.ma.masked_invalid(base_band),
            np.ma.masked_invalid(scen_band), profile, resampled)


def _diff_array(base_m, scen_m):
    """``scenario - baseline`` as a float array, NaN wherever either side is
    missing; plus the boolean mask of pixels valid in both."""
    import numpy as np

    both = ~(np.ma.getmaskarray(base_m) | np.ma.getmaskarray(scen_m))
    diff = np.where(both, scen_m.filled(0.0) - base_m.filled(0.0), np.nan)
    return diff, both


def _delta_stats(diff, both, base_m, scen_m) -> dict:
    import numpy as np

    delta = diff[both]
    b_valid = base_m.compressed()
    s_valid = scen_m.compressed()
    out: dict = {
        "compared_px": int(both.sum()),
        "baseline_valid_px": int(b_valid.size),
        "scenario_valid_px": int(s_valid.size),
        "baseline_sum": float(b_valid.sum()) if b_valid.size else None,
        "scenario_sum": float(s_valid.sum()) if s_valid.size else None,
    }
    if not delta.size:
        return out

    # totals restricted to the pixels present in *both* runs -> the delta is
    # exactly their difference, with no spurious contribution from nodata gaps.
    overlap_base = float(base_m.filled(0.0)[both].sum())
    overlap_scen = float(scen_m.filled(0.0)[both].sum())
    d_sum = overlap_scen - overlap_base
    out.update(
        overlap_baseline_sum=overlap_base,
        overlap_scenario_sum=overlap_scen,
        delta_sum=d_sum,
        pct_change=(d_sum / overlap_base * 100.0) if overlap_base else None,
        delta_min=float(delta.min()),
        delta_max=float(delta.max()),
        delta_mean=float(delta.mean()),
        delta_std=float(delta.std()),
        increased_px=int((delta > 0).sum()),
        decreased_px=int((delta < 0).sum()),
        unchanged_px=int((delta == 0).sum()),
    )
    if delta.min() != delta.max():
        counts, edges = np.histogram(delta, bins=10)
        out["delta_histogram"] = {
            "bin_edges": [float(x) for x in edges],
            "counts": [int(x) for x in counts],
        }
    return out


def _write_diff(diff, profile: dict, out_path: Path) -> None:
    import numpy as np
    import rasterio

    profile = {k: v for k, v in profile.items() if k not in _UNSAFE_PROFILE_KEYS}
    profile.update(dtype="float32", count=1, nodata=float("nan"), compress="deflate")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(diff.astype("float32"), 1)


# ---------------------------------------------------------------------------
def compare(payload: dict) -> dict:
    # sibling helpers -- zonal stats over the diff rasters and the preview PNG
    from invest_mcp.geo.summarize import _preview, _zonal

    pairs = payload.get("pairs", [])
    out_dir = Path(payload["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    primary_rel = payload.get("primary_relpath")

    pair_results: list[dict] = []
    diff_specs: list[dict] = []  # {"path", "label"} of every diff we managed to write
    primary_diff: str | None = None

    for spec in pairs:
        rel = spec["relpath"]
        row = {
            "relpath": rel,
            "label": spec.get("label"),
            "units": spec.get("units"),
            "about": spec.get("about"),
            "baseline_path": spec["baseline_path"],
            "scenario_path": spec["scenario_path"],
        }
        try:
            base_m, scen_m, profile, resampled = _load_pair(
                spec["baseline_path"], spec["scenario_path"])
            diff, both = _diff_array(base_m, scen_m)
            row["resampled_scenario_to_baseline_grid"] = resampled
            row["stats"] = _delta_stats(diff, both, base_m, scen_m)
            diff_path = out_dir / f"diff_{Path(rel).stem}.tif"
            _write_diff(diff, profile, diff_path)
            row["diff_raster"] = str(diff_path)
            diff_specs.append({
                "path": str(diff_path),
                "label": f"\u0394 {spec.get('label') or Path(rel).stem}",
            })
            if rel == primary_rel:
                primary_diff = str(diff_path)
        except Exception as exc:  # noqa: BLE001 - never crash the whole compare
            row["error"] = f"{type(exc).__name__}: {exc}"
        pair_results.append(row)

    if primary_diff is None and diff_specs:
        primary_diff = diff_specs[0]["path"]

    aoi_result = None
    aoi_path = payload.get("aoi_path")
    if aoi_path and diff_specs:
        try:
            aoi_result = _zonal(diff_specs, aoi_path,
                                int(payload.get("max_zonal_features", 200)))
        except Exception as exc:  # noqa: BLE001
            aoi_result = {"path": aoi_path, "error": f"{type(exc).__name__}: {exc}"}

    preview = None
    if payload.get("make_preview") and primary_diff:
        label = next((s["label"] for s in diff_specs if s["path"] == primary_diff),
                     "\u0394")
        preview = _preview(primary_diff, label, out_dir, diverging=True)

    result = {
        "ok": True,
        "pairs": pair_results,
        "aoi": aoi_result,
        "preview": preview,
    }
    sidecar = out_dir / "compare.json"
    sidecar.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["sidecar_json"] = str(sidecar)
    return result


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        json.dump({"ok": False, "error": f"bad payload: {exc}"}, sys.stdout)
        return
    try:
        result = compare(payload)
    except Exception:  # noqa: BLE001 - never crash silently
        result = {"ok": False, "error": "compare worker crashed",
                  "traceback": traceback.format_exc()}
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
