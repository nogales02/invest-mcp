"""Aggregate-to-units worker -- RUNS IN THE ``invest-geo`` CONDA ENV.

Invoked by :mod:`invest_mcp.geo.client` as::

    <invest-geo>/python.exe -m invest_mcp.geo.aggregate  < payload.json  > result.json

Roll an ecosystem-service raster up to a set of *reporting units* -- municipal
boundaries, cadastral parcels, intervention footprints -- so a result comes out
as a table of "how much service per polygon", and, where InVEST does not carry a
monetary figure, multiply by a flat per-unit value so the same table also has a
currency column. The service raster can be a plain InVEST output (carbon stock,
sediment export, nutrient export...) or a ``diff_*.tif`` written by
``compare_scenarios`` -- aggregating the *delta* is the usual case: "what does
this land-use change buy each municipality?".

Input payload (stdin, JSON)::

    {
      "rasters": [
        {"path": "C:/.../diff_c_storage_bas.tif", "label": "carbon_delta",
         "units": "t", "value_per_unit": 50.0}
      ],
      "units_path": "C:/.../municipios.shp",
      "dst_path": "C:/.../aggregated.gpkg",
      "id_columns": ["NOMBRE", "COD_MUN"] | null,
      "stats": ["sum", "mean", "count", "min", "max", "std", "median"],
      "area_weighted": false,
      "value_currency": "USD",
      "all_touched": false,
      "max_units": 5000
    }

For every unit x raster it computes the requested zonal stats over the pixels
whose centre falls inside the polygon (``all_touched`` switches to any-touch),
and ``val_<label> = value_per_unit * sum``. With ``area_weighted`` the sum is
first multiplied by the pixel area in hectares -- use that when the raster holds
a per-hectare density rather than a per-pixel total; it is refused (with a note)
for a geographic CRS where m2 is not well defined.

Output (stdout, JSON): per-unit rows, per-raster grand totals (incl. the valued
total), the written vector's description and the CSV path. The units vector is
written back out with one column per (raster, stat) plus the value column, a
tidy CSV is written alongside, and a ``<dst>_aggregate.json`` sidecar too.

Only stdlib is imported at module load; GDAL-backed libs (and the sibling
``summarize`` / ``prep`` helpers) are imported lazily so a ``.venv`` without
GDAL can still import this module without executing it.
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from pathlib import Path

_ALLOWED_STATS = ("sum", "mean", "count", "min", "max", "std", "median")


def _slug(raw: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "_", str(raw)).strip("_")
    return s or "raster"


def _default_id_columns(gdf) -> list[str]:
    cols = [c for c in gdf.columns if c != gdf.geometry.name]
    return cols[:4]


# ---------------------------------------------------------------------------
# per-unit zonal stats over one raster
# ---------------------------------------------------------------------------
def _zonal_one(ds, geom, stats, rio_mask, np, *, all_touched: bool,
               px_area_ha) -> dict:
    """Return ``{stat: value, ..., "_sum_raw": float, "_sum_aw": float|None}``.

    ``_sum_raw`` is the plain pixel sum (always present, even if "sum" was not
    requested); ``_sum_aw`` is it times the pixel area in hectares, or ``None``
    when the raster CRS is geographic.
    """
    empty = {s: (0 if s == "count" else None) for s in stats}
    empty["_sum_raw"] = 0.0
    empty["_sum_aw"] = 0.0 if px_area_ha else None
    if geom is None or geom.is_empty:
        return empty
    try:
        clipped, _ = rio_mask(ds, [geom.__geo_interface__], crop=True,
                              filled=False, all_touched=all_touched)
    except ValueError:
        return empty  # geometry falls entirely outside the raster
    band = np.ma.masked_invalid(clipped[0].astype("float64"))
    valid = band.compressed()
    n = int(valid.size)
    out: dict = {}
    if "count" in stats:
        out["count"] = n
    if not n:
        for s in stats:
            if s != "count":
                out[s] = None
        out["_sum_raw"] = 0.0
        out["_sum_aw"] = 0.0 if px_area_ha else None
        return out
    s_raw = float(valid.sum())
    for s in stats:
        if s == "sum":
            out["sum"] = s_raw
        elif s == "mean":
            out["mean"] = float(valid.mean())
        elif s == "min":
            out["min"] = float(valid.min())
        elif s == "max":
            out["max"] = float(valid.max())
        elif s == "std":
            out["std"] = float(valid.std())
        elif s == "median":
            out["median"] = float(np.median(valid))
    out["_sum_raw"] = s_raw
    out["_sum_aw"] = (s_raw * px_area_ha) if px_area_ha else None
    return out


# ---------------------------------------------------------------------------
def aggregate(payload: dict) -> dict:
    import geopandas as gpd
    import numpy as np
    import rasterio
    from rasterio.mask import mask as rio_mask

    from invest_mcp.geo.prep import _describe_vector
    from invest_mcp.geo.summarize import _jsonable

    rasters = payload.get("rasters") or []
    if not rasters:
        return {"ok": False, "error": "no rasters given"}

    stats = [s for s in (payload.get("stats") or ["sum", "mean", "count"])
             if s in _ALLOWED_STATS]
    stats = list(dict.fromkeys(stats)) or ["sum"]
    all_touched = bool(payload.get("all_touched"))
    area_weighted = bool(payload.get("area_weighted"))
    currency = payload.get("value_currency") or "USD"
    units_path = payload["units_path"]
    dst_path = Path(payload["dst_path"])
    max_units = int(payload.get("max_units") or 5000)

    gdf = gpd.read_file(units_path)
    if gdf.crs is None:
        return {"ok": False, "error": f"{units_path} has no CRS; cannot aggregate."}
    truncated = len(gdf) > max_units
    gdf = (gdf.iloc[:max_units] if truncated else gdf).copy().reset_index(drop=True)

    want_ids = payload.get("id_columns") or _default_id_columns(gdf)
    id_cols = [c for c in want_ids if c in gdf.columns and c != gdf.geometry.name]

    notes: list[str] = []
    if payload.get("id_columns"):
        missing = [c for c in payload["id_columns"] if c not in gdf.columns]
        if missing:
            notes.append(f"id_columns not found on the units layer: {', '.join(missing)}.")

    new_cols: dict[str, list] = {}
    raster_totals: list[dict] = []

    for i, spec in enumerate(rasters):
        rpath = spec["path"]
        rname = Path(rpath).name
        label = _slug(spec.get("label") or Path(rpath).stem)
        base, k = label, 2
        while label in {t["label"] for t in raster_totals}:
            label = f"{base}_{k}"
            k += 1
        vpu = spec.get("value_per_unit")

        try:
            with rasterio.open(rpath) as ds:
                if ds.crs is None:
                    notes.append(f"{rname}: raster has no CRS; skipped.")
                    continue
                projected = bool(ds.crs.is_projected)
                px_w, px_h = abs(ds.transform.a), abs(ds.transform.e)
                px_area_ha = (px_w * px_h / 10_000.0) if projected else None
                use_aw = bool(area_weighted and px_area_ha)
                if area_weighted and not px_area_ha:
                    notes.append(f"{rname}: area_weighted ignored -- raster CRS is "
                                 "geographic, no reliable pixel area in m2.")
                geoms = gdf.to_crs(ds.crs.to_wkt()).geometry
                per_unit = [
                    _zonal_one(ds, g, stats, rio_mask, np,
                               all_touched=all_touched, px_area_ha=px_area_ha)
                    for g in geoms
                ]
        except Exception as exc:  # noqa: BLE001 - never crash the whole run
            notes.append(f"{rname}: aggregation failed ({type(exc).__name__}: {exc}).")
            continue

        for s in stats:
            new_cols[f"{label}_{s}"] = [u.get(s) for u in per_unit]

        sums_used = [
            (u["_sum_aw"] if use_aw and u.get("_sum_aw") is not None else u["_sum_raw"])
            for u in per_unit
        ]
        grand_sum = float(sum(v for v in sums_used if v is not None))
        total: dict = {
            "path": rpath, "name": rname, "label": label,
            "units": spec.get("units"),
            "value_per_unit": vpu,
            "pixel_area_ha": px_area_ha,
            "area_weighted": use_aw,
            "unit_sum_total": grand_sum,
        }
        if vpu is not None:
            try:
                vpu_f = float(vpu)
            except (TypeError, ValueError):
                notes.append(f"{label}: value_per_unit {vpu!r} is not a number; "
                             "no valuation column.")
            else:
                new_cols[f"val_{label}"] = [
                    (s * vpu_f) if s is not None else None for s in sums_used
                ]
                total["value_total"] = grand_sum * vpu_f
                total["value_currency"] = currency
        raster_totals.append(total)

    if not new_cols:
        return {"ok": False, "error": "no raster could be aggregated; see notes.",
                "notes": notes}

    out_gdf = gdf[id_cols + [gdf.geometry.name]].copy()
    for col, vals in new_cols.items():
        out_gdf[col] = vals

    if dst_path.suffix.lower() == ".shp":
        longnames = [c for c in new_cols if len(c) > 10]
        if longnames:
            notes.append("Shapefile output truncates field names to 10 chars "
                         f"(and may merge collisions): {', '.join(longnames)}. "
                         "Use a .gpkg destination to keep the full names.")

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    out_gdf.to_file(dst_path)
    csv_path = dst_path.with_suffix(".csv")
    out_gdf.drop(columns=[out_gdf.geometry.name]).to_csv(csv_path, index=False)

    features: list[dict] = []
    for pos, row in out_gdf.iterrows():
        features.append({
            "feature_index": int(pos),
            "properties": {c: _jsonable(row[c]) for c in id_cols},
            "values": {c: _jsonable(row[c]) for c in new_cols},
        })

    result = {
        "ok": True,
        "units": {
            "path": units_path,
            "feature_count": int(len(out_gdf)),
            "crs": gdf.crs.to_string(),
            "id_columns": id_cols,
            "truncated": truncated,
        },
        "rasters": raster_totals,
        "stats": stats,
        "columns": list(new_cols),
        "features": features,
        "output_vector": _describe_vector(dst_path),
        "csv_path": str(csv_path),
        "notes": notes,
    }
    sidecar = dst_path.with_name(dst_path.stem + "_aggregate.json")
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
        result = aggregate(payload)
    except Exception:  # noqa: BLE001 - never crash silently across the MCP boundary
        result = {"ok": False, "error": "aggregate worker crashed",
                  "traceback": traceback.format_exc()}
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
