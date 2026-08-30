"""Deterministic data-prep worker -- RUNS IN THE ``invest-geo`` CONDA ENV.

Invoked by :mod:`invest_mcp.geo.client` as::

    <invest-geo>/python.exe -m invest_mcp.geo.prep   < payload.json  > result.json

One payload, one operation, selected by ``op``:

``reproject``
    Warp a raster (or reproject a vector) to a target CRS, with an optional
    target pixel size.
    ``{"op":"reproject", "src","dst", "target_crs",
       "resampling":"nearest", "resolution":[x,y]|null,
       "kind":"auto"|"raster"|"vector"}``

``clip``
    Cut a raster (crop to the AOI bounding box + mask) or a vector down to an
    AOI polygon. The AOI is reprojected to the layer's CRS first.
    ``{"op":"clip", "src","dst","aoi",
       "kind":"auto"|"raster"|"vector", "all_touched":false}``

``align_stack``
    Put several rasters on one identical grid (same CRS, pixel size, extent and
    pixel alignment) so InVEST can stack them. The grid comes either from a
    ``reference`` raster, or from ``target_crs`` + ``resolution`` + ``extent``.
    ``{"op":"align_stack", "rasters":[{"src","dst"}, ...],
       "reference":"...|null",
       "target_crs":"...|null", "resolution":[x,y]|null,
       "extent":[minx,miny,maxx,maxy]|null, "resampling":"nearest"}``

``raster_classes``
    Unique integer class values of a categorical raster (e.g. a LULC map) with
    pixel counts -- used to seed a biophysical-table skeleton. No file is written.
    ``{"op":"raster_classes", "src":"...", "max_classes":1000}``

Output (stdout, JSON)::

    {"ok": bool, "op": "...", "outputs": [<layer description>, ...], "notes": [...]}

or ``{"ok": false, "error": "..."}``.  Only stdlib is imported at module load;
rasterio/geopandas/pyogrio are imported inside the ops so a GDAL-less ``.venv``
can still import this module without executing it.

Note: the raster ops read whole bands into memory -- fine for typical InVEST
inputs, but a very large raster would need windowed processing (same caveat as
:mod:`invest_mcp.geo.compare`).
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

_RASTER_SUFFIXES = {".tif", ".tiff", ".vrt", ".img", ".nc", ".jp2"}
_VECTOR_SUFFIXES = {".shp", ".gpkg", ".geojson", ".json", ".gml", ".fgb", ".kml"}
# GeoTIFF profile keys copied from the source that can break a fresh write of a
# smaller/re-gridded raster -- dropped before writing (see geo/compare.py).
_UNSAFE_PROFILE_KEYS = ("blockxsize", "blockysize", "tiled", "interleave", "photometric")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _resampling(name: str | None):
    from rasterio.enums import Resampling

    key = str(name or "nearest").strip().lower()
    try:
        return Resampling[key]
    except KeyError:
        raise ValueError(
            f"unknown resampling {name!r}; use one of "
            + ", ".join(r.name for r in Resampling)
        ) from None


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


def _clean_profile(profile: dict) -> dict:
    return {k: v for k, v in profile.items() if k not in _UNSAFE_PROFILE_KEYS}


def _describe_raster(path) -> dict:
    import rasterio

    with rasterio.open(path) as ds:
        a, e = ds.transform.a, ds.transform.e
        crs = ds.crs
        return {
            "path": str(path),
            "kind": "raster",
            "crs": (crs.to_string() if crs else None),
            "epsg": (crs.to_epsg() if crs else None),
            "width": ds.width,
            "height": ds.height,
            "pixel_size": [abs(a), abs(e)],
            "bounds": [ds.bounds.left, ds.bounds.bottom, ds.bounds.right, ds.bounds.top],
            "nodata": (None if ds.nodata is None else float(ds.nodata)),
            "dtype": ds.dtypes[0],
            "band_count": ds.count,
        }


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

    import numpy as np
    import rasterio
    from rasterio.warp import calculate_default_transform, reproject

    res = p.get("resolution")
    with rasterio.open(src) as ds:
        if ds.crs is None:
            raise ValueError(f"{src} has no CRS; cannot reproject.")
        kw = {}
        if res:
            kw["resolution"] = (float(res[0]), float(res[1]))
        transform, width, height = calculate_default_transform(
            ds.crs, target_crs, ds.width, ds.height, *ds.bounds, **kw
        )
        profile = _clean_profile(ds.profile.copy())
        profile.update(
            crs=target_crs, transform=transform, width=width, height=height,
            compress="deflate",
        )
        src_nodata = ds.nodata
        dst_arr = np.zeros((ds.count, height, width), dtype=profile["dtype"])
        reproject(
            source=ds.read(),
            destination=dst_arr,
            src_transform=ds.transform,
            src_crs=ds.crs,
            dst_transform=transform,
            dst_crs=target_crs,
            src_nodata=src_nodata,
            dst_nodata=src_nodata,
            resampling=_resampling(p.get("resampling")),
        )
    with rasterio.open(dst, "w", **profile) as out:
        out.write(dst_arr)
    return [_describe_raster(dst)]


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

    import geopandas as gpd
    import rasterio
    from rasterio.mask import mask as rio_mask

    mask_gdf = gpd.read_file(aoi)
    with rasterio.open(src) as ds:
        if ds.crs is None:
            raise ValueError(f"{src} has no CRS; cannot clip.")
        if mask_gdf.crs is not None and mask_gdf.crs != ds.crs:
            mask_gdf = mask_gdf.to_crs(ds.crs)
        geoms = [
            g.__geo_interface__
            for g in mask_gdf.geometry
            if g is not None and not g.is_empty
        ]
        if not geoms:
            raise ValueError("AOI has no usable geometry.")
        out_img, out_transform = rio_mask(
            ds, geoms, crop=True, all_touched=bool(p.get("all_touched"))
        )
        profile = _clean_profile(ds.profile.copy())
    profile.update(
        height=out_img.shape[1], width=out_img.shape[2],
        transform=out_transform, compress="deflate",
    )
    with rasterio.open(dst, "w", **profile) as out:
        out.write(out_img)
    return [_describe_raster(dst)]


# ---------------------------------------------------------------------------
# align a stack of rasters onto one grid
# ---------------------------------------------------------------------------
def _target_grid(p: dict):
    """Return ``(crs, transform, width, height)`` for the aligned stack."""
    import rasterio
    from rasterio.transform import from_origin

    ref = p.get("reference")
    if ref:
        with rasterio.open(ref) as ds:
            if ds.crs is None:
                raise ValueError(f"reference raster {ref} has no CRS.")
            return ds.crs, ds.transform, ds.width, ds.height

    crs, res, extent = p.get("target_crs"), p.get("resolution"), p.get("extent")
    if not (crs and res and extent):
        raise ValueError(
            "align_stack needs either 'reference' (a raster), or all of "
            "'target_crs', 'resolution' [x,y] and 'extent' [minx,miny,maxx,maxy]."
        )
    minx, miny, maxx, maxy = (float(v) for v in extent)
    xres, yres = abs(float(res[0])), abs(float(res[1]))
    if xres <= 0 or yres <= 0:
        raise ValueError("resolution values must be positive.")
    width = max(1, int(round((maxx - minx) / xres)))
    height = max(1, int(round((maxy - miny) / yres)))
    return (
        rasterio.crs.CRS.from_user_input(crs),
        from_origin(minx, maxy, xres, yres),
        width,
        height,
    )


def _op_align_stack(p: dict) -> list[dict]:
    import numpy as np
    import rasterio
    from rasterio.warp import reproject

    items = p.get("rasters") or []
    if not items:
        raise ValueError("align_stack needs a non-empty 'rasters' list.")
    crs, transform, width, height = _target_grid(p)
    resamp = _resampling(p.get("resampling"))

    outs: list[dict] = []
    for item in items:
        src = item["src"]
        dst = Path(item["dst"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(src) as ds:
            if ds.crs is None:
                raise ValueError(f"{src} has no CRS; cannot align.")
            src_nodata = ds.nodata
            fill = src_nodata if src_nodata is not None else 0
            dst_arr = np.full((ds.count, height, width), fill, dtype=ds.dtypes[0])
            reproject(
                source=ds.read(),
                destination=dst_arr,
                src_transform=ds.transform,
                src_crs=ds.crs,
                dst_transform=transform,
                dst_crs=crs,
                src_nodata=src_nodata,
                dst_nodata=src_nodata,
                resampling=resamp,
            )
            profile = _clean_profile(ds.profile.copy())
        profile.update(
            crs=crs, transform=transform, width=width, height=height,
            compress="deflate",
        )
        with rasterio.open(dst, "w", **profile) as out:
            out.write(dst_arr)
        outs.append(_describe_raster(dst))
    return outs


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
    "align_stack": _op_align_stack,
    "raster_classes": _op_raster_classes,
}


def run(payload: dict) -> dict:
    op = payload.get("op")
    fn = _OPS.get(op)
    if fn is None:
        return {"ok": False, "error": f"unknown op {op!r}; expected one of {sorted(_OPS)}"}
    outputs = fn(payload)
    return {"ok": True, "op": op, "outputs": outputs, "notes": []}


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
