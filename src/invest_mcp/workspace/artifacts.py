"""Catalog the files an InVEST run produced so an LLM can reason about results
without opening rasters."""

from __future__ import annotations

from pathlib import Path

_KIND_BY_EXT = {
    ".tif": "raster", ".tiff": "raster", ".vrt": "raster",
    ".shp": "vector", ".gpkg": "vector", ".geojson": "vector", ".json": "json",
    ".csv": "table", ".xlsx": "table",
    ".html": "report", ".pdf": "report",
    ".txt": "log", ".log": "log",
    ".pickle": "intermediate", ".npy": "intermediate", ".npz": "intermediate",
}

_MAX_FILES = 400


def _kind(path: Path) -> str:
    return _KIND_BY_EXT.get(path.suffix.lower(), "other")


def catalog(workspace: str | Path) -> dict:
    ws = Path(workspace)
    if not ws.is_dir():
        return {"workspace": str(ws), "exists": False, "files": [], "summary": {}}

    files: list[dict] = []
    summary: dict[str, int] = {}
    truncated = False
    for p in sorted(ws.rglob("*")):
        if p.is_dir():
            continue
        if len(files) >= _MAX_FILES:
            truncated = True
            break
        try:
            size = p.stat().st_size
        except OSError:
            size = -1
        kind = _kind(p)
        summary[kind] = summary.get(kind, 0) + 1
        files.append(
            {
                "path": str(p.relative_to(ws)).replace("\\", "/"),
                "abspath": str(p),
                "kind": kind,
                "size_bytes": size,
            }
        )

    # InVEST writes its own detailed log at the workspace root; surface it.
    invest_logs = [f["path"] for f in files if Path(f["path"]).name.lower().startswith("invest-")]

    return {
        "workspace": str(ws),
        "exists": True,
        "truncated": truncated,
        "summary": summary,
        "invest_logs": invest_logs,
        "files": files,
    }
