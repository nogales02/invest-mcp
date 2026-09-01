"""Deterministic data-prep worker -- RUNS IN THE ``invest-geo`` CONDA ENV.

Invoked by :mod:`invest_mcp.geo.client` as::

    <invest-geo>/python.exe -m invest_mcp.geo.prep   < payload.json  > result.json

One payload, one operation, selected by ``op``:

``reproject``
    Warp a raster (or reproject a vector) to a target CRS, with an optional
    target pixel size. ``resampling`` defaults to ``"auto"`` -- the method is
    picked from the data kind (categorical vs continuous) and the scale change
    (see :mod:`invest_mcp.geo.resampling`).
    ``{"op":"reproject", "src","dst", "target_crs",
       "resampling":"auto", "resolution":[x,y]|null,
       "categorical":null|bool, "kind":"auto"|"raster"|"vector"}``

``clip``
    Cut a raster (crop to the AOI + mask) or a vector down to an AOI polygon.
    ``{"op":"clip", "src","dst","aoi",
       "kind":"auto"|"raster"|"vector", "all_touched":false}``

``resample``
    Change a raster's pixel size (and optionally CRS) with no clip -- match a
    ``target_resolution`` [x,y] or copy the grid spacing of a ``reference``
    raster. ``method`` defaults to ``"auto"``.
    ``{"op":"resample", "src","dst",
       "target_resolution":[x,y]|null, "reference":"...|null",
       "target_crs":"...|null", "method":"auto", "categorical":null|bool}``

``align_stack``
    Put several rasters on one identical grid (same CRS, pixel size, extent and
    pixel alignment) so InVEST can stack them. The grid comes either from a
    ``reference`` raster, or from ``target_crs`` + ``resolution`` + ``extent``.
    ``resampling`` defaults to ``"auto"`` and is decided **per raster**; a
    per-item ``resampling`` / ``categorical`` overrides it, a non-``auto``
    top-level ``resampling`` forces every raster.
    ``{"op":"align_stack", "rasters":[{"src","dst","resampling"?,"categorical"?}, ...],
       "reference":"...|null",
       "target_crs":"...|null", "resolution":[x,y]|null,
       "extent":[minx,miny,maxx,maxy]|null, "resampling":"auto"}``

``plan_grid``
    Read only the headers of several rasters and recommend a common analysis
    grid (InVEST runs at the LULC grid), flagging layers that would be upsampled
    past any real detail. No file is written.
    ``{"op":"plan_grid", "rasters":["...", ...],
       "reference":"...|null", "target_crs":"...|null"}``

``raster_classes``
    Unique integer class values of a categorical raster with pixel counts --
    used to seed a biophysical-table skeleton. No file is written.
    ``{"op":"raster_classes", "src":"...", "max_classes":1000}``

Output (stdout, JSON)::

    {"ok": bool, "op": "...", "outputs": [<layer description>, ...], "notes": [...]}

or ``{"ok": false, "error": "..."}``.  Only stdlib is imported at module load;
rasterio/geopandas/pyogrio and :mod:`invest_mcp.geo.resampling`'s GDAL helpers
are imported inside the ops so a GDAL-less ``.venv`` can still import this module.

The raster ops warp through ``gdal.Warp`` (see :mod:`invest_mcp.geo.resampling`),
which streams tile-by-tile -- memory stays bounded regardless of raster size.
Only the vector paths and ``raster_classes`` read data into memory (small).
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

_RASTER_SUFFIXES = {".tif", ".tiff", ".vrt", ".img", ".nc", ".jp2"}
_VECTOR_SUFFIXES = {".shp", ".gpkg", ".geojson", ".json", ".gml", ".fgb", ".kml"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _guess_kind(path: str, declared: str | None) -> str:
    if declared in ("raster", "vector"):
        return declared
    suf = Path(path).suffix.lower()
    if suf in _RASTER_SUFFIXES:
        return "raster"
    if suf in _VECTOR_SUFFIXES:
        return "vector"
    try:  # last resort: probe
        import rasterio

        with rasterio.open(path):
            return "raster"
    except Exception:  # noqa: BLE001
        return "vector"


def _describe_raster(path) -> dict:
    from invest_mcp.geo.resampling import raster_header

    return raster_header(str(path))


def _describe_vector(path) -> dict:
    import pyogrio

    info = pyogrio.read_info(path)
    tb = info.get("total_bounds")
    crs = info.get("crs")
    return {
        "path": str(path),
        "kind": "vector",
        "crs": (crs if crs else None),
        "feature_count": int(info.get("features") or 0),
        "geometry_type": info.get("geometry_type"),
        "bounds": ([float(x) for x in tb] if tb is not None else None),
    }


def _res_metres(xres: float, crs: str | None, centre_lat: float) -> float:
    """Approximate metres-per-pixel for a target spacing, for the scale
    heuristic only -- degrees are converted with a cos(lat) factor."""
    import math

    if crs and ("4326" in str(crs) or "CRS84" in str(crs).upper()):
        return abs(xres) * 111_320.0 * max(0.05, math.cos(math.radians(centre_lat)))
    return abs(xres)


# ---------------------------------------------------------------------------
# reproject
# ---------------------------------------------------------------------------
def _op_reproject(p: dict) -> list[dict]:
    src = p["src"]
    dst = Path(p["dst"])
    dst.parent.mkdir(parents=True, exist_ok=True)
    target_crs = p.get("target_crs")
    if not target_crs:
        raise ValueError("reproject needs a target_crs")

    if _guess_kind(src, p.get("kind", "auto")) == "vector":
        import geopandas as gpd

        gdf = gpd.read_file(src)
        if gdf.crs is None:
            raise ValueError(f"{src} has no CRS; cannot reproject.")
        gdf.to_crs(target_crs).to_file(dst)
        return [_describe_vector(dst)]

    from invest_mcp.geo.resampling import (choose_resampling, classify_raster,
                                           warp_raster)

    res = p.get("resolution")
    xres = yres = None
    if res:
        xres, yres = abs(float(res[0])), abs(float(res[1]))

    cls = classify_raster(src)
    categorical = (bool(p["categorical"]) if p.get("categorical") is not None
                   else cls["categorical"])
    lat = 0.0
    b = cls.get("bounds")
    if b and cls.get("is_geographic"):
        lat = (b[1] + b[3]) / 2.0
    dst_res_m = _res_metres(xres, target_crs, lat) if xres else None
    method, note = choose_resampling(
        categorical=categorical,
        src_res_m=cls["pixel_size_m"][0],
        dst_res_m=dst_res_m,
        explicit=p.get("resampling"),
    )
    hdr = warp_raster(
        src, str(dst), dst_srs=str(target_crs), xres=xres, yres=yres,
        resample=method, src_nodata=cls["nodata"], dst_nodata=cls["nodata"],
    )
    return [{**hdr, "resampling": method, "resampling_note": note,
             "categorical": categorical}]


# ---------------------------------------------------------------------------
# clip to AOI
# ---------------------------------------------------------------------------
def _op_clip(p: dict) -> list[dict]:
    src = p["src"]
    dst = Path(p["dst"])
    dst.parent.mkdir(parents=True, exist_ok=True)
    aoi = p["aoi"]

    if _guess_kind(src, p.get("kind", "auto")) == "vector":
        import geopandas as gpd

        gdf = gpd.read_file(src)
        mask = gpd.read_file(aoi)
        if gdf.crs is not None and mask.crs is not None and mask.crs != gdf.crs:
            mask = mask.to_crs(gdf.crs)
        clipped = gpd.clip(gdf, mask)
        if clipped.empty:
            raise ValueError("clip produced zero features; AOI and layer may not overlap.")
        clipped.to_file(dst)
        return [_describe_vector(dst)]

    from invest_mcp.geo.resampling import raster_header, warp_raster

    # gdal.Warp's cutline rejects invalid rings; materialise a cleaned,
    # single-geometry cutline (make_valid + union) next to the output.
    cutline_path, cutline_srs = _clean_cutline(aoi, dst)
    try:
        src_hdr = raster_header(src)
        hdr = warp_raster(
            src, str(dst), resample="near",
            src_nodata=src_hdr["nodata"], dst_nodata=src_hdr["nodata"],
            cutline=cutline_path, cutline_srs=cutline_srs, crop_to_cutline=True,
            all_touched=bool(p.get("all_touched")),
        )
    finally:
        _cleanup(cutline_path)
    return [hdr]


def _clean_cutline(aoi: str, dst: Path) -> tuple[str, str | None]:
    """Return ``(path, crs)`` of a validity-cleaned, dissolved copy of ``aoi``."""
    import geopandas as gpd
    from shapely import make_valid
    from shapely.ops import unary_union

    gdf = gpd.read_file(aoi)
    geoms = [make_valid(g) for g in gdf.geometry if g is not None and not g.is_empty]
    if not geoms:
        raise ValueError("AOI has no usable geometry.")
    merged = unary_union(geoms)
    out = gpd.GeoDataFrame(geometry=[merged], crs=gdf.crs)
    clean_path = str(dst.with_name(dst.stem + "_cutline.gpkg"))
    out.to_file(clean_path, driver="GPKG")
    return clean_path, (str(gdf.crs) if gdf.crs is not None else None)


def _cleanup(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# resample (no clip): match a resolution or a reference grid spacing
# ---------------------------------------------------------------------------
def _op_resample(p: dict) -> list[dict]:
    src = p["src"]
    dst = Path(p["dst"])
    dst.parent.mkdir(parents=True, exist_ok=True)

    from invest_mcp.geo.resampling import (choose_resampling, classify_raster,
                                           raster_header, warp_raster)

    ref = p.get("reference")
    target_crs = p.get("target_crs") or None
    res = p.get("resolution") or p.get("target_resolution")

    if ref:
        rh = raster_header(ref)
        xres, yres = rh["pixel_size"]
        if not target_crs:
            target_crs = rh["crs"]
        dst_res_m = rh["pixel_size_m"][0]
    elif res:
        xres, yres = abs(float(res[0])), abs(float(res[1]))
        dst_res_m = None
    else:
        raise ValueError("resample needs 'target_resolution' [x,y] or 'reference' (a raster)")

    cls = classify_raster(src)
    categorical = (bool(p["categorical"]) if p.get("categorical") is not None
                   else cls["categorical"])
    if dst_res_m is None:
        lat = 0.0
        b = cls.get("bounds")
        if b and cls.get("is_geographic"):
            lat = (b[1] + b[3]) / 2.0
        dst_res_m = _res_metres(xres, target_crs or cls["crs"], lat)

    method, note = choose_resampling(
        categorical=categorical,
        src_res_m=cls["pixel_size_m"][0],
        dst_res_m=dst_res_m,
        explicit=p.get("method"),
    )
    hdr = warp_raster(
        src, str(dst), dst_srs=(str(target_crs) if target_crs else None),
        xres=xres, yres=yres, resample=method,
        src_nodata=cls["nodata"], dst_nodata=cls["nodata"],
    )
    return [{**hdr, "resampling": method, "resampling_note": note,
             "categorical": categorical}]


# ---------------------------------------------------------------------------
# align a stack of rasters onto one grid
# ---------------------------------------------------------------------------
def _target_grid(p: dict):
    """Return ``(crs, xres, yres, bounds)`` for the aligned stack."""
    from invest_mcp.geo.resampling import raster_header

    ref = p.get("reference")
    if ref:
        rh = raster_header(ref)
        if not rh["crs"]:
            raise ValueError(f"reference raster {ref} has no CRS.")
        return rh["crs"], rh["pixel_size"][0], rh["pixel_size"][1], rh["bounds"]

    crs, res, extent = p.get("target_crs"), p.get("resolution"), p.get("extent")
    if not (crs and res and extent):
        raise ValueError(
            "align_stack needs either 'reference' (a raster), or all of "
            "'target_crs', 'resolution' [x,y] and 'extent' [minx,miny,maxx,maxy]."
        )
    xres, yres = abs(float(res[0])), abs(float(res[1]))
    if xres <= 0 or yres <= 0:
        raise ValueError("resolution values must be positive.")
    minx, miny, maxx, maxy = (float(v) for v in extent)
    return str(crs), xres, yres, [minx, miny, maxx, maxy]


def _op_align_stack(p: dict) -> list[dict]:
    from invest_mcp.geo.resampling import (choose_resampling, classify_raster,
                                           warp_raster)

    items = p.get("rasters") or []
    if not items:
        raise ValueError("align_stack needs a non-empty 'rasters' list.")
    crs, xres, yres, bounds = _target_grid(p)

    top = str(p.get("resampling") or "auto").strip().lower()
    forced = top if top not in ("", "auto") else None

    is_geo = "4326" in str(crs) or "CRS84" in str(crs).upper()
    lat = (bounds[1] + bounds[3]) / 2.0 if is_geo else 0.0
    dst_res_m = _res_metres(xres, crs, lat)

    outs: list[dict] = []
    for item in items:
        src = item["src"]
        dst = Path(item["dst"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        cls = classify_raster(src)
        categorical = (bool(item["categorical"])
                       if item.get("categorical") is not None
                       else cls["categorical"])
        explicit = item.get("resampling") or forced
        method, note = choose_resampling(
            categorical=categorical,
            src_res_m=cls["pixel_size_m"][0],
            dst_res_m=dst_res_m,
            explicit=explicit,
        )
        hdr = warp_raster(
            src, str(dst), dst_srs=str(crs), xres=xres, yres=yres,
            output_bounds=bounds, output_bounds_srs=str(crs),
            resample=method, src_nodata=cls["nodata"], dst_nodata=cls["nodata"],
        )
        outs.append({**hdr, "resampling": method, "resampling_note": note,
                     "categorical": categorical})
    return outs


# ---------------------------------------------------------------------------
# plan_grid: recommend a common analysis grid from raster headers
# ---------------------------------------------------------------------------
def _op_plan_grid(p: dict) -> list[dict]:
    from invest_mcp.geo.resampling import classify_raster, raster_header

    srcs = p.get("rasters") or []
    if not srcs:
        raise ValueError("plan_grid needs a non-empty 'rasters' list.")
    target_crs = p.get("target_crs") or None
    ref = p.get("reference")

    layers: list[dict] = []
    for s in srcs:
        cls = classify_raster(s)
        layers.append({
            "path": str(s), "crs": cls["crs"], "epsg": cls["epsg"],
            "pixel_size": cls["pixel_size"],
            "pixel_size_m": cls["pixel_size_m"][0],
            "width": cls["width"], "height": cls["height"],
            "dtype": cls["dtype"], "categorical": cls["categorical"],
            "distinct_sample": cls["distinct_sample"],
        })

    if ref:
        rh = raster_header(ref)
        rec = {"source": "reference raster", "from": str(ref),
               "crs": target_crs or rh["crs"],
               "pixel_size_m": rh["pixel_size_m"][0]}
    else:
        cats = [l for l in layers if l["categorical"]]
        pool = cats or layers
        finest = min(pool, key=lambda l: l["pixel_size_m"] or 1e9)
        rec = {
            "source": ("finest categorical layer (InVEST runs at the LULC grid)"
                       if cats else "finest layer"),
            "from": finest["path"],
            "crs": target_crs or finest["crs"],
            "pixel_size_m": finest["pixel_size_m"],
        }

    rec_m = rec["pixel_size_m"] or 0.0
    warnings: list[str] = []
    for l in layers:
        if not rec_m or not l["pixel_size_m"]:
            continue
        ratio = l["pixel_size_m"] / rec_m
        if ratio >= 2.0:
            warnings.append(
                f"{Path(l['path']).name}: native ~{l['pixel_size_m']:.0f} m would be "
                f"upsampled ~{ratio:.1f}x onto a {rec_m:.0f} m grid -- no real detail "
                f"gained. Consider a coarser analysis grid or keep this layer coarse."
            )

    widest = max(
        ((l["width"] * (l["pixel_size_m"] or 0.0),
          l["height"] * (l["pixel_size_m"] or 0.0)) for l in layers),
        default=(0.0, 0.0),
    )
    est_cells = None
    if rec_m and widest[0]:
        est_cells = int(round(widest[0] / rec_m) * round(widest[1] / rec_m))
    estimate = {
        "recommended_pixel_m": rec_m or None,
        "approx_extent_m": [round(widest[0]), round(widest[1])],
        "approx_cells": est_cells,
        "approx_float32_mb": (round(est_cells * 4 / 1024 / 1024, 1)
                              if est_cells else None),
        "note": "ballpark from the widest input's extent; a real clip to the AOI "
                "will differ.",
    }
    return [{"layers": layers, "recommended_grid": rec,
             "warnings": warnings, "estimate": estimate}]


# ---------------------------------------------------------------------------
# unique class values of a (categorical) raster -- for table skeletons
# ---------------------------------------------------------------------------
def _op_raster_classes(p: dict) -> list[dict]:
    import numpy as np
    import rasterio

    src = p["src"]
    max_classes = int(p.get("max_classes") or 1000)
    with rasterio.open(src) as ds:
        nodata = ds.nodata
        arr = ds.read(1, masked=True).compressed()

    note = None
    if not np.issubdtype(arr.dtype, np.integer):
        if arr.size and float(np.max(np.abs(arr - np.round(arr)))) > 1e-9:
            note = "raster values are not integers; rounded for the class list"
        arr = np.round(arr).astype("int64")

    vals, counts = np.unique(arr, return_counts=True)
    truncated = bool(vals.size > max_classes)
    if truncated:  # keep the most common, then re-sort by value
        keep = np.argsort(counts)[::-1][:max_classes]
        vals, counts = vals[keep], counts[keep]
        order = np.argsort(vals)
        vals, counts = vals[order], counts[order]

    return [{
        "src": src,
        "nodata": (None if nodata is None else float(nodata)),
        "class_count": int(vals.size),
        "truncated": truncated,
        "classes": [{"value": int(v), "pixels": int(c)} for v, c in zip(vals, counts)],
        "note": note,
    }]


# ---------------------------------------------------------------------------
_OPS = {
    "reproject": _op_reproject,
    "clip": _op_clip,
    "resample": _op_resample,
    "align_stack": _op_align_stack,
    "plan_grid": _op_plan_grid,
    "raster_classes": _op_raster_classes,
}


def run(payload: dict) -> dict:
    op = payload.get("op")
    fn = _OPS.get(op)
    if fn is None:
        return {"ok": False, "error": f"unknown op {op!r}; expected one of {sorted(_OPS)}"}
    outputs = fn(payload)
    notes = [o["resampling_note"] for o in outputs
             if isinstance(o, dict) and o.get("resampling_note")]
    return {"ok": True, "op": op, "outputs": outputs, "notes": notes}


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        json.dump({"ok": False, "error": f"bad payload: {exc}"}, sys.stdout)
        return
    try:
        result = run(payload)
    except Exception as exc:  # noqa: BLE001 - never crash silently across the MCP boundary
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
