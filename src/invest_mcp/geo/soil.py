"""Soil-data fetch worker -- RUNS IN THE ``invest-geo`` CONDA ENV.

Invoked by :mod:`invest_mcp.geo.client` as::

    <invest-geo>/python.exe -m invest_mcp.geo.soil   < payload.json  > result.json

Downloads the soil inputs the InVEST models need from **SoilGrids 2.0** (ISRIC,
250 m, no credentials). The global property grids are published as VRTs in the
Interrupted Goode Homolosine projection; we read them straight over HTTPS with
GDAL ``/vsicurl/`` wrapped in a ``WarpedVRT`` to EPSG:4326 and windowed to the
AOI bbox.

``variable``:

* ``"texture"``               -> sand / silt / clay fraction (percent by weight),
  three rasters. ``dst_path`` MUST contain ``{fraction}``.
* ``"hydrologic_soil_group"`` -> HSG 1..4 (A..D) derived from the USDA texture
  class. One raster, uint8, nodata 0. (Texture-only approximation -- Ksat,
  depth-to-bedrock and water-table refinements are ignored.)
* ``"usle_k"``                -> soil erodibility K via the Williams / EPIC (1995)
  equation from sand/silt/clay/SOC, converted to SI units
  (t.ha.h.ha-1.MJ-1.mm-1). One raster, float32.

Payload (stdin, JSON)::

    {"variable": "texture" | "hydrologic_soil_group" | "usle_k",
     "source": "soilgrids",
     "depth": "0-5cm" | "5-15cm" | "15-30cm" | "30-60cm" | "60-100cm" | "100-200cm",
     "stat": "mean" | "Q0.05" | "Q0.5" | "Q0.95",
     "dst_path": ".../soil_{fraction}.tif" (texture) | ".../hsg.tif",
     "bbox_wgs84": [...] | null, "aoi_path": "..." | null,
     "clip_to_aoi": true, "buffer_deg": 0.05,
     "target_crs": "..." | null, "target_resolution": [x, y] | null,
     "resampling": "bilinear" | null, "keep_intermediate": false}

Output (stdout, JSON): ``{"ok", "variable", "source", "depth", "stat", "units",
"provider_host", "bbox_wgs84", "outputs": [{"fraction"?, "path", "raster": {...}}],
"notes": [...], "group_legend"?}``.

Only stdlib is imported at module load; numpy / rasterio load inside the
functions (same rule as the other geo/ workers).
"""

from __future__ import annotations

import json
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

_SOILGRIDS_BASE = "https://files.isric.org/soilgrids/latest/data"
_SOILGRIDS_HOST = "https://files.isric.org"
_SG_DEPTHS = ("0-5cm", "5-15cm", "15-30cm", "30-60cm", "60-100cm", "100-200cm")
_SG_STATS = ("mean", "Q0.05", "Q0.5", "Q0.95")
_SG_NODATA_IN = -32768
_OUT_NODATA = -9999.0
_VARIABLES = ("texture", "hydrologic_soil_group", "usle_k")
_FRACTIONS = ("sand", "silt", "clay")

# SoilGrids 2.0 stores clay/sand/silt as g/kg and soc as dg/kg; dividing by these
# factors gives percent by weight (organic carbon percent for soc).
_TO_PERCENT = {"sand": 10.0, "silt": 10.0, "clay": 10.0, "soc": 100.0}

# USDA 12-class texture triangle -> integer code, and code -> hydrologic soil
# group (1=A .. 4=D). The HSG mapping is the widely used texture-only rule
# (deep, well-drained soils, no shallow water table).
_TEXTURE_CLASSES = (
    "sand", "loamy sand", "sandy loam", "loam", "silt loam", "silt",
    "sandy clay loam", "clay loam", "silty clay loam", "sandy clay",
    "silty clay", "clay",
)
_HSG_BY_TEXCODE = {1: 1, 2: 1, 3: 1, 4: 2, 5: 2, 6: 2, 7: 3,
                   8: 4, 9: 4, 10: 4, 11: 4, 12: 4}


def _sg_vrt_url(prop: str, depth: str, stat: str) -> str:
    return f"/vsicurl/{_SOILGRIDS_BASE}/{prop}/{prop}_{depth}_{stat}.vrt"


# ---------------------------------------------------------------------------
# windowed read of one SoilGrids property, warped to EPSG:4326 and clipped to
# the bbox; returned in physical units (percent).
# ---------------------------------------------------------------------------
def _read_prop(prop: str, depth: str, stat: str, bbox):
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT
    from rasterio.windows import from_bounds

    url = _sg_vrt_url(prop, depth, stat)
    with rasterio.open(url) as src:
        with WarpedVRT(src, crs="EPSG:4326", resampling=Resampling.bilinear) as vrt:
            win = from_bounds(*bbox, transform=vrt.transform)
            win = win.round_offsets().round_lengths()
            arr = vrt.read(1, window=win, masked=True).astype("float64")
            transform = vrt.window_transform(win)
    arr = np.ma.masked_equal(np.ma.masked_invalid(arr), _SG_NODATA_IN)
    return arr / _TO_PERCENT[prop], transform


# ---------------------------------------------------------------------------
def _write_wgs84(arr, transform, path: Path, nodata, dtype: str) -> None:
    import numpy as np
    import rasterio

    profile = dict(driver="GTiff", count=1, dtype=dtype, crs="EPSG:4326",
                   transform=transform, height=arr.shape[0], width=arr.shape[1],
                   nodata=nodata, compress="deflate")
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as out:
        out.write(np.ma.filled(arr, nodata).astype(dtype), 1)


# ---------------------------------------------------------------------------
# USDA texture class + hydrologic soil group
# ---------------------------------------------------------------------------
def _texture_class_code(sand, silt, clay):
    """USDA 12-class texture triangle -> int codes 1..12; 0 where any fraction is
    masked / non-finite. ``sand``/``silt``/``clay`` are percent-by-weight
    arrays; they are renormalised to sum to 100 first so SoilGrids rounding
    does not push points off the triangle."""
    import numpy as np

    S = np.ma.filled(sand, np.nan).astype("float64")
    Si = np.ma.filled(silt, np.nan).astype("float64")
    C = np.ma.filled(clay, np.nan).astype("float64")
    tot = S + Si + C
    with np.errstate(invalid="ignore", divide="ignore"):
        ok = np.isfinite(tot) & (tot > 0)
        S = np.where(ok, S / tot * 100.0, np.nan)
        C = np.where(ok, C / tot * 100.0, np.nan)
        Si = 100.0 - S - C

    conds = [
        (S >= 85) & (Si + 1.5 * C < 15),                                          # 1 sand
        (S >= 70) & (S < 90) & (Si + 1.5 * C >= 15) & (Si + 2 * C < 30),          # 2 loamy sand
        (((C >= 7) & (C < 20) & (S > 52) & (Si + 2 * C >= 30))
         | ((C < 7) & (Si < 50) & (S > 43))),                                     # 3 sandy loam
        (C >= 7) & (C < 27) & (Si >= 28) & (Si < 50) & (S <= 52),                 # 4 loam
        (((Si >= 50) & (C >= 12) & (C < 27))
         | ((Si >= 50) & (Si < 80) & (C < 12))),                                  # 5 silt loam
        (Si >= 80) & (C < 12),                                                    # 6 silt
        (C >= 20) & (C < 35) & (Si < 28) & (S > 45),                              # 7 sandy clay loam
        (C >= 27) & (C < 40) & (S > 20) & (S <= 45),                              # 8 clay loam
        (C >= 27) & (C < 40) & (S <= 20),                                         # 9 silty clay loam
        (C >= 35) & (S > 45),                                                     # 10 sandy clay
        (C >= 40) & (Si >= 40),                                                   # 11 silty clay
        (C >= 40) & (S <= 45) & (Si < 40),                                        # 12 clay
    ]
    codes = np.select(conds, list(range(1, 13)), default=0)
    codes = np.where(np.isfinite(S) & np.isfinite(C), codes, 0)
    return codes.astype("int32")


def _hsg_from_texture(codes):
    import numpy as np

    out = np.zeros(codes.shape, dtype="uint8")
    for tc, group in _HSG_BY_TEXCODE.items():
        out[codes == tc] = group
    return out


# ---------------------------------------------------------------------------
# soil erodibility K -- Williams / EPIC (1995)
# ---------------------------------------------------------------------------
def _usle_k_epic(sand, silt, clay, oc):
    """Return a masked K array in SI units (t.ha.h.ha-1.MJ-1.mm-1). All inputs
    are percent by weight; ``oc`` is organic carbon percent."""
    import numpy as np

    S = np.ma.filled(sand, np.nan).astype("float64")
    Si = np.ma.filled(silt, np.nan).astype("float64")
    C = np.ma.filled(clay, np.nan).astype("float64")
    OC = np.clip(np.ma.filled(oc, np.nan).astype("float64"), 0.0, None)
    SN1 = 1.0 - S / 100.0

    with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
        f_csand = 0.2 + 0.3 * np.exp(-0.0256 * S * (1.0 - Si / 100.0))
        denom = C + Si
        f_clsi = np.where(denom > 0, (Si / np.where(denom > 0, denom, 1.0)) ** 0.3, 0.0)
        f_orgc = 1.0 - (0.25 * OC) / (OC + np.exp(3.72 - 2.95 * OC))
        f_hisand = 1.0 - (0.7 * SN1) / (SN1 + np.exp(-5.51 + 22.9 * SN1))
        k_us = f_csand * f_clsi * f_orgc * f_hisand
        k_si = k_us * 0.1317                       # US customary -> SI

    k_si = np.where(np.isfinite(k_si), np.clip(k_si, 0.0, 0.07), np.nan)
    return np.ma.masked_invalid(k_si)


# ---------------------------------------------------------------------------
def fetch_soil(payload: dict) -> dict:
    from invest_mcp.geo.fetch import _resolve_bbox, reproject_clip_describe

    variable = payload["variable"]
    if variable not in _VARIABLES:
        raise ValueError(f"variable must be one of {_VARIABLES}")
    source = (payload.get("source") or "soilgrids").lower()
    if source not in ("soilgrids", "soilgrids2", "isric"):
        raise ValueError(f"unknown soil source {source!r}; only 'soilgrids' is wired up")
    depth = payload.get("depth") or "0-5cm"
    if depth not in _SG_DEPTHS:
        raise ValueError(f"depth must be one of {_SG_DEPTHS}")
    stat = payload.get("stat") or "mean"
    if stat not in _SG_STATS:
        raise ValueError(f"stat must be one of {_SG_STATS}")

    dst_tmpl = str(payload["dst_path"])
    if variable == "texture" and "{fraction}" not in dst_tmpl:
        raise ValueError("variable='texture' needs '{fraction}' in dst_path, "
                         "e.g. .../soil_{fraction}.tif")

    bbox, _aoi = _resolve_bbox(payload)

    root = Path(dst_tmpl.replace("{fraction}", "sand"))
    work = root.parent / f"{root.stem}_fetch"
    work.mkdir(parents=True, exist_ok=True)

    common = dict(
        target_crs=payload.get("target_crs"),
        target_resolution=payload.get("target_resolution"),
        resampling=payload.get("resampling"),
        default_resampling=("nearest" if variable == "hydrologic_soil_group"
                            else "bilinear"),
        clip_aoi_path=(payload["aoi_path"] if payload.get("clip_to_aoi")
                       and payload.get("aoi_path") else None),
    )

    sand, tr = _read_prop("sand", depth, stat, bbox)
    silt, _ = _read_prop("silt", depth, stat, bbox)
    clay, _ = _read_prop("clay", depth, stat, bbox)

    outputs: list[dict] = []
    notes: list[str] = []
    group_legend = None

    if variable == "texture":
        for frac, arr in (("sand", sand), ("silt", silt), ("clay", clay)):
            wgs = work / f"{frac}_wgs84.tif"
            _write_wgs84(arr, tr, wgs, _OUT_NODATA, "float32")
            dst = Path(dst_tmpl.replace("{fraction}", frac))
            desc = reproject_clip_describe(wgs, dst, work, **common)
            outputs.append({"fraction": frac, "path": str(dst), "raster": desc})
        units = "percent by weight"
    elif variable == "hydrologic_soil_group":
        hsg = _hsg_from_texture(_texture_class_code(sand, silt, clay))
        wgs = work / "hsg_wgs84.tif"
        _write_wgs84(hsg, tr, wgs, 0, "uint8")
        dst = Path(dst_tmpl)
        desc = reproject_clip_describe(wgs, dst, work, **common)
        outputs.append({"path": str(dst), "raster": desc})
        units = "hydrologic soil group (1=A, 2=B, 3=C, 4=D)"
        group_legend = {"1": "A", "2": "B", "3": "C", "4": "D"}
        notes.append(
            "HSG derived from the USDA texture class only -- saturated hydraulic "
            "conductivity, depth to a restrictive layer and water-table depth are "
            "not considered. For a rigorous layer use HYSOGs250m or HiHydroSoil.")
    else:  # usle_k
        oc, _ = _read_prop("soc", depth, stat, bbox)
        k = _usle_k_epic(sand, silt, clay, oc)
        wgs = work / "usle_k_wgs84.tif"
        _write_wgs84(k, tr, wgs, _OUT_NODATA, "float32")
        dst = Path(dst_tmpl)
        desc = reproject_clip_describe(wgs, dst, work, **common)
        outputs.append({"path": str(dst), "raster": desc})
        units = "t.ha.h.ha-1.MJ-1.mm-1 (SI)"
        notes.append(
            "Erodibility K estimated with the Williams / EPIC (1995) equation "
            "from sand/silt/clay/SOC and converted to SI (x0.1317). It is a "
            "pedotransfer estimate -- prefer a regional K grid or lab-measured "
            "values where you have them.")

    if not payload.get("keep_intermediate"):
        shutil.rmtree(work, ignore_errors=True)

    result = {
        "ok": True,
        "variable": variable,
        "source": "soilgrids",
        "depth": depth,
        "stat": stat,
        "units": units,
        "provider_host": _SOILGRIDS_HOST,
        "bbox_wgs84": bbox,
        "outputs": outputs,
        "notes": notes,
    }
    if group_legend is not None:
        result["group_legend"] = group_legend
    return result


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        json.dump({"ok": False, "error": f"bad payload: {exc}"}, sys.stdout)
        return
    try:
        result = fetch_soil(payload)
    except Exception as exc:  # noqa: BLE001 - never crash silently across the MCP boundary
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                  "traceback": traceback.format_exc()}
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
