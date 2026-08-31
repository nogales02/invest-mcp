"""Read / write InVEST **datastacks** (parameter sets) -- the ``.invest.json``
format the InVEST Workbench round-trips.

This is the declared integration point between this server and the Workbench
(see ``CLAUDE.md`` section 1): both drive the same ``natcap.invest`` core, and a
parameter set is how a case study moves between them.

A parameter set is a plain JSON object::

    {"args": { ... the model's args dict ... },
     "model_id": "carbon",
     "invest_version": "3.20.1"}

The bundled ``invest.exe`` (3.20.1) reads the ``model_id`` key; older files wrote
``"model_name": "natcap.invest.carbon"`` instead, which we tolerate on read.
InVEST also supports *archive* datastacks (``.invest.tar.gz`` bundling the data)
and CSV-embedded parameter sets -- those are out of scope here; this module only
handles the JSON parameter set.

Pure stdlib -- runs in the server process, no GDAL, no ``invest`` subprocess.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_MODEL_NAME_PREFIX = "natcap.invest."


def model_id_from_raw(raw: dict) -> str:
    """Best-effort model id from a parameter-set dict, tolerating the legacy
    ``model_name`` = ``natcap.invest.<id>`` spelling."""
    mid = raw.get("model_id") or raw.get("modelID") or ""
    if not mid:
        mn = str(raw.get("model_name") or raw.get("modelName") or "")
        if mn.startswith(_MODEL_NAME_PREFIX):
            mn = mn[len(_MODEL_NAME_PREFIX):]
        mid = mn
    return str(mid).strip()


def build_parameter_set(model_id: str, args: dict, *, invest_version: str = "") -> dict:
    ps: dict = {"args": dict(args), "model_id": str(model_id)}
    if invest_version:
        ps["invest_version"] = str(invest_version)
    return ps


def relativize_args(
    args: dict, path_arg_names, base_dir: str | Path
) -> tuple[dict, list[str]]:
    """Rewrite absolute path args as paths relative to ``base_dir`` (POSIX
    separators, as the Workbench writes them). Returns ``(new_args, changed)``.
    Paths on a different drive (Windows) are left absolute."""
    base = Path(base_dir)
    out = dict(args)
    changed: list[str] = []
    for name in path_arg_names:
        val = args.get(name)
        if not val or not isinstance(val, str):
            continue
        p = Path(val)
        if not p.is_absolute():
            continue
        try:
            rel = os.path.relpath(p, base)
        except ValueError:
            continue  # different drive
        out[name] = Path(rel).as_posix()
        changed.append(name)
    return out, changed


def absolutize_args(args: dict, path_arg_names, base_dir: str | Path) -> dict:
    """Resolve relative path args against ``base_dir`` (the datastack's folder)."""
    base = Path(base_dir)
    out = dict(args)
    for name in path_arg_names:
        val = args.get(name)
        if not val or not isinstance(val, str):
            continue
        p = Path(val)
        if p.is_absolute():
            continue
        out[name] = str((base / p).resolve())
    return out


def read_parameter_set(src_path: str | Path) -> dict:
    """Parse a ``.invest.json`` parameter set. Raises ``ValueError`` if the file
    is not a parameter set. Returns ``{model_id, args, invest_version,
    raw_keys}`` (paths are left exactly as written -- see ``absolutize_args``)."""
    src = Path(src_path)
    try:
        raw = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{src.name} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("args"), dict):
        raise ValueError(
            "not an InVEST parameter set: expected a JSON object with an "
            "'args' object")
    return {
        "model_id": model_id_from_raw(raw),
        "args": raw["args"],
        "invest_version": str(raw.get("invest_version") or ""),
        "raw_keys": sorted(raw),
    }


def write_parameter_set(
    dst_path: str | Path, model_id: str, args: dict, *, invest_version: str = ""
) -> Path:
    dst = Path(dst_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    ps = build_parameter_set(model_id, args, invest_version=invest_version)
    dst.write_text(json.dumps(ps, indent=2), encoding="utf-8")
    return dst
