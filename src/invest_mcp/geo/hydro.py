"""Watershed-delineation worker -- RUNS IN THE ``invest-geo`` CONDA ENV.

Invoked by :mod:`invest_mcp.geo.client` as::

    <invest-geo>/python.exe -m invest_mcp.geo.hydro   < payload.json  > result.json

Runs the full pygeoprocessing D8 routing chain and cuts watershed polygons for a
set of outlet points -- the same engine ``natcap.invest`` uses internally, so the
basins line up with how SDR / NDR / SWY route:

    fill_pits -> flow_dir_d8 -> flow_accumulation_d8 -> extract_streams_d8
              -> (snap outlets to the stream network) -> delineate_watersheds_d8

Input payload (stdin, JSON)::

    {
      "dem_path": "C:/.../DEM.tif",
      "outlets_path": "C:/.../pour_points.shp",
      "dst_path": "C:/.../data/processed/watersheds.gpkg",
      "threshold_flow_accumulation": 1000,   # px, for stream extraction + snapping
      "snap_distance_px": 10,                 # 0 disables snapping
      "fill_pits": true,
      "keep_intermediate": false
    }

Output (stdout, JSON): a description of the watersheds vector, a per-point snap
report, the flow-accumulation threshold used, and (when kept) the paths of the
intermediate rasters. Intermediates and pygeoprocessing's own temp files live in
``<dst_stem>_hydro/`` next to the destination.

Only stdlib is imported at module load; rasterio / geopandas / pygeoprocessing
are imported inside :func:`delineate` so a GDAL-less ``.venv`` can import this
module without executing it.
"""

from __future__ import annotations

import json
import shutil
import sys
import traceback
from pathlib import Path

_VECTOR_SUFFIXES = {".shp", ".gpkg", ".geojson", ".json", ".gml", ".fgb"}


# ---------------------------------------------------------------------------
# snap outlet points onto the derived stream network
# ---------------------------------------------------------------------------
def _snap_points_to_streams(points_xy, streams_path: str, radius_px: int):
    """Move each (x, y) to the nearest stream pixel centre within ``radius_px``.

    Windowed read around each point -- cost is independent of the stream
    network's size. Returns ``(snapped_xy, reports)``.
    """
    import numpy as np
    import rasterio
    from rasterio.windows import Window

    snapped: list[tuple[float, float]] = []
    reports: list[dict] = []
    with rasterio.open(streams_path) as ds:
        px_w, px_h = abs(ds.transform.a), abs(ds.transform.e)
        for i, (x, y) in enumerate(points_xy):
            row, col = ds.index(x, y)
            r0, r1 = max(0, row - radius_px), min(ds.height, row + radius_px + 1)
            c0, c1 = max(0, col - radius_px), min(ds.width, col + radius_px + 1)
            if r0 >= r1 or c0 >= c1:
                snapped.append((x, y))
                reports.append({"point_index": i, "snapped": False,
                                "reason": "outside raster"})
                continue
            block = ds.read(1, window=Window(c0, r0, c1 - c0, r1 - r0))
            ys, xs = np.where(block == 1)
            if ys.size == 0:
                snapped.append((x, y))
                reports.append({"point_index": i, "snapped": False,
                                "reason": f"no stream within {radius_px}px"})
                continue
            # pixel-centre coordinates of the candidate stream pixels
            cx, cy = rasterio.transform.xy(ds.transform, ys + r0, xs + c0)
            cx = np.asarray(cx, dtype="float64")
            cy = np.asarray(cy, dtype="float64")
            d = np.hypot(cx - x, cy - y)
            k = int(np.argmin(d))
            snapped.append((float(cx[k]), float(cy[k])))
            reports.append({
                "point_index": i, "snapped": True,
                "distance_map_units": round(float(d[k]), 4),
                "distance_px": round(float(d[k]) / ((px_w + px_h) / 2), 3),
            })
    return snapped, reports


# ---------------------------------------------------------------------------
def _describe_vector(path) -> dict:
    import pyogrio

    info = pyogrio.read_info(path)
    tb = info.get("total_bounds")
    return {
        "path": str(path),
        "crs": (info.get("crs") or None),
        "feature_count": int(info.get("features") or 0),
        "geometry_type": info.get("geometry_type"),
        "bounds": ([float(v) for v in tb] if tb is not None else None),
    }


def delineate(payload: dict) -> dict:
    import geopandas as gpd
    import rasterio
    from pygeoprocessing import routing

    dem_path = payload["dem_path"]
    outlets_path = payload["outlets_path"]
    dst_path = Path(payload["dst_path"])
    threshold = float(payload.get("threshold_flow_accumulation") or 1000)
    snap_px = int(payload.get("snap_distance_px") or 0)
    do_fill = bool(payload.get("fill_pits", True))
    keep = bool(payload.get("keep_intermediate", False))

    if dst_path.suffix.lower() not in _VECTOR_SUFFIXES:
        return {"ok": False, "error": f"dst_path must be a vector file, got {dst_path.suffix!r}"}

    work = dst_path.parent / f"{dst_path.stem}_hydro"
    tmp = work / "_tmp"
    work.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    with rasterio.open(dem_path) as dem:
        if dem.crs is None:
            return {"ok": False, "error": f"{dem_path} has no CRS."}
        dem_crs = dem.crs

    # 1. outlets -> DEM CRS
    gdf = gpd.read_file(outlets_path)
    if gdf.crs is None:
        return {"ok": False, "error": f"{outlets_path} has no CRS; cannot align to the DEM."}
    if gdf.crs != dem_crs:
        gdf = gdf.to_crs(dem_crs)
    is_points = gdf.geom_type.isin(["Point", "MultiPoint"]).all()

    # 2. fill pits
    if do_fill:
        filled = str(work / "dem_filled.tif")
        routing.fill_pits((dem_path, 1), filled, working_dir=str(tmp))
    else:
        filled = dem_path
        notes.append("fill_pits skipped -- flow direction is undefined over any pits.")

    # 3-5. flow dir -> accumulation -> streams
    flow_dir = str(work / "flow_dir_d8.tif")
    routing.flow_dir_d8((filled, 1), flow_dir, working_dir=str(tmp))
    flow_accum = str(work / "flow_accumulation.tif")
    routing.flow_accumulation_d8((flow_dir, 1), flow_accum)
    streams = str(work / "streams.tif")
    routing.extract_streams_d8((flow_accum, 1), threshold, streams)

    # 6. snap outlet points to the stream network
    snap_report: list[dict] = []
    if snap_px > 0 and is_points:
        pts = [(geom.x, geom.y) for geom in gdf.geometry]
        snapped, snap_report = _snap_points_to_streams(pts, streams, snap_px)
        from shapely.geometry import Point

        gdf = gdf.set_geometry([Point(xy) for xy in snapped])
    elif snap_px > 0:
        notes.append("snap skipped -- outlet geometries are not points.")

    outlets_prepared = str(work / "outlets_prepared.gpkg")
    gdf.to_file(outlets_prepared, driver="GPKG")

    # 7. delineate (pygeoprocessing insists on a .gpkg target)
    ws_gpkg = str(work / "watersheds.gpkg")
    routing.delineate_watersheds_d8((flow_dir, 1), outlets_prepared, ws_gpkg,
                                    working_dir=str(tmp))

    ws_desc = _describe_vector(ws_gpkg)
    if not ws_desc["feature_count"]:
        return {"ok": False,
                "error": "no watersheds delineated -- outlet points may not sit on "
                         "valid flow-direction pixels. Increase snap_distance_px or "
                         "lower threshold_flow_accumulation.",
                "snap_report": snap_report}

    # 8. write to the requested format / location
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if dst_path.suffix.lower() == ".gpkg":
        shutil.copyfile(ws_gpkg, dst_path)
    else:
        gpd.read_file(ws_gpkg).to_file(dst_path)

    # 9. tidy
    shutil.rmtree(tmp, ignore_errors=True)
    intermediates = {}
    if keep:
        intermediates = {
            "filled_dem": filled if do_fill else None,
            "flow_dir_d8": flow_dir,
            "flow_accumulation": flow_accum,
            "streams": streams,
            "outlets_prepared": outlets_prepared,
        }
    else:
        shutil.rmtree(work, ignore_errors=True)

    result = {
        "ok": True,
        "watersheds": {**_describe_vector(dst_path), "path": str(dst_path)},
        "threshold_flow_accumulation": threshold,
        "snapped": bool(snap_px > 0 and is_points),
        "snap_report": snap_report,
        "intermediates": intermediates,
        "notes": notes,
    }
    return result


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        json.dump({"ok": False, "error": f"bad payload: {exc}"}, sys.stdout)
        return
    try:
        result = delineate(payload)
    except Exception as exc:  # noqa: BLE001 - never crash silently across the MCP boundary
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
