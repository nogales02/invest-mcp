"""Geospatial preflight worker -- RUNS IN THE ``invest-geo`` CONDA ENV.

Invoked by :mod:`invest_mcp.geo.client` as::

    <invest-geo>/python.exe -m invest_mcp.geo.preflight   < payload.json  > result.json

Input payload (stdin, JSON)::

    {
      "spatial_inputs": [
        {"arg": "lulc_bas_path", "path": "C:/.../lulc.tif", "kind": "raster",
         "projected_required": true, "projection_units": "m"},
        ...
      ],
      "different_projections_ok": false,
      "validate_spatial_overlap": true
    }

Output (stdout, JSON)::

    {"ok": bool, "checks": [{"level","code","message","args"}], "layers": {arg: {...}}}

``ok`` is False iff at least one check has level ``error``. Only stdlib is imported
at module load; GDAL-backed libs are imported lazily so ``--help``-style probes
don't blow up on a broken env.
"""

from __future__ import annotations

import json
import sys
import traceback

_METERS = {"metre", "meter", "meters", "metres", "m"}
_PIXEL_RATIO_WARN = 5.0


# ---------------------------------------------------------------------------
# metadata readers
# ---------------------------------------------------------------------------
def _read_raster(path: str) -> dict:
    import rasterio

    with rasterio.open(path) as ds:
        crs = ds.crs
        a, e = ds.transform.a, ds.transform.e
        return {
            "kind": "raster",
            "crs": crs.to_wkt() if crs else None,
            "width": ds.width,
            "height": ds.height,
            "pixel_size": [abs(a), abs(e)],
            "bounds": [ds.bounds.left, ds.bounds.bottom, ds.bounds.right, ds.bounds.top],
            "nodata": None if ds.nodata is None else float(ds.nodata),
            "dtype": ds.dtypes[0],
            "band_count": ds.count,
        }


def _read_vector(path: str) -> dict:
    import pyogrio

    info = pyogrio.read_info(path)
    crs = info.get("crs")
    bounds = info.get("total_bounds")
    if bounds is not None:
        bounds = [float(x) for x in bounds]
    return {
        "kind": "vector",
        "crs": crs if crs else None,
        "feature_count": int(info.get("features") or 0),
        "geometry_type": info.get("geometry_type"),
        "bounds": bounds,
        "pixel_size": None,
        "nodata": None,
    }


def _read_layer(entry: dict) -> dict:
    kind = entry.get("kind", "raster")
    path = entry["path"]
    if kind == "vector":
        return _read_vector(path)
    if kind == "raster":
        return _read_raster(path)
    # raster_or_vector: try raster, fall back to vector
    try:
        return _read_raster(path)
    except Exception:
        return _read_vector(path)


# ---------------------------------------------------------------------------
# CRS analysis
# ---------------------------------------------------------------------------
def _crs_info(crs_repr: str | None) -> dict | None:
    if not crs_repr:
        return None
    from pyproj import CRS

    try:
        c = CRS.from_user_input(crs_repr)
    except Exception as exc:  # noqa: BLE001
        return {"parsed": False, "error": str(exc)}
    unit = None
    try:
        unit = c.axis_info[0].unit_name
    except Exception:
        pass
    return {
        "parsed": True,
        "name": c.name,
        "epsg": c.to_epsg(),
        "is_projected": bool(c.is_projected),
        "is_geographic": bool(c.is_geographic),
        "unit_name": unit,
    }


def _crs_key(info: dict | None, raw: str | None) -> str | None:
    if not info or not info.get("parsed"):
        return None
    return f"EPSG:{info['epsg']}" if info.get("epsg") else (raw or "unknown")


def _bounds_to_wgs84_box(bounds, crs_repr):
    from pyproj import CRS, Transformer
    from shapely.geometry import box

    c = CRS.from_user_input(crs_repr)
    if c.to_epsg() == 4326:
        return box(*bounds)
    t = Transformer.from_crs(c, CRS.from_epsg(4326), always_xy=True)
    left, bottom, right, top = bounds
    xs: list[float] = []
    ys: list[float] = []
    steps = 15
    for i in range(steps + 1):
        fx = left + (right - left) * i / steps
        fy = bottom + (top - bottom) * i / steps
        for X, Y in (
            t.transform(fx, bottom),
            t.transform(fx, top),
            t.transform(left, fy),
            t.transform(right, fy),
        ):
            if X == float("inf") or Y == float("inf"):
                continue
            xs.append(X)
            ys.append(Y)
    if not xs:
        raise ValueError("could not project bounds to WGS84")
    return box(min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------
def run_checks(payload: dict) -> dict:
    inputs = payload.get("spatial_inputs", [])
    diff_ok = bool(payload.get("different_projections_ok"))
    check_overlap = bool(payload.get("validate_spatial_overlap"))

    checks: list[dict] = []
    layers: dict[str, dict] = {}

    def add(level, code, message, args):
        checks.append({"level": level, "code": code, "message": message, "args": list(args)})

    # --- per-layer -----------------------------------------------------
    for entry in inputs:
        arg = entry["arg"]
        try:
            meta = _read_layer(entry)
        except Exception as exc:  # noqa: BLE001
            add("error", "read_error", f"Could not open {arg} ({entry['path']}): {exc}", [arg])
            continue
        meta["path"] = entry["path"]
        meta["crs_info"] = _crs_info(meta.get("crs"))
        meta["crs_key"] = _crs_key(meta["crs_info"], meta.get("crs"))
        layers[arg] = meta

        ci = meta["crs_info"]
        if ci is None:
            add("error", "crs_missing",
                f"{arg} has no coordinate reference system defined.", [arg])
        elif not ci.get("parsed"):
            add("error", "crs_unparseable",
                f"{arg} has a CRS that could not be interpreted: {ci.get('error')}", [arg])
        else:
            if entry.get("projected_required") and not ci["is_projected"]:
                add("error", "crs_not_projected",
                    f"{arg} must use a projected CRS (it is "
                    f"{'geographic' if ci['is_geographic'] else 'unprojected'}: {ci['name']}).",
                    [arg])
            if entry.get("projected_required") and ci.get("unit_name") \
                    and ci["unit_name"].lower() not in _METERS:
                add("warning", "crs_units_not_meters",
                    f"{arg} CRS linear unit is '{ci['unit_name']}', InVEST expects metres.",
                    [arg])

        if meta.get("kind") == "raster" and meta.get("nodata") is None:
            add("warning", "nodata_undefined",
                f"{arg} raster has no nodata value set; InVEST may misread masked areas.",
                [arg])
        if meta.get("kind") == "vector" and meta.get("feature_count", 1) == 0:
            add("error", "empty_vector", f"{arg} vector has zero features.", [arg])

    parsed = {
        a: m for a, m in layers.items()
        if m.get("crs_info") and m["crs_info"].get("parsed") and m.get("bounds")
    }

    # --- CRS consistency --------------------------------------------------
    keys = {a: m["crs_key"] for a, m in parsed.items()}
    distinct = sorted(set(keys.values()))
    if len(distinct) > 1:
        level = "warning" if diff_ok else "error"
        note = "" if diff_ok else " This model does not reproject inputs automatically."
        add(level, "crs_mismatch",
            "Spatial inputs use different coordinate systems: "
            + "; ".join(f"{a}={k}" for a, k in keys.items()) + "." + note,
            list(keys))

    # --- spatial overlap ------------------------------------------------
    if check_overlap and len(parsed) >= 2:
        boxes: dict[str, object] = {}
        for a, m in parsed.items():
            try:
                boxes[a] = _bounds_to_wgs84_box(m["bounds"], m["crs"])
            except Exception as exc:  # noqa: BLE001
                add("warning", "overlap_check_skipped",
                    f"Could not test overlap for {a}: {exc}", [a])
        names = list(boxes)
        disjoint_pairs = [
            (names[i], names[j])
            for i in range(len(names)) for j in range(i + 1, len(names))
            if not boxes[names[i]].intersects(boxes[names[j]])
        ]
        if disjoint_pairs:
            add("error", "no_spatial_overlap",
                "These spatial inputs do not overlap: "
                + "; ".join(f"{x} vs {y}" for x, y in disjoint_pairs) + ".",
                sorted({n for p in disjoint_pairs for n in p}))
        elif len(boxes) >= 2:
            inter = None
            for b in boxes.values():
                inter = b if inter is None else inter.intersection(b)
            if inter is not None and inter.is_empty:
                add("warning", "partial_spatial_overlap",
                    "All inputs overlap pairwise but share no common area; "
                    "the analysis extent may be very small.",
                    list(boxes))

    # --- pixel size sanity --------------------------------------------------
    px = [v for m in layers.values() for v in (m.get("pixel_size") or []) if v and v > 0]
    if len(px) >= 2 and max(px) / min(px) > _PIXEL_RATIO_WARN:
        add("warning", "pixel_size_mismatch",
            f"Raster pixel sizes vary widely ({min(px):g} to {max(px):g}); "
            "InVEST will resample, but check the inputs are the intended resolution.",
            [a for a, m in layers.items() if m.get("pixel_size")])

    return {
        "ok": not any(c["level"] == "error" for c in checks),
        "checks": checks,
        "layers": layers,
    }


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        json.dump({"ok": False, "error": f"bad payload: {exc}"}, sys.stdout)
        return
    try:
        result = run_checks(payload)
    except Exception:  # noqa: BLE001 - never crash silently
        result = {"ok": False, "error": "preflight worker crashed",
                  "traceback": traceback.format_exc()}
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
