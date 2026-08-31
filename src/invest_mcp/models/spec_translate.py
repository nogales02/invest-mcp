"""Translate an InVEST MODEL_SPEC into (a) a JSON Schema for the ``args`` object
and (b) a compact human-readable briefing an LLM can reason about.

InVEST arg ``type`` values seen in the wild:
    workspace, directory, file, raster, vector, raster_or_vector, csv,
    number, integer, ratio, percent, boolean, freestyle_string, option_string
Unknown types degrade gracefully to ``string``.
"""

from __future__ import annotations

from typing import Any

_PATHISH = {"raster", "vector", "raster_or_vector", "csv", "file", "directory"}
_SPATIAL = {"raster", "vector", "raster_or_vector"}

# args we manage ourselves and therefore hide from the generated schema
_SERVER_MANAGED = {"workspace_dir", "n_workers"}


def _options_to_enum(spec: dict) -> list[str] | None:
    opts = spec.get("options")
    if not opts:
        return None
    if isinstance(opts, dict):
        return list(opts.keys())
    if isinstance(opts, (list, tuple)):
        out = []
        for o in opts:
            if isinstance(o, dict):
                out.append(str(o.get("key", o.get("value", o))))
            else:
                out.append(str(o))
        return out
    return None


def _describe(spec: dict) -> str:
    bits = []
    about = (spec.get("about") or "").strip()
    if about:
        bits.append(about)
    units = spec.get("units")
    if units:
        bits.append(f"Units: {units}.")
    if spec.get("type") in _PATHISH:
        kind = spec["type"].replace("_", " ")
        bits.append(f"Provide an absolute path to a {kind} file/folder on disk.")
    if spec.get("projected"):
        pu = spec.get("projection_units") or "m"
        bits.append(f"Must use a projected coordinate system (units: {pu}).")
    req = spec.get("required")
    if isinstance(req, str):
        bits.append(f"Required only when: {req}.")
    return " ".join(bits)


def arg_to_json_schema(spec: dict) -> dict[str, Any]:
    t = spec.get("type", "freestyle_string")
    node: dict[str, Any]
    if t == "boolean":
        node = {"type": "boolean"}
    elif t == "integer":
        node = {"type": "integer"}
    elif t in ("number", "ratio", "percent"):
        node = {"type": "number"}
        if t == "ratio":
            node.update(minimum=0, maximum=1)
        if t == "percent":
            node.update(minimum=0, maximum=100)
    elif t == "option_string":
        node = {"type": "string"}
        enum = _options_to_enum(spec)
        if enum:
            node["enum"] = enum
    else:
        # workspace/directory/file/raster/vector/csv/freestyle_string/unknown
        node = {"type": "string"}
    desc = _describe(spec)
    if desc:
        node["description"] = desc
    return node


def spec_to_args_schema(model_spec: dict) -> dict[str, Any]:
    """JSON Schema (draft 2020-12 compatible) for the model's ``args`` dict."""
    args: dict = model_spec.get("args", {})
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, arg_spec in args.items():
        if name in _SERVER_MANAGED or arg_spec.get("hidden") is True:
            continue
        properties[name] = arg_to_json_schema(arg_spec)
        if arg_spec.get("required") is True:
            required.append(name)
    schema: dict[str, Any] = {
        "type": "object",
        "title": f"{model_spec.get('model_id', 'model')} args",
        "properties": properties,
        "additionalProperties": True,
    }
    if required:
        schema["required"] = required
    return schema


def path_args(model_spec: dict) -> dict[str, str]:
    """Map of arg name -> InVEST type, for every arg that points at an input file."""
    return {
        name: s["type"]
        for name, s in model_spec.get("args", {}).items()
        if s.get("type") in _PATHISH
    }


def table_arg_specs(model_spec: dict) -> dict[str, dict]:
    """Per CSV arg: its key column and the columns InVEST expects.

    ``{arg: {"index_col": str|None, "required": bool|str,
             "about": str, "columns": [{"id","about","required","units"}]}}``

    ``required`` on a column is ``True``, ``False`` or a **string** (a condition,
    e.g. ``"calc_n"``). Hidden / disallowed columns are dropped. Column ids may
    contain a ``[TOKEN]`` placeholder (``kc_[MONTH]``) -- left as-is here.
    """
    out: dict[str, dict] = {}
    for name, s in model_spec.get("args", {}).items():
        if s.get("type") != "csv":
            continue
        raw = s.get("columns")
        items = (
            list(raw) if isinstance(raw, list)
            else [{"id": k, **v} for k, v in raw.items()] if isinstance(raw, dict)
            else []
        )
        cols = [
            {
                "id": c.get("id"),
                "about": (c.get("about") or "").strip(),
                "required": c.get("required"),
                "units": c.get("units"),
            }
            for c in items
            if c.get("id") and not c.get("hidden") and c.get("allowed") is not False
        ]
        out[name] = {
            "index_col": s.get("index_col"),
            "required": s.get("required"),
            "about": (s.get("about") or "").strip(),
            "columns": cols,
        }
    return out


def spatial_arg_specs(model_spec: dict) -> dict[str, dict]:
    """Per spatial-input arg: whether a projected CRS is mandated and its units.

    Used to drive the geospatial preflight. CSV / plain-file args are excluded.
    """
    out: dict[str, dict] = {}
    for name, s in model_spec.get("args", {}).items():
        if s.get("type") not in _SPATIAL:
            continue
        out[name] = {
            "kind": s["type"],
            "projected_required": bool(s.get("projected")),
            "projection_units": s.get("projection_units") or "m",
        }
    return out


def human_briefing(model_spec: dict) -> str:
    """A short markdown briefing: purpose, inputs (grouped), outputs."""
    lines: list[str] = []
    title = model_spec.get("model_title") or model_spec.get("model_id", "InVEST model")
    lines.append(f"# {title} (`{model_spec.get('model_id')}`)")
    if model_spec.get("about"):
        lines.append("")
        lines.append(model_spec["about"].strip())
    ug = model_spec.get("userguide")
    if ug:
        lines.append("")
        lines.append(f"User guide page: `{ug}`")

    args: dict = model_spec.get("args", {})
    order = model_spec.get("input_field_order") or [list(args.keys())]
    lines.append("\n## Inputs")
    for group in order:
        for name in group:
            s = args.get(name)
            if not s or name in _SERVER_MANAGED or s.get("hidden") is True:
                continue
            req = s.get("required")
            tag = "required" if req is True else ("optional" if req is False else f"required if `{req}`")
            label = s.get("name") or name
            about = (s.get("about") or "").strip().split("\n")[0]
            units = f" _(units: {s['units']})_" if s.get("units") else ""
            lines.append(f"- **{name}** — {label} · `{s.get('type')}` · {tag}{units}\n  {about}")

    outputs: dict = model_spec.get("outputs", {})
    if outputs:
        lines.append("\n## Key outputs")
        for oid, o in list(outputs.items())[:20]:
            about = (o.get("about") or "").strip().split("\n")[0]
            path = o.get("path", oid)
            lines.append(f"- `{path}` — {about}")
    return "\n".join(lines)
