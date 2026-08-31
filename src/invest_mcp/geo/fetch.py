"""Remote-data fetch worker -- RUNS IN THE ``invest-geo`` CONDA ENV.

Invoked by :mod:`invest_mcp.geo.client` as::

    <invest-geo>/python.exe -m invest_mcp.geo.fetch   < payload.json  > result.json

Downloads a raster layer for an area of interest from an open, no-auth source and
lands it as a GeoTIFF -- optionally reprojected onto the project CRS and clipped
to the AOI polygon. The reproject / clip steps reuse the primitives in
:mod:`invest_mcp.geo.prep`.

Common payload keys (``op`` = ``"dem"`` or ``"landcover"``)::

    {"op": "...", "source": "...",
     "bbox_wgs84": [minx, miny, maxx, maxy] | null,
     "aoi_path": "C:/.../aoi.shp" | null,     # bounds taken from here if bbox absent
     "clip_to_aoi": true,                     # mask to the AOI polygon after mosaic
     "buffer_deg": 0.05,
     "dst_path": "C:/.../data/raw/<layer>.tif",
     "target_crs": "EPSG:32733" | null,
     "target_resolution": [x, y] | null,
     "resampling": "bilinear" | "nearest" | ...,   # default per op
     "keep_intermediate": false,
     "year": 2021}                            # landcover only

Sources:
    ``cop30``       Copernicus DEM GLO-30 (~30 m, near-global) from the public AWS
                    bucket ``copernicus-dem-30m`` -- 1x1 degree COG tiles.
    ``worldcover``  ESA WorldCover 10 m land cover (2020 = v100, 2021 = v200) from
                    the public AWS bucket ``esa-worldcover`` -- 3x3 degree COG
                    tiles, 11 classes.

Both are read straight over HTTPS with GDAL ``/vsicurl/`` -- no credentials.

Output (stdout, JSON): a description of the written raster, the tiles used /
missing, the provider host, and the (buffered) WGS84 bbox.

Only stdlib is imported at module load. Note: the mosaic is read into memory for
the reproject step -- fine for typical watershed extents, but a very large AOI
would want windowed processing (same caveat as :mod:`invest_mcp.geo.compare`).
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import traceback
from pathlib import Path

# keep GDAL's remote reads snappy and resilient
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("GDAL_HTTP_TIMEOUT", "60")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "3")
os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "2")
os.environ.setdefault("CPL_VSIL_CURL_USE_HEAD", "NO")

_COP30_HOST = "https://copernicus-dem-30m.s3.amazonaws.com"
_WORLDCOVER_HOST = "https://esa-worldcover.s3.eu-central-1.amazonaws.com"
_DEM_NODATA = -9999.0

# ESA WorldCover class values -> label (both v100/2020 and v200/2021).
_WORLDCOVER_LEGEND = {
    10: "Tree cover", 20: "Shrubland", 30: "Grassland", 40: "Cropland",
    50: "Built-up", 60: "Bare / sparse vegetation", 70: "Snow and ice",
    80: "Permanent water bodies", 90: "Herbaceous wetland", 95: "Mangroves",
    100: "Moss and lichen",
}


def _corner(lat: int, lon: int) -> str:
    ns = f"N{lat:02d}" if lat >= 0 else f"S{-lat:02d}"
    ew = f"E{lon:03d}" if lon >= 0 else f"W{-lon:03d}"
    return f"{ns}{ew}"


# ---------------------------------------------------------------------------
# Copernicus GLO-30 tiling (1 degree)
# ---------------------------------------------------------------------------
def _cop30_tile_name(lat: int, lon: int) -> str:
    c = _corner(lat, lon)
    return f"Copernicus_DSM_COG_10_{c[:3]}_00_{c[3:]}_00_DEM"


def _cop30_urls(bbox: list[float]) -> list[tuple[str, str]]:
    """1x1 degree tiles (named by SW corner) covering ``bbox`` = [minx,miny,maxx,maxy]."""
    minx, miny, maxx, maxy = bbox
    lat_lo, lat_hi = math.floor(miny), math.floor(maxy - 1e-9)
    lon_lo, lon_hi = math.floor(minx), math.floor(maxx - 1e-9)
    out: list[tuple[str, str]] = []
    for lat in range(lat_lo, lat_hi + 1):
        for lon in range(lon_lo, lon_hi + 1):
            name = _cop30_tile_name(lat, lon)
            out.append((name, f"/vsicurl/{_COP30_HOST}/{name}/{name}.tif"))
    return out


# ---------------------------------------------------------------------------
# ESA WorldCover tiling (3 degree)
# ---------------------------------------------------------------------------
def _floor3(v: float) -> int:
    return int(math.floor(v / 3.0) * 3)


def _worldcover_urls(bbox: list[float], year: int) -> list[tuple[str, str]]:
    """3x3 degree tiles (SW corner on a 3-degree grid) covering ``bbox``."""
    ver, y = ("v200", "2021") if int(year) >= 2021 else ("v100", "2020")
    minx, miny, maxx, maxy = bbox
    lat_lo, lat_hi = _floor3(miny), _floor3(maxy - 1e-9)
    lon_lo, lon_hi = _floor3(minx), _floor3(maxx - 1e-9)
    base = f"{_WORLDCOVER_HOST}/{ver}/{y}/map"
    out: list[tuple[str, str]] = []
    for lat in range(lat_lo, lat_hi + 1, 3):
        for lon in range(lon_lo, lon_hi + 1, 3):
            name = f"ESA_WorldCover_10m_{y}_{ver}_{_corner(lat, lon)}_Map"
            out.append((name, f"/vsicurl/{base}/{name}.tif"))
    return out


# ---------------------------------------------------------------------------
def _resolve_bbox(payload: dict):
    """Return ``(bbox_wgs84, aoi_gdf_or_None)`` from bbox / aoi_path + buffer."""
    import geopandas as gpd

    buf = float(payload.get("buffer_deg") or 0.0)
    bbox = payload.get("bbox_wgs84")
    aoi_path = payload.get("aoi_path")
    aoi = None
    if aoi_path:
        aoi = gpd.read_file(aoi_path)
        if aoi.crs is None:
            raise ValueError(f"{aoi_path} has no CRS.")
        aoi = aoi.to_crs(4326)
        if bbox is None:
            bbox = list(aoi.total_bounds)
    if bbox is None:
        raise ValueError("provide bbox_wgs84 or aoi_path")
    bbox = [float(bbox[0]) - buf, float(bbox[1]) - buf,
            float(bbox[2]) + buf, float(bbox[3]) + buf]
    bbox = [max(-180.0, bbox[0]), max(-90.0, bbox[1]),
            min(180.0, bbox[2]), min(90.0, bbox[3])]
    if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        raise ValueError(f"degenerate bbox {bbox}")
    return bbox, aoi


def reproject_clip_describe(wgs84_tif, dst, work, *, target_crs=None,
                            target_resolution=None, resampling=None,
                            default_resampling="bilinear",
                            clip_aoi_path=None) -> dict:
    """Common tail for every fetch: take an EPSG:4326 GeoTIFF, optionally
    reproject and clip it to the AOI polygon, and describe the result. Reuses
    the prep primitives. Also used by :mod:`invest_mcp.geo.climate`."""
    from invest_mcp.geo.prep import _describe_raster, _op_clip, _op_reproject

    dst = Path(dst)
    current = str(wgs84_tif)
    if target_crs:
        rp = str(Path(work) / f"{dst.stem}_proj.tif")
        _op_reproject({"src": current, "dst": rp, "target_crs": target_crs,
                       "resampling": resampling or default_resampling,
                       "resolution": target_resolution, "kind": "raster"})
        current = rp
    if clip_aoi_path:
        _op_clip({"src": current, "dst": str(dst), "aoi": clip_aoi_path,
                  "kind": "raster", "all_touched": True})
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(current, dst)
    return _describe_raster(dst)


def _download_layer(payload: dict, urls: list[tuple[str, str]], *,
                    nodata_override: float | None, default_resampling: str,
                    coverage_hint: str) -> dict:
    """Open the /vsicurl/ tiles that exist, mosaic to the bbox, then reproject /
    clip. Returns ``{"raster", "tiles_used", "tiles_missing"}``."""
    import rasterio
    from rasterio.merge import merge as rio_merge

    dst = Path(payload["dst_path"])
    work = dst.parent / f"{dst.stem}_fetch"
    work.mkdir(parents=True, exist_ok=True)
    bbox = payload["_bbox"]

    used: list[str] = []
    missing: list[str] = []
    srcs = []
    for name, url in urls:
        try:
            srcs.append(rasterio.open(url))
            used.append(name)
        except rasterio.errors.RasterioIOError:
            missing.append(name)          # ocean / outside coverage
    if not srcs:
        raise RuntimeError(
            f"no tiles cover this area ({coverage_hint}). bbox={bbox}, "
            f"tiles tried={missing}")

    merge_kw = {} if nodata_override is None else {"nodata": nodata_override}
    mosaic, transform = rio_merge(srcs, bounds=tuple(bbox), **merge_kw)
    profile = srcs[0].profile.copy()
    src_nodata = srcs[0].nodata
    for s in srcs:
        s.close()
    for k in ("blockxsize", "blockysize", "tiled", "interleave", "photometric"):
        profile.pop(k, None)
    out_nodata = nodata_override if nodata_override is not None else src_nodata
    profile.update(driver="GTiff", count=1, crs="EPSG:4326", transform=transform,
                   height=mosaic.shape[1], width=mosaic.shape[2], compress="deflate")
    if out_nodata is not None:
        profile["nodata"] = out_nodata
    src_tif = str(work / "layer_wgs84.tif")
    with rasterio.open(src_tif, "w", **profile) as out:
        out.write(mosaic[0], 1)

    desc = reproject_clip_describe(
        src_tif, dst, work, target_crs=payload.get("target_crs"),
        target_resolution=payload.get("target_resolution"),
        resampling=payload.get("resampling"), default_resampling=default_resampling,
        clip_aoi_path=payload["aoi_path"] if payload.get("clip_to_aoi")
        and payload.get("aoi_path") else None)
    if not payload.get("keep_intermediate"):
        shutil.rmtree(work, ignore_errors=True)
    return {"raster": desc, "tiles_used": used, "tiles_missing": missing}


def _fetch_dem(payload: dict) -> dict:
    if (payload.get("source") or "cop30").lower() != "cop30":
        raise ValueError(f"unknown DEM source {payload.get('source')!r}; "
                         "only 'cop30' (Copernicus GLO-30) is wired up")
    bbox, _aoi = _resolve_bbox(payload)
    payload["_bbox"] = bbox
    out = _download_layer(payload, _cop30_urls(bbox),
                          nodata_override=_DEM_NODATA, default_resampling="bilinear",
                          coverage_hint="all ocean / outside GLO-30 range")
    return {**out, "source": "cop30", "provider_host": _COP30_HOST,
            "bbox_wgs84": bbox}


def _fetch_landcover(payload: dict) -> dict:
    src = (payload.get("source") or "worldcover").lower()
    if src not in ("worldcover", "esa_worldcover", "esa-worldcover"):
        raise ValueError(f"unknown land-cover source {src!r}; "
                         "only 'worldcover' (ESA WorldCover 10 m) is wired up")
    year = int(payload.get("year") or 2021)
    bbox, _aoi = _resolve_bbox(payload)
    payload["_bbox"] = bbox
    out = _download_layer(payload, _worldcover_urls(bbox, year),
                          nodata_override=None, default_resampling="nearest",
                          coverage_hint="all ocean / outside WorldCover land tiles")
    return {**out, "source": "worldcover",
            "year": 2021 if year >= 2021 else 2020,
            "provider_host": _WORLDCOVER_HOST, "bbox_wgs84": bbox,
            "class_legend": {str(k): v for k, v in _WORLDCOVER_LEGEND.items()}}


# ---------------------------------------------------------------------------
_OPS = {"dem": _fetch_dem, "landcover": _fetch_landcover}


def run(payload: dict) -> dict:
    op = payload.get("op")
    fn = _OPS.get(op)
    if fn is None:
        return {"ok": False, "error": f"unknown op {op!r}; expected one of {sorted(_OPS)}"}
    return {"ok": True, "op": op, **fn(payload)}


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        json.dump({"ok": False, "error": f"bad payload: {exc}"}, sys.stdout)
        return
    try:
        result = run(payload)
    except Exception as exc:  # noqa: BLE001 - never crash silently across the MCP boundary
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                  "traceback": traceback.format_exc()}
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
