"""Climate-data fetch worker -- RUNS IN THE ``invest-geo`` CONDA ENV.

Invoked by :mod:`invest_mcp.geo.client` as::

    <invest-geo>/python.exe -m invest_mcp.geo.climate   < payload.json  > result.json

Downloads the climate inputs the water-yield models need, from **WorldClim v2.1**
(1970-2000 monthly climatology, no credentials) read straight over HTTPS with
GDAL ``/vsizip//vsicurl/``:

* ``variable="precipitation"`` -> WorldClim ``prec`` (mm/month). ``annual`` = sum
  of the 12 months.
* ``variable="eto"``           -> reference ET computed with **Hargreaves-Samani**
  from WorldClim ``tmin`` / ``tmax`` / ``tavg`` (extraterrestrial radiation from
  latitude + day-of-year, FAO-56). mm/month; ``annual`` = sum.

Payload (stdin, JSON)::

    {"variable": "precipitation" | "eto",
     "source": "worldclim",
     "period": "monthly" | "annual",
     "months": [1..12] | null,          # subset, monthly only; default all 12
     "resolution": "10m" | "5m" | "2.5m" | "30s",
     "dst_path": ".../precip_{month}.tif" (monthly, needs {month}) | ".../precip.tif" (annual),
     "bbox_wgs84": [...] | null, "aoi_path": "..." | null,
     "clip_to_aoi": true, "buffer_deg": 0.05,
     "target_crs": "..." | null, "target_resolution": [x,y] | null,
     "resampling": "bilinear", "keep_intermediate": false}

Output (stdout, JSON): ``{"ok", "variable", "source", "period", "units",
"method"?, "outputs": [{"month"?, "path", "raster": {...}}], "notes": [...]}``.

Only stdlib at import; numpy / rasterio load inside :func:`fetch_climate`.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import traceback
from pathlib import Path

os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("GDAL_HTTP_TIMEOUT", "60")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "3")
os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "2")

_WORLDCLIM_BASE = "https://geodata.ucdavis.edu/climate/worldclim/2_1/base"
_WC_RES = ("10m", "5m", "2.5m", "30s")
_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
# day-of-year of the middle of each month (non-leap)
_MID_MONTH_DOY = (16, 46, 75, 106, 136, 167, 197, 228, 259, 289, 320, 350)


def _wc_member_url(var: str, res: str, month: int) -> str:
    z = f"{_WORLDCLIM_BASE}/wc2.1_{res}_{var}.zip"
    return f"/vsizip//vsicurl/{z}/wc2.1_{res}_{var}_{month:02d}.tif"


# ---------------------------------------------------------------------------
# windowed read of one global WorldClim member, clipped to the bbox
# ---------------------------------------------------------------------------
def _read_window(url: str, bbox):
    import numpy as np
    import rasterio
    from rasterio.windows import from_bounds

    with rasterio.open(url) as ds:
        win = from_bounds(*bbox, transform=ds.transform)
        win = win.round_offsets().round_lengths()
        arr = ds.read(1, window=win, masked=True).astype("float64")
        transform = ds.window_transform(win)
        nodata = ds.nodata
        profile = ds.profile.copy()
    return np.ma.masked_invalid(arr), transform, nodata, profile


def _lat_row_centres(transform, height):
    """Pixel-centre latitude of each row, given the window transform."""
    import numpy as np

    top = transform.f
    ysize = transform.e  # negative
    return top + (np.arange(height) + 0.5) * ysize


# ---------------------------------------------------------------------------
# Hargreaves-Samani reference ET
# ---------------------------------------------------------------------------
def _ra_mm_per_day(lats_deg, month: int):
    """FAO-56 extraterrestrial radiation, MJ m-2 d-1 converted to mm d-1, as a
    1-D array over the given latitudes."""
    import numpy as np

    j = _MID_MONTH_DOY[month - 1]
    phi = np.deg2rad(np.asarray(lats_deg, dtype="float64"))
    dr = 1.0 + 0.033 * np.cos(2.0 * np.pi / 365.0 * j)
    dec = 0.409 * np.sin(2.0 * np.pi / 365.0 * j - 1.39)
    ws = np.arccos(np.clip(-np.tan(phi) * np.tan(dec), -1.0, 1.0))
    gsc = 0.0820  # MJ m-2 min-1
    ra = (24.0 * 60.0 / np.pi) * gsc * dr * (
        ws * np.sin(phi) * np.sin(dec) + np.cos(phi) * np.cos(dec) * np.sin(ws)
    )
    return 0.408 * ra  # -> mm d-1 equivalent


def _hargreaves_month(bbox, res: str, month: int):
    """Return ``(eto_mm_month_masked_array, transform, profile)`` for one month."""
    import numpy as np

    tmin, tr, _nd, prof = _read_window(_wc_member_url("tmin", res, month), bbox)
    tmax, _t2, _n2, _p2 = _read_window(_wc_member_url("tmax", res, month), bbox)
    tavg, _t3, _n3, _p3 = _read_window(_wc_member_url("tavg", res, month), bbox)

    ra = _ra_mm_per_day(_lat_row_centres(tr, tmin.shape[0]), month)[:, None]
    tdiff = np.ma.clip(tmax - tmin, 0.0, None)
    eto_day = 0.0023 * ra * (tavg + 17.8) * np.ma.sqrt(tdiff)
    eto = np.ma.clip(eto_day, 0.0, None) * _DAYS_IN_MONTH[month - 1]
    return eto, tr, prof


# ---------------------------------------------------------------------------
def _write_wgs84(arr, transform, profile, path: Path, nodata: float):
    import numpy as np
    import rasterio

    for k in ("blockxsize", "blockysize", "tiled", "interleave", "photometric"):
        profile.pop(k, None)
    profile.update(driver="GTiff", count=1, dtype="float32", crs="EPSG:4326",
                   transform=transform, height=arr.shape[0], width=arr.shape[1],
                   nodata=nodata, compress="deflate")
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as out:
        out.write(np.ma.filled(arr, nodata).astype("float32"), 1)


def _months_for(payload: dict) -> list[int]:
    if payload.get("period") == "annual":
        return list(range(1, 13))
    ms = payload.get("months") or list(range(1, 13))
    ms = sorted({int(m) for m in ms})
    if any(m < 1 or m > 12 for m in ms):
        raise ValueError("months must be in 1..12")
    return ms


def _month_array(variable: str, bbox, res: str, month: int):
    if variable == "precipitation":
        arr, tr, nd, prof = _read_window(_wc_member_url("prec", res, month), bbox)
        return arr, tr, prof
    if variable == "eto":
        return _hargreaves_month(bbox, res, month)
    raise ValueError(f"unknown variable {variable!r}; use 'precipitation' or 'eto'")


def fetch_climate(payload: dict) -> dict:
    import numpy as np

    from invest_mcp.geo.fetch import _resolve_bbox, reproject_clip_describe

    variable = payload["variable"]
    source = (payload.get("source") or "worldclim").lower()
    if source != "worldclim":
        raise ValueError(f"unknown climate source {source!r}; only 'worldclim' is wired up")
    res = payload.get("resolution") or "10m"
    if res not in _WC_RES:
        raise ValueError(f"resolution must be one of {_WC_RES}")
    period = payload.get("period") or "monthly"
    if period not in ("monthly", "annual"):
        raise ValueError("period must be 'monthly' or 'annual'")

    dst_tmpl = str(payload["dst_path"])
    if period == "monthly" and "{month}" not in dst_tmpl:
        raise ValueError("monthly period needs '{month}' in dst_path, "
                         "e.g. .../precip_{month}.tif")

    bbox, _aoi = _resolve_bbox(payload)
    months = _months_for(payload)
    nodata = -9999.0

    root = Path(dst_tmpl.replace("{month}", "annual" if period == "annual" else "1"))
    work = root.parent / f"{root.stem}_fetch"
    work.mkdir(parents=True, exist_ok=True)

    common = dict(
        target_crs=payload.get("target_crs"),
        target_resolution=payload.get("target_resolution"),
        resampling=payload.get("resampling"), default_resampling="bilinear",
        clip_aoi_path=(payload["aoi_path"] if payload.get("clip_to_aoi")
                       and payload.get("aoi_path") else None),
    )

    outputs: list[dict] = []
    if period == "annual":
        acc = None
        tr = prof = None
        for m in months:
            arr, tr, prof = _month_array(variable, bbox, res, m)
            acc = arr if acc is None else acc + arr
        wgs = work / "annual_wgs84.tif"
        _write_wgs84(acc, tr, prof, wgs, nodata)
        dst = Path(dst_tmpl) if "{month}" not in dst_tmpl else Path(
            dst_tmpl.replace("{month}", "annual"))
        desc = reproject_clip_describe(wgs, dst, work, **common)
        outputs.append({"path": str(dst), "raster": desc})
    else:
        for m in months:
            arr, tr, prof = _month_array(variable, bbox, res, m)
            wgs = work / f"m{m:02d}_wgs84.tif"
            _write_wgs84(arr, tr, prof, wgs, nodata)
            dst = Path(dst_tmpl.replace("{month}", str(m)))
            desc = reproject_clip_describe(wgs, dst, work, **common)
            outputs.append({"month": m, "path": str(dst), "raster": desc})

    if not payload.get("keep_intermediate"):
        shutil.rmtree(work, ignore_errors=True)

    result = {
        "ok": True,
        "variable": variable,
        "source": "worldclim",
        "resolution": res,
        "period": period,
        "units": "mm" if period == "monthly" else "mm (annual total)",
        "provider_host": "https://geodata.ucdavis.edu",
        "bbox_wgs84": bbox,
        "outputs": outputs,
        "notes": [],
    }
    if variable == "eto":
        result["method"] = ("Hargreaves-Samani ETo from WorldClim tmin/tmax/tavg; "
                            "extraterrestrial radiation from latitude + day-of-year (FAO-56)")
        result["notes"].append(
            "ETo is modelled (Hargreaves), not measured -- fine for InVEST water "
            "yield, but swap in a local ETo grid if you have one.")
    return result


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        json.dump({"ok": False, "error": f"bad payload: {exc}"}, sys.stdout)
        return
    try:
        result = fetch_climate(payload)
    except Exception as exc:  # noqa: BLE001 - never crash silently across the MCP boundary
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                  "traceback": traceback.format_exc()}
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
