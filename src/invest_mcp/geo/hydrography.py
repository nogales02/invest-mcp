"""Hydrography fetch worker -- RUNS IN THE ``invest-geo`` CONDA ENV.

Invoked by :mod:`invest_mcp.geo.client` as::

    <invest-geo>/python.exe -m invest_mcp.geo.hydrography   < payload.json  > result.json

Pulls the river network / basin polygons an InVEST study usually needs for
context and reporting from **HydroSHEDS v1** (WWF / McGill, no credentials) and
lands them as a vector file clipped to the area of interest.

``product``:

* ``"rivers"``  -> HydroRIVERS v1.0 line network (attributes: discharge, upstream
  area, Strahler order, the ``NEXT_DOWN`` topology, ...).
* ``"basins"``  -> HydroBASINS v1c standard (no lakes) polygons at a Pfafstetter
  ``level`` 1..12 (1 = continent-scale, 12 = smallest sub-basins; 8 is a good
  default for a watershed study).

Both are distributed as one zipped shapefile per continental region
(``af ar as au eu gr na sa si``). We read the shapefile straight out of the
remote zip with GDAL ``/vsizip//vsicurl/`` and a bounding-box filter -- the
shapefiles ship an ``.sbn`` spatial index so only the features near the AOI are
transferred, not the whole continent.

Payload (stdin, JSON)::

    {"product": "rivers" | "basins",
     "region": "eu" | ... | null,       # auto-detected from the AOI centroid if null
     "level": 8,                          # basins only, 1..12
     "bbox_wgs84": [minx, miny, maxx, maxy] | null,
     "aoi_path": "C:/.../aoi.shp" | null, # bounds taken from here if bbox absent
     "clip_to_aoi": false,               # true = geometrically clip to the polygon;
                                         #        false = keep whole features that intersect
     "buffer_deg": 0.05,
     "dst_path": "C:/.../data/raw/rivers.gpkg",
     "target_crs": "EPSG:32632" | null,
     "keep_intermediate": false}

Output (stdout, JSON): ``{"ok", "product", "region", "level"?, "source",
"provider_host", "bbox_wgs84", "feature_count", "vector": {...}, "attributes":
[...], "notes": [...], "citation"}``.

Only stdlib is imported at module load; geopandas / pyogrio load inside the
functions (same rule as the other geo/ workers).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

# keep GDAL's remote reads snappy and resilient; files.hydrosheds.org blocks HEAD
# so vsicurl must fall back to ranged GET (CPL_VSIL_CURL_USE_HEAD=NO).
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("GDAL_HTTP_TIMEOUT", "120")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "3")
os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "2")
os.environ.setdefault("CPL_VSIL_CURL_USE_HEAD", "NO")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".zip,.shp,.dbf,.shx,.sbn,.sbx,.prj")

_HYDROSHEDS_HOST = "https://data.hydrosheds.org"
_HYDRORIVERS_BASE = f"{_HYDROSHEDS_HOST}/file/HydroRIVERS"
_HYDROBASINS_BASE = f"{_HYDROSHEDS_HOST}/file/hydrobasins/standard"

_PRODUCTS = ("rivers", "basins")
_REGIONS = ("af", "ar", "as", "au", "eu", "gr", "na", "sa", "si")
_REGION_NAMES = {
    "af": "Africa", "ar": "North American Arctic", "as": "Central & South-East Asia",
    "au": "Australasia", "eu": "Europe & Middle East", "gr": "Greenland",
    "na": "North & Central America", "sa": "South America", "si": "Siberia",
}
# Generous lon/lat envelopes per HydroSHEDS continental region. They overlap on
# purpose (the Middle East is in both ``af`` and ``eu``, etc.) -- auto-detection
# only accepts an *unambiguous* single hit, otherwise it asks for ``region``.
_REGION_BBOX = {
    "af": (-19.0, -35.0, 55.0, 38.5),
    "ar": (-165.0, 48.0, -51.0, 84.0),
    "as": (55.0, 0.5, 151.0, 56.0),
    "au": (92.0, -56.0, 180.0, 25.0),
    "eu": (-25.0, 11.0, 70.0, 62.5),
    "gr": (-75.0, 58.0, -10.0, 84.5),
    "na": (-138.0, 4.5, -51.0, 62.0),
    "sa": (-93.0, -56.5, -32.0, 15.5),
    "si": (57.0, 44.0, 180.0, 81.5),
}

_CITATION = (
    "Lehner, B., Grill, G. (2013): Global river hydrography and network routing: "
    "baseline data and new approaches to study the world's large river systems. "
    "Hydrological Processes 27(15): 2171-2186. Data: HydroSHEDS v1 "
    "(https://www.hydrosheds.org)."
)

_DRIVER_BY_SUFFIX = {
    ".gpkg": "GPKG", ".shp": "ESRI Shapefile",
    ".geojson": "GeoJSON", ".json": "GeoJSON", ".fgb": "FlatGeobuf",
}


# ---------------------------------------------------------------------------
def _regions_containing(lon: float, lat: float) -> list[str]:
    hits = []
    for reg, (minx, miny, maxx, maxy) in _REGION_BBOX.items():
        if minx <= lon <= maxx and miny <= lat <= maxy:
            hits.append(reg)
    return hits


def _resolve_region(payload: dict, bbox: list[float]) -> str:
    """Return a HydroSHEDS region code. Explicit ``region`` wins; otherwise the
    AOI centroid must fall in exactly one region envelope."""
    given = (payload.get("region") or "").strip().lower()
    if given:
        if given not in _REGIONS:
            raise ValueError(
                f"region must be one of {_REGIONS} "
                f"({', '.join(f'{k}={v}' for k, v in _REGION_NAMES.items())})")
        return given

    lon = (bbox[0] + bbox[2]) / 2.0
    lat = (bbox[1] + bbox[3]) / 2.0
    hits = _regions_containing(lon, lat)
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(
            f"AOI centroid ({lon:.3f}, {lat:.3f}) is outside every HydroSHEDS "
            f"region envelope; pass region= one of {_REGIONS}")
    raise ValueError(
        f"AOI centroid ({lon:.3f}, {lat:.3f}) falls in several HydroSHEDS regions "
        f"{hits}; pass region= to disambiguate "
        f"({', '.join(f'{h}={_REGION_NAMES[h]}' for h in hits)})")


def _source_uri(product: str, region: str, level: int) -> str:
    """The ``/vsizip//vsicurl/`` URI of the shapefile inside the remote zip."""
    if product == "rivers":
        zip_url = f"{_HYDRORIVERS_BASE}/HydroRIVERS_v10_{region}_shp.zip"
        inner = f"HydroRIVERS_v10_{region}_shp/HydroRIVERS_v10_{region}.shp"
        return f"/vsizip//vsicurl/{zip_url}/{inner}"
    zip_url = f"{_HYDROBASINS_BASE}/hybas_{region}_lev{level:02d}_v1c.zip"
    return f"/vsizip//vsicurl/{zip_url}"


def _pick_driver(dst: Path) -> str:
    drv = _DRIVER_BY_SUFFIX.get(dst.suffix.lower())
    if drv is None:
        raise ValueError(
            f"unsupported vector extension {dst.suffix!r}; use one of "
            f"{sorted(_DRIVER_BY_SUFFIX)}")
    return drv


# ---------------------------------------------------------------------------
def fetch_hydrography(payload: dict) -> dict:
    import geopandas as gpd

    from invest_mcp.geo.fetch import _resolve_bbox
    from invest_mcp.geo.prep import _describe_vector

    product = (payload.get("product") or "").strip().lower()
    if product not in _PRODUCTS:
        raise ValueError(f"product must be one of {_PRODUCTS}")
    source = (payload.get("source") or "hydrosheds").lower()
    if source not in ("hydrosheds", "hydrorivers", "hydrobasins"):
        raise ValueError(f"unknown hydrography source {source!r}; only 'hydrosheds' is wired up")

    level = int(payload.get("level") or 8)
    if product == "basins" and not (1 <= level <= 12):
        raise ValueError("basins level must be an integer 1..12")

    bbox, aoi = _resolve_bbox(payload)          # buffered lon/lat bbox + AOI gdf (4326) or None
    region = _resolve_region(payload, bbox)

    dst = Path(payload["dst_path"])
    driver = _pick_driver(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    uri = _source_uri(product, region, level)
    gdf = gpd.read_file(uri, bbox=tuple(bbox))
    if gdf.crs is None:
        gdf.set_crs(4326, inplace=True)

    clipped = False
    if not gdf.empty and payload.get("clip_to_aoi") and aoi is not None:
        mask = aoi.to_crs(gdf.crs)
        gdf = gpd.clip(gdf, mask)
        clipped = True

    target_crs = payload.get("target_crs")
    if not gdf.empty and target_crs:
        gdf = gdf.to_crs(target_crs)

    notes: list[str] = []
    if gdf.empty:
        notes.append(
            f"no HydroSHEDS {product} features intersect this AOI in region "
            f"'{region}' -- check the region / bbox, or widen buffer_deg.")
        # still write an (empty) file so downstream steps have a path
        gdf = gdf.set_geometry(gdf.geometry)

    if dst.exists():
        dst.unlink()
    for sidecar in dst.parent.glob(dst.stem + ".*"):
        if sidecar != dst:
            sidecar.unlink()
    gdf.to_file(dst, driver=driver)

    if clipped:
        notes.append("features geometrically clipped to the AOI polygon "
                     "(line ends are truncated at the boundary).")
    else:
        notes.append("whole features that intersect the AOI bounding box are kept "
                     "(set clip_to_aoi=true to cut them to the polygon).")
    if product == "rivers":
        notes.append("HydroRIVERS carries long-term average discharge (DIS_AV_CMS) "
                     "and upstream area (UPLAND_SKM); it is not a routed DEM product.")
    else:
        notes.append(f"HydroBASINS Pfafstetter level {level}; use HYBAS_ID / NEXT_DOWN "
                     "for the nesting topology.")

    desc = _describe_vector(dst)
    result = {
        "ok": True,
        "product": product,
        "region": region,
        "region_name": _REGION_NAMES[region],
        "source": "hydrosheds",
        "provider_host": _HYDROSHEDS_HOST,
        "bbox_wgs84": bbox,
        "feature_count": int(len(gdf)),
        "vector": desc,
        "attributes": [c for c in gdf.columns if c != gdf.geometry.name],
        "notes": notes,
        "citation": _CITATION,
    }
    if product == "basins":
        result["level"] = level
    return result


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        json.dump({"ok": False, "error": f"bad payload: {exc}"}, sys.stdout)
        return
    try:
        result = fetch_hydrography(payload)
    except Exception as exc:  # noqa: BLE001 - never crash silently across the MCP boundary
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                  "traceback": traceback.format_exc()}
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
