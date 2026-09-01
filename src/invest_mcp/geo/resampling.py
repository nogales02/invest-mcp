"""Resampling: the decision and an efficient, RAM-bounded implementation.

Two separate concerns live here:

* **the choice** -- ``choose_resampling`` encodes the categorical-vs-continuous x
  upsample-vs-downsample matrix so the agent (and every prep op) picks a method
  on purpose instead of inheriting a silent ``nearest`` default. It is pure
  stdlib and importable from the ``.venv`` for its unit tests.
* **the work** -- ``warp_raster`` shells the pixels through ``gdal.Warp``, which
  streams tile-by-tile: memory stays bounded by ``warpMemoryLimit`` +
  ``GDAL_CACHEMAX`` no matter how large the raster, multithreaded, writing a
  tiled+compressed GeoTIFF with overviews. This replaces the "read the whole
  band into a numpy array" path the other prep ops used.

Only stdlib is imported at module load; ``osgeo`` / ``numpy`` are imported inside
the functions so a GDAL-less interpreter can still import the module.
"""

from __future__ import annotations

import math
import os

# A target pixel must be at least this many times larger (or smaller) than the
# source for the operation to count as aggregating (or refining). Inside the band
# it is treated as "same scale" -- a plain reprojection.
_SCALE_RATIO = 1.5

# canonical method name -> gdal.Warp resampleAlg string
_GDAL_ALG = {
    "nearest": "near", "near": "near",
    "bilinear": "bilinear", "linear": "bilinear",
    "cubic": "cubic",
    "cubicspline": "cubicspline", "cubic_spline": "cubicspline",
    "lanczos": "lanczos",
    "average": "average", "mean": "average",
    "rms": "rms",
    "mode": "mode", "majority": "mode",
    "max": "max", "min": "min",
    "med": "med", "median": "med",
    "q1": "q1", "q3": "q3",
    "sum": "sum",
}
# canonical (gdal.Warp) names
CONTINUOUS_METHODS = ("bilinear", "cubic", "cubicspline", "lanczos", "average",
                      "rms", "sum", "min", "max", "med", "q1", "q3")
CATEGORICAL_METHODS = ("near", "mode")
_INTERPOLATING = ("bilinear", "cubic", "cubicspline", "lanczos")

RESAMPLING_MATRIX = {
    # (data_kind, direction): method
    ("categorical", "up"): "nearest",
    ("categorical", "same"): "nearest",
    ("categorical", "down"): "mode",
    ("continuous", "up"): "bilinear",
    ("continuous", "same"): "bilinear",
    ("continuous", "down"): "average",
}


def normalize_method(name: str | None) -> str:
    """Canonical ``gdal.Warp`` resampleAlg string for a user/rasterio spelling."""
    key = str(name or "nearest").strip().lower()
    try:
        return _GDAL_ALG[key]
    except KeyError:
        raise ValueError(
            f"unknown resampling {name!r}; use one of: "
            + ", ".join(sorted(set(_GDAL_ALG)))
        ) from None


def scale_direction(src_res_m: float | None, dst_res_m: float | None) -> str:
    """``'up'`` (target finer), ``'down'`` (target coarser), ``'same'`` or
    ``'unknown'`` -- comparing metres-per-pixel, tolerant of missing values."""
    if not src_res_m or not dst_res_m or src_res_m <= 0 or dst_res_m <= 0:
        return "unknown"
    if dst_res_m >= src_res_m * _SCALE_RATIO:
        return "down"
    if dst_res_m <= src_res_m / _SCALE_RATIO:
        return "up"
    return "same"


def choose_resampling(*, categorical: bool,
                      src_res_m: float | None = None,
                      dst_res_m: float | None = None,
                      explicit: str | None = None) -> tuple[str, str | None]:
    """Return ``(method, note)``.

    ``explicit`` (anything other than ``None`` / ``""`` / ``"auto"``) is honoured
    verbatim but sanity-checked -- ``note`` warns when the forced method fights
    the data (e.g. ``bilinear`` on a categorical raster, ``nearest`` when
    downsampling classes). With no ``explicit`` method the matrix decides from
    ``categorical`` and the source/target metres-per-pixel.
    """
    kind = "categorical" if categorical else "continuous"
    direction = scale_direction(src_res_m, dst_res_m)

    forced = str(explicit or "").strip().lower()
    if forced and forced != "auto":
        method = normalize_method(forced)
        note = None
        if categorical and method not in CATEGORICAL_METHODS:
            note = (f"{method!r} interpolates values -- wrong for a categorical "
                    f"raster; use 'nearest' (up/same scale) or 'mode' (downsample).")
        elif categorical and method == "near" and direction == "down":
            note = ("downsampling a categorical raster with 'nearest' keeps one "
                    "sub-pixel and drops the majority class; 'mode' is usually right.")
        elif not categorical and method in _INTERPOLATING and direction == "down":
            note = (f"downsampling a continuous raster with {method!r} skips most "
                    "pixels; 'average' preserves the areal mean.")
        return method, note

    resolved_dir = direction if direction != "unknown" else "same"
    method = RESAMPLING_MATRIX[(kind, resolved_dir)]
    method = normalize_method(method)
    note = None
    if direction == "up" and src_res_m and dst_res_m:
        note = (f"target pixel ~{dst_res_m:g} m is finer than the source "
                f"~{src_res_m:g} m; resampling cannot add real detail.")
    elif direction == "unknown":
        note = "source/target resolution unknown; assumed a same-scale reprojection."
    return method, note


# ---------------------------------------------------------------------------
# GDAL-backed helpers (imports inside -- need the invest-geo env)
# ---------------------------------------------------------------------------
def _tune_gdal(gdal) -> None:
    """RAM-bounded, remote-friendly warper defaults."""
    gdal.SetConfigOption("GDAL_CACHEMAX",
                         str(os.environ.get("INVEST_MCP_GDAL_CACHE_MB", "512")))
    gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    gdal.SetConfigOption("VSI_CACHE", "TRUE")
    gdal.SetConfigOption("GDAL_HTTP_MULTIRANGE", "YES")
    gdal.SetConfigOption("GDAL_NUM_THREADS", "ALL_CPUS")


def _warp_mem_bytes() -> float:
    try:
        mb = float(os.environ.get("INVEST_MCP_WARP_MEM_MB", "256"))
    except ValueError:
        mb = 256.0
    return max(64.0, mb) * (1024 ** 2)


_INT_DTYPES_CACHE: set | None = None


def _int_gdal_types(gdal) -> set:
    global _INT_DTYPES_CACHE
    if _INT_DTYPES_CACHE is None:
        names = ("GDT_Byte", "GDT_Int8", "GDT_Int16", "GDT_UInt16",
                 "GDT_Int32", "GDT_UInt32", "GDT_Int64", "GDT_UInt64")
        _INT_DTYPES_CACHE = {getattr(gdal, n) for n in names if hasattr(gdal, n)}
    return _INT_DTYPES_CACHE


def _metres_per_pixel(px: float, py: float, srs) -> tuple[float, float, float]:
    """``(px_m, py_m, centre_lat)`` -- for geographic CRSs convert degrees with a
    cos(lat) factor; for projected assume the linear unit is already ~metres."""
    if srs is not None and srs.IsGeographic():
        return px * 111_320.0, py * 110_540.0, 0.0
    return abs(px), abs(py), 0.0


def raster_header(path: str) -> dict:
    """Cheap metadata read -- no pixels touched."""
    from osgeo import gdal, osr

    gdal.UseExceptions()
    ds = gdal.Open(str(path))
    if ds is None:
        raise RuntimeError(f"cannot open raster {path!r}")
    try:
        gt = ds.GetGeoTransform()
        w, h = ds.RasterXSize, ds.RasterYSize
        px, py = abs(gt[1]), abs(gt[5])
        wkt = ds.GetProjection()
        srs = osr.SpatialReference(wkt) if wkt else None
        if srs is not None:
            srs.AutoIdentifyEPSG()
        epsg = (srs.GetAuthorityCode(None) if srs else None)
        band = ds.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        dtype = gdal.GetDataTypeName(band.DataType)
        is_int = band.DataType in _int_gdal_types(gdal)
        minx, maxy = gt[0], gt[3]
        maxx, miny = minx + gt[1] * w, maxy + gt[5] * h
        cy = maxy + gt[5] * h / 2.0
        px_m, py_m, _ = _metres_per_pixel(px, py, srs)
        if srs is not None and srs.IsGeographic():
            px_m = px * 111_320.0 * max(0.05, math.cos(math.radians(cy)))
        return {
            "path": str(path),
            "kind": "raster",
            "crs": (f"EPSG:{epsg}" if epsg else (wkt or None)),
            "epsg": (int(epsg) if epsg else None),
            "is_geographic": bool(srs.IsGeographic()) if srs else None,
            "width": w, "height": h,
            "band_count": ds.RasterCount,
            "pixel_size": [px, py],
            "pixel_size_m": [round(px_m, 4), round(py_m, 4)],
            "bounds": [min(minx, maxx), min(miny, maxy),
                       max(minx, maxx), max(miny, maxy)],
            "nodata": (None if nodata is None else float(nodata)),
            "dtype": dtype,
            "is_integer": bool(is_int),
        }
    finally:
        ds = None


def classify_raster(path: str, *, sample_px: int = 512,
                    categorical_max_classes: int = 64) -> dict:
    """Guess whether a raster is categorical, from a **decimated** read (capped at
    ``sample_px`` per side -- a few hundred KB, not the whole band)."""
    import numpy as np
    from osgeo import gdal

    gdal.UseExceptions()
    hdr = raster_header(path)
    ds = gdal.Open(str(path))
    try:
        band = ds.GetRasterBand(1)
        bx = min(ds.RasterXSize, sample_px) or 1
        by = min(ds.RasterYSize, sample_px) or 1
        arr = band.ReadAsArray(buf_xsize=bx, buf_ysize=by)
    finally:
        ds = None
    distinct = None
    categorical = bool(hdr["is_integer"])
    if arr is not None:
        flat = np.asarray(arr).ravel()
        nd = hdr["nodata"]
        if nd is not None:
            flat = flat[flat != nd]
        flat = flat[np.isfinite(flat)] if np.issubdtype(flat.dtype, np.floating) else flat
        if flat.size:
            uniq = np.unique(flat)
            distinct = int(uniq.size)
            non_integer = bool(np.any(uniq != np.round(uniq)))
            categorical = (not non_integer) and distinct <= categorical_max_classes
        else:
            categorical = bool(hdr["is_integer"])
    return {
        **hdr,
        "categorical": categorical,
        "distinct_sample": distinct,
        "sample_shape": [by, bx],
    }


def _overview_levels(width: int, height: int) -> list[int]:
    levels, factor = [], 2
    while max(width, height) // factor >= 256 and factor <= 64:
        levels.append(factor)
        factor *= 2
    return levels


def warp_raster(src: str, dst: str, *,
                dst_srs: str | None = None,
                xres: float | None = None, yres: float | None = None,
                output_bounds: list[float] | None = None,
                output_bounds_srs: str | None = None,
                resample: str = "near",
                src_nodata: float | None = None,
                dst_nodata: float | None = None,
                cutline: str | None = None,
                cutline_srs: str | None = None,
                crop_to_cutline: bool = False,
                all_touched: bool = False,
                target_aligned_pixels: bool | None = None,
                build_overviews: bool = True) -> dict:
    """Stream ``src`` -> ``dst`` through ``gdal.Warp``. Returns the dst header.

    Memory is bounded by ``warpMemoryLimit`` + ``GDAL_CACHEMAX`` regardless of
    raster size; the write is a tiled DEFLATE GeoTIFF with overviews.
    """
    from pathlib import Path

    from osgeo import gdal

    gdal.UseExceptions()
    _tune_gdal(gdal)

    alg = normalize_method(resample)
    opts: dict = {
        "format": "GTiff",
        "multithread": True,
        "warpMemoryLimit": _warp_mem_bytes(),
        "resampleAlg": alg,
        "creationOptions": ["TILED=YES", "COMPRESS=DEFLATE",
                            "BIGTIFF=IF_SAFER", "NUM_THREADS=ALL_CPUS"],
    }
    if dst_srs:
        opts["dstSRS"] = dst_srs
    if xres and yres:
        opts["xRes"], opts["yRes"] = abs(float(xres)), abs(float(yres))
    if output_bounds is not None:
        opts["outputBounds"] = [float(v) for v in output_bounds]
        if output_bounds_srs:
            opts["outputBoundsSRS"] = output_bounds_srs
        # explicit bounds pin the extent -- aligning would nudge it off the grid
        opts["targetAlignedPixels"] = bool(target_aligned_pixels)
    elif xres and yres:
        opts["targetAlignedPixels"] = (True if target_aligned_pixels is None
                                       else bool(target_aligned_pixels))
    if src_nodata is not None:
        opts["srcNodata"] = src_nodata
    if dst_nodata is not None:
        opts["dstNodata"] = dst_nodata
    if cutline:
        opts["cutlineDSName"] = cutline
        if cutline_srs:
            opts["cutlineSRS"] = cutline_srs
        if crop_to_cutline:
            opts["cropToCutline"] = True
    warp_options = []
    if all_touched:
        warp_options.append("CUTLINE_ALL_TOUCHED=TRUE")
    if warp_options:
        opts["warpOptions"] = warp_options

    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    out = gdal.Warp(str(dst), str(src), options=gdal.WarpOptions(**opts))
    if out is None:
        raise RuntimeError("gdal.Warp produced no output dataset")
    try:
        if build_overviews:
            lv = _overview_levels(out.RasterXSize, out.RasterYSize)
            if lv:
                ov = "NEAREST" if alg in ("near", "mode") else "AVERAGE"
                out.BuildOverviews(ov, lv)
    finally:
        out = None
    return raster_header(dst)
