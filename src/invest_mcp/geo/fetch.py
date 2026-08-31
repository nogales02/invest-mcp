"""Remote-data fetch worker -- RUNS IN THE ``invest-geo`` CONDA ENV.

Invoked by :mod:`invest_mcp.geo.client` as::

    <invest-geo>/python.exe -m invest_mcp.geo.fetch   < payload.json  > result.json

Downloads a raster layer for an area of interest from an open, no-auth source and
lands it as a GeoTIFF -- optionally reprojected onto the project CRS and clipped
to the AOI polygon. The reproject / clip steps reuse the primitives in
:mod:`invest_mcp.geo.prep`.

op = ``"dem"``::

    {"op": "dem", "source": "cop30",
     "bbox_wgs84": [minx, miny, maxx, maxy] | null,
     "aoi_path": "C:/.../aoi.shp" | null,     # bounds taken from here if bbox absent
     "clip_to_aoi": true,                     # mask to the AOI polygon after mosaic
     "buffer_deg": 0.05,
     "dst_path": "C:/.../data/raw/dem_cop30.tif",
     "target_crs": "EPSG:32733" | null,
     "target_resolution": [x, y] | null,
     "resampling": "bilinear",
     "keep_intermediate": false}

Sources:
    ``cop30``  Copernicus DEM GLO-30 (~30 m, global) from the public AWS bucket
               ``copernicus-dem-30m`` -- 1x1 degree COG tiles read straight over
               HTTPS with GDAL ``/vsicurl/``. No credentials.

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
_DEM_NODATA = -9999.0


# ---------------------------------------------------------------------------
# Copernicus GLO-30 tiling
# ---------------------------------------------------------------------------
def _cop30_tile_name(lat: int, lon: int) -> str:
    ns = f"N{lat:02d}" if lat >= 0 else f"S{-lat:02d}"
    ew = f"E{lon:03d}" if lon >= 0 else f"W{-lon:03d}"
    return f"Copernicus_DSM_COG_10_{ns}_00_{ew}_00_DEM"


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


def _fetch_dem(payload: dict) -> dict:
    import rasterio
    from rasterio.merge import merge as rio_merge

    if (payload.get("source") or "cop30").lower() != "cop30":
        raise ValueError(f"unknown DEM source {payload.get('source')!r}; "
                         "only 'cop30' (Copernicus GLO-30) is wired up")

    dst = Path(payload["dst_path"])
    work = dst.parent / f"{dst.stem}_fetch"
    work.mkdir(parents=True, exist_ok=True)

    bbox, _aoi = _resolve_bbox(payload)

    used: list[str] = []
    missing: list[str] = []
    srcs = []
    for name, url in _cop30_urls(bbox):
        try:
            srcs.append(rasterio.open(url))
            used.append(name)
        except rasterio.errors.RasterioIOError:
            missing.append(name)          # ocean / outside GLO-30 coverage
    if not srcs:
        raise RuntimeError(
            f"no Copernicus GLO-30 tiles cover this area (all ocean / out of "
            f"range). bbox={bbox}, tiles tried={missing}")

    mosaic, transform = rio_merge(srcs, bounds=tuple(bbox), nodata=_DEM_NODATA)
    profile = srcs[0].profile.copy()
    for s in srcs:
        s.close()
    for k in ("blockxsize", "blockysize", "tiled", "interleave", "photometric"):
        profile.pop(k, None)
    profile.update(driver="GTiff", count=1, crs="EPSG:4326", transform=transform,
                   height=mosaic.shape[1], width=mosaic.shape[2],
                   nodata=_DEM_NODATA, compress="deflate")
    src_tif = str(work / "dem_wgs84.tif")
    with rasterio.open(src_tif, "w", **profile) as out:
        out.write(mosaic[0], 1)

    # reproject / clip through the shared prep primitives
    from invest_mcp.geo.prep import _describe_raster, _op_clip, _op_reproject

    current = src_tif
    if payload.get("target_crs"):
        rp = str(work / "dem_proj.tif")
        _op_reproject({"src": current, "dst": rp, "target_crs": payload["target_crs"],
                       "resampling": payload.get("resampling") or "bilinear",
                       "resolution": payload.get("target_resolution"),
                       "kind": "raster"})
        current = rp

    if payload.get("clip_to_aoi") and payload.get("aoi_path"):
        _op_clip({"src": current, "dst": str(dst), "aoi": payload["aoi_path"],
                  "kind": "raster", "all_touched": True})
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(current, dst)

    desc = _describe_raster(dst)
    if not payload.get("keep_intermediate"):
        shutil.rmtree(work, ignore_errors=True)

    return {
        "raster": desc,
        "source": "cop30",
        "provider_host": _COP30_HOST,
        "tiles_used": used,
        "tiles_missing": missing,
        "bbox_wgs84": bbox,
    }


# ---------------------------------------------------------------------------
_OPS = {"dem": _fetch_dem}


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
