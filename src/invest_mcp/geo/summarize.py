"""Output-summary worker -- RUNS IN THE ``invest-geo`` CONDA ENV.

Invoked by :mod:`invest_mcp.geo.client` as::

    <invest-geo>/python.exe -m invest_mcp.geo.summarize   < payload.json  > result.json

Input payload (stdin, JSON)::

    {
      "rasters": [
        {"path": "C:/.../c_storage_bas.tif", "label": "Baseline carbon storage",
         "units": "t/ha", "about": "..."},
        ...
      ],
      "aoi_path": "C:/.../watershed.shp" | null,
      "out_dir": "C:/.../jobs/<id>/summary",
      "primary": "C:/.../c_storage_bas.tif" | null,
      "make_preview": true,
      "max_zonal_features": 200
    }

Output (stdout, JSON): per-raster descriptive stats, optional per-feature zonal
stats over the AOI, and a best-effort PNG preview of the primary raster. The same
dict is also written to ``<out_dir>/summary.json`` as a sidecar.

Only stdlib is imported at module load; GDAL-backed libs are imported lazily so a
``.venv`` (no GDAL) can still import this module without executing it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import traceback
from pathlib import Path

# reading a whole band above this many pixels -> decimate and flag the stats approx
_DECIMATE_ABOVE_PX = 25_000_000
_PREVIEW_TIMEOUT_S = 90


# ---------------------------------------------------------------------------
# per-raster descriptive statistics
# ---------------------------------------------------------------------------
def _raster_stats(path: str) -> dict:
    import numpy as np
    import rasterio

    with rasterio.open(path) as ds:
        a, e = ds.transform.a, ds.transform.e
        px_w, px_h = abs(a), abs(e)
        total_px = ds.width * ds.height
        crs = ds.crs
        approx = total_px > _DECIMATE_ABOVE_PX
        if approx:
            scale = int((total_px / _DECIMATE_ABOVE_PX) ** 0.5) + 1
            out_h = max(1, ds.height // scale)
            out_w = max(1, ds.width // scale)
            band = ds.read(1, out_shape=(out_h, out_w), masked=True)
            sample_frac = (out_h * out_w) / total_px
        else:
            band = ds.read(1, masked=True)
            sample_frac = 1.0
        nodata = ds.nodata

    arr = np.ma.masked_invalid(band.astype("float64"))
    valid = arr.compressed()
    projected = bool(crs and crs.is_projected)
    px_area_m2 = (px_w * px_h) if projected else None

    stats: dict = {
        "pixel_count": int(total_px),
        "valid_count": int(round(valid.size / sample_frac)) if approx else int(valid.size),
        "nodata_count": (int(total_px) - int(round(valid.size / sample_frac))
                         if approx else int(total_px - valid.size)),
        "nodata_value": None if nodata is None else float(nodata),
        "pixel_size": [px_w, px_h],
        "pixel_area_m2": px_area_m2,
        "crs_projected": projected,
        "approx": approx,
    }
    if valid.size:
        s = float(valid.sum())
        stats.update(
            min=float(valid.min()),
            max=float(valid.max()),
            mean=float(valid.mean()),
            std=float(valid.std()),
            sum=s / sample_frac if approx else s,
        )
        if valid.min() != valid.max():
            counts, edges = np.histogram(valid, bins=10)
            stats["histogram"] = {
                "bin_edges": [float(x) for x in edges],
                "counts": [int(x) for x in counts],
            }
    else:
        stats.update(min=None, max=None, mean=None, std=None, sum=None)
    return stats


# ---------------------------------------------------------------------------
# zonal statistics over an AOI
# ---------------------------------------------------------------------------
def _id_columns(gdf) -> list[str]:
    cols = [c for c in gdf.columns if c != gdf.geometry.name]
    return cols[:4]


def _zonal(rasters: list[dict], aoi_path: str, max_features: int) -> dict:
    import geopandas as gpd
    import numpy as np
    import rasterio
    from rasterio.mask import mask as rio_mask

    gdf = gpd.read_file(aoi_path)
    truncated = len(gdf) > max_features
    if truncated:
        gdf = gdf.iloc[:max_features]
    id_cols = _id_columns(gdf)

    features: list[dict] = []
    for pos, (_, row) in enumerate(gdf.iterrows()):
        features.append({
            "feature_index": pos,
            "properties": {c: _jsonable(row[c]) for c in id_cols},
            "rasters": {},
        })

    notes: list[str] = []
    for spec in rasters:
        rpath = spec["path"]
        rname = Path(rpath).name
        try:
            with rasterio.open(rpath) as ds:
                if ds.crs is None or gdf.crs is None:
                    notes.append(f"{rname}: raster or AOI has no CRS; skipped zonal.")
                    continue
                geoms = gdf.to_crs(ds.crs.to_wkt()).geometry
                for pos, geom in enumerate(geoms):
                    entry = _mask_stats(ds, geom, rio_mask, np)
                    features[pos]["rasters"][rname] = entry
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{rname}: zonal failed ({type(exc).__name__}: {exc}).")

    return {
        "path": aoi_path,
        "feature_count": len(gdf),
        "id_columns": id_cols,
        "truncated": truncated,
        "features": features,
        "notes": notes,
    }


def _mask_stats(ds, geom, rio_mask, np) -> dict:
    if geom is None or geom.is_empty:
        return {"valid_count": 0}
    try:
        clipped, _ = rio_mask(ds, [geom.__geo_interface__], crop=True, filled=False)
    except ValueError:
        return {"valid_count": 0}  # geometry outside the raster
    band = np.ma.masked_invalid(clipped[0].astype("float64"))
    valid = band.compressed()
    if not valid.size:
        return {"valid_count": 0}
    return {
        "valid_count": int(valid.size),
        "min": float(valid.min()),
        "max": float(valid.max()),
        "mean": float(valid.mean()),
        "std": float(valid.std()),
        "sum": float(valid.sum()),
    }


def _jsonable(v):
    try:
        import numpy as np
        if isinstance(v, np.generic):
            return v.item()
    except Exception:  # noqa: BLE001
        pass
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


# ---------------------------------------------------------------------------
# preview PNG (rendered in its own process -- see _preview_worker)
# ---------------------------------------------------------------------------
def _preview(primary: str, label: str, out_dir: Path) -> dict:
    out_png = out_dir / f"preview_{Path(primary).stem}.png"
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "invest_mcp.geo._preview_worker",
             primary, str(out_png), label or ""],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_PREVIEW_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"preview render exceeded {_PREVIEW_TIMEOUT_S}s"}
    if proc.returncode == 0 and out_png.is_file():
        return {"path": str(out_png), "raster": Path(primary).name}
    return {"error": (proc.stderr or "preview render failed").strip()[-500:]}


# ---------------------------------------------------------------------------
def summarize(payload: dict) -> dict:
    rasters_in = payload.get("rasters", [])
    out_dir = Path(payload["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    raster_results: list[dict] = []
    for spec in rasters_in:
        path = spec["path"]
        row = {
            "path": path,
            "name": Path(path).name,
            "label": spec.get("label"),
            "units": spec.get("units"),
            "about": spec.get("about"),
        }
        try:
            row["stats"] = _raster_stats(path)
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"{type(exc).__name__}: {exc}"
        raster_results.append(row)

    aoi_result = None
    aoi_path = payload.get("aoi_path")
    if aoi_path:
        ok_rasters = [r for r in rasters_in if any(
            rr["path"] == r["path"] and "stats" in rr for rr in raster_results)]
        try:
            aoi_result = _zonal(ok_rasters, aoi_path,
                                int(payload.get("max_zonal_features", 200)))
        except Exception as exc:  # noqa: BLE001
            aoi_result = {"path": aoi_path, "error": f"{type(exc).__name__}: {exc}"}

    preview = None
    primary = payload.get("primary")
    if payload.get("make_preview") and primary:
        label = next((r.get("label") or "" for r in rasters_in
                      if r["path"] == primary), "")
        preview = _preview(primary, label, out_dir)

    result = {
        "ok": True,
        "rasters": raster_results,
        "aoi": aoi_result,
        "preview": preview,
    }
    sidecar = out_dir / "summary.json"
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
        result = summarize(payload)
    except Exception:  # noqa: BLE001 - never crash silently
        result = {"ok": False, "error": "summarize worker crashed",
                  "traceback": traceback.format_exc()}
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
