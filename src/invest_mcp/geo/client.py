"""Server-side entry to the geospatial preflight.

Runs in the MCP server process (plain Python, no GDAL). Builds the payload from a
model spec + args and shells out to the ``invest-geo`` conda env worker.
"""

from __future__ import annotations

import json
import subprocess

from invest_mcp.config import Settings, geo_subprocess_env
from invest_mcp.models.spec_translate import spatial_arg_specs

_TIMEOUT_S = 300


def build_payload(model_spec: dict, args: dict) -> dict:
    specs = spatial_arg_specs(model_spec)
    spatial_inputs = []
    for name, meta in specs.items():
        value = args.get(name)
        if value in (None, "", []):
            continue
        spatial_inputs.append({"arg": name, "path": str(value), **meta})
    return {
        "spatial_inputs": spatial_inputs,
        "different_projections_ok": bool(model_spec.get("different_projections_ok")),
        "validate_spatial_overlap": bool(model_spec.get("validate_spatial_overlap")),
    }


def run_preflight(model_spec: dict, args: dict, settings: Settings) -> dict:
    """Return the worker's result dict. Raises RuntimeError only if the geo env
    itself is missing (so callers can treat that as 'skipped')."""
    payload = build_payload(model_spec, args)
    if not payload["spatial_inputs"]:
        return {"ok": True, "checks": [], "layers": {},
                "note": "model has no spatial file inputs set"}

    geo_python = settings.resolved_geo_python  # RuntimeError if absent
    try:
        proc = subprocess.run(
            [str(geo_python), "-m", "invest_mcp.geo.preflight"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=geo_subprocess_env(geo_python),
            timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "checks": [
            {"level": "error", "code": "preflight_timeout",
             "message": f"Preflight exceeded {_TIMEOUT_S}s.", "args": []}
        ], "layers": {}}

    if proc.returncode != 0 and not proc.stdout.strip():
        return {"ok": False, "checks": [
            {"level": "error", "code": "preflight_worker_error",
             "message": (proc.stderr or "worker exited non-zero").strip()[-2000:], "args": []}
        ], "layers": {}}

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "checks": [
            {"level": "error", "code": "preflight_bad_output",
             "message": f"worker returned non-JSON. stdout={proc.stdout[:800]!r} "
                        f"stderr={proc.stderr[:800]!r}", "args": []}
        ], "layers": {}}
