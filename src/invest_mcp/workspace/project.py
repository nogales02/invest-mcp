"""The InVEST *project* folder convention -- scaffold + manifest I/O.

A "project" bundles everything for one case study: its area of interest, the CRS
everything will be worked in, the raw (immutable) and processed (derived) data,
the biophysical/lookup tables, the datastacks handed to InVEST, and the
per-run job workspaces. See ``CLAUDE.md`` section 6.

Pure stdlib -- no GDAL -- so this runs in the server process, not the
``invest-geo`` env. The deterministic raster/vector routines that fill the tree
(:mod:`invest_mcp.geo.prep`) run in that env instead.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

PROJECT_FILE = "project.json"
SCHEMA_VERSION = 1

# raw/ is immutable (what you downloaded); processed/ is everything derived from
# it -- reprojected, clipped, aligned. That split is what makes a run traceable.
SUBDIRS = (
    "data/raw",
    "data/processed",
    "tables",
    "datastacks",
    "jobs",
    "logs",
)


def _relpath_if_inside(path: str, root: Path) -> str | None:
    try:
        return str(Path(path).resolve().relative_to(root)).replace("\\", "/")
    except (ValueError, OSError):
        return None


def scaffold(
    root: Path,
    *,
    name: str,
    target_crs: str | None,
    aoi_path: str | None,
    overwrite: bool = False,
) -> dict:
    """Create :data:`SUBDIRS` under ``root`` and write ``project.json``.

    Idempotent: missing directories are added, and an existing manifest is kept
    untouched unless ``overwrite`` is set.
    """
    root = Path(root)
    created: list[str] = []
    for sub in SUBDIRS:
        d = root / sub
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(sub)

    manifest_path = root / PROJECT_FILE
    if manifest_path.is_file() and not overwrite:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"{manifest_path} exists but is unreadable: {exc}") from exc
        return {
            "project_file": str(manifest_path),
            "created_dirs": created,
            "manifest": manifest,
            "manifest_existed": True,
        }

    aoi_entry = None
    if aoi_path:
        aoi_entry = {"path": aoi_path, "relpath": _relpath_if_inside(aoi_path, root)}

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target_crs": target_crs,
        "aoi": aoi_entry,
        "subdirs": list(SUBDIRS),
        # filled later by the data-fetch / prep routines: one entry per dataset
        # ({"id","role","path","crs","source",...}).
        "datasets": [],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {
        "project_file": str(manifest_path),
        "created_dirs": created,
        "manifest": manifest,
        "manifest_existed": False,
    }


def load(root: Path) -> dict | None:
    """Return the parsed ``project.json`` under ``root``, or ``None``."""
    p = Path(root) / PROJECT_FILE
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
