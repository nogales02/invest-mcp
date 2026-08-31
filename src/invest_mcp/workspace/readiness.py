"""Project data-readiness assessment -- pure logic, no GDAL.

Given a scaffolded project folder and a set of InVEST MODEL_SPECs, report which
models could be *attempted* now and which required file inputs are still
missing. This **supports** the decision of what to run; it does not make it --
the file<->arg matching is name-based and best-effort, and every guess is
surfaced (``matched`` / ``ambiguous`` / ``missing``) for the caller to confirm.

Only stdlib + :mod:`invest_mcp.models.spec_translate` (also pure), so this runs
in the server process.
"""

from __future__ import annotations

from pathlib import Path

from invest_mcp.models import spec_translate

_RASTER_SUF = {".tif", ".tiff", ".vrt", ".img", ".nc", ".jp2"}
_VECTOR_SUF = {".shp", ".gpkg", ".geojson", ".json", ".gml", ".fgb", ".kml"}
_TABLE_SUF = {".csv", ".xlsx", ".xls", ".tsv"}
_SKIP_PARTS = {"taskgraph_cache", "_taskgraph_working_dir", "jobs", "logs", "datastacks"}

# Common models to assess when the caller doesn't name any.
DEFAULT_MODELS = (
    "carbon",
    "annual_water_yield",
    "seasonal_water_yield",
    "sdr",
    "ndr",
    "pollination",
    "urban_flood_risk_mitigation",
)

# One keyword table, matched against BOTH file stems and InVEST arg names. Roles
# are deliberately coarse (e.g. a basin polygon covers both `watersheds_path`
# and `aoi_path`). Best-effort -- unmatched required inputs are reported, not
# hidden.
ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "dem": ("dem", "elevation", "srtm", "mdt", "mde"),
    "lulc": ("lulc", "landcover", "land_cover", "landuse", "land_use",
             "cobertura", "worldcover"),
    "watersheds": ("watershed", "basin", "subbasin", "cuenca", "subcuenca",
                   "aoi", "area_of_interest", "study_area", "boundary", "limite"),
    "precipitation": ("precipitation", "precip", "rainfall", "chirps"),
    "et0": ("eto", "et0", "evapotransp", "reference_et"),
    "erosivity": ("erosivity",),
    "erodibility": ("erodibility",),
    "runoff_proxy": ("runoff_proxy", "runoff"),
    "soil_group": ("soil_group", "soilgroup", "hydrological_group",
                   "hydrologic_soil", "soils_hydrological", "hsg"),
    "soil_depth": ("soil_depth", "soildepth", "depth_to_root", "root_rest",
                   "rooting_depth", "rootdepth"),
    "pawc": ("pawc", "plant_available_water"),
    "biophysical_table": ("biophysical",),
    "carbon_table": ("carbon_pool",),
    "guild_table": ("guild",),
    "curve_number_table": ("curve_number",),
    "structures": ("structure",),
    "drainage": ("drainage",),
}

_KIND_FOR_TYPE = {"raster": "raster", "vector": "vector", "csv": "table"}


def guess_role(name: str) -> str | None:
    """First role whose keywords appear in ``name``'s lower-cased stem."""
    stem = Path(name).stem.lower()
    for role, kws in ROLE_KEYWORDS.items():
        if any(k in stem for k in kws):
            return role
    return None


def scan_project(root: Path) -> list[dict]:
    """Every raster / vector / table file under ``data/`` and ``tables/``."""
    root = Path(root)
    out: list[dict] = []
    for base in ("data", "tables"):
        d = root / base
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if p.is_dir() or set(p.parts) & _SKIP_PARTS:
                continue
            suf = p.suffix.lower()
            kind = (
                "raster" if suf in _RASTER_SUF
                else "vector" if suf in _VECTOR_SUF
                else "table" if suf in _TABLE_SUF
                else None
            )
            if kind is None:
                continue
            out.append({
                "path": str(p),
                "relpath": str(p.relative_to(root)).replace("\\", "/"),
                "kind": kind,
                "role": guess_role(p.name),
            })
    return out


def assess_model(model_id: str, spec: dict, inventory: list[dict]) -> dict:
    """Match a model's *required* file/table args against the project inventory."""
    schema = spec_translate.spec_to_args_schema(spec)
    required = set(schema.get("required", []))
    path_types = spec_translate.path_args(spec)          # {arg: invest type}
    args = spec.get("args", {})

    by_role: dict[str, list[dict]] = {}
    for item in inventory:
        if item["role"]:
            by_role.setdefault(item["role"], []).append(item)

    matched: dict[str, str] = {}
    missing: list[dict] = []
    ambiguous: list[dict] = []
    for arg, itype in path_types.items():
        if arg not in required:
            continue  # only block on required inputs
        role = guess_role(arg)
        cands = list(by_role.get(role, [])) if role else []
        want = _KIND_FOR_TYPE.get(itype)
        if want:
            cands = [c for c in cands if c["kind"] == want] or cands
        about = (args.get(arg, {}).get("about") or "").strip().split("\n")[0]
        if len(cands) == 1:
            matched[arg] = cands[0]["path"]
        elif len(cands) > 1:
            ambiguous.append({"arg": arg, "type": itype, "role": role,
                              "candidates": [c["relpath"] for c in cands]})
        else:
            missing.append({"arg": arg, "type": itype, "role": role, "about": about})

    needs_values = sorted(a for a in required if a not in path_types)
    can_attempt = not missing and not ambiguous
    return {
        "model_id": model_id,
        "title": spec.get("model_title") or model_id,
        "can_attempt": can_attempt,
        "matched": matched,
        "ambiguous": ambiguous,
        "missing": missing,
        "needs_values": needs_values,
    }


def narrative(inventory: list[dict], assessments: list[dict]) -> str:
    by_kind: dict[str, int] = {}
    roles: set[str] = set()
    for it in inventory:
        by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1
        if it["role"]:
            roles.add(it["role"])
    lines = [
        f"{len(inventory)} data file(s): "
        + (", ".join(f"{n} {k}" for k, n in sorted(by_kind.items())) or "none")
        + ".",
        "Roles detected: " + (", ".join(sorted(roles)) or "none") + ".",
        "",
    ]
    for a in assessments:
        if a.get("error"):
            lines.append(f"- `{a['model_id']}` — could not read spec: {a['error']}")
            continue
        if a["can_attempt"]:
            extra = (f" (still needs values for: {', '.join(a['needs_values'])})"
                     if a["needs_values"] else "")
            lines.append(f"- `{a['model_id']}` — inputs matched, ready to attempt{extra}.")
        else:
            gaps = [m["arg"] for m in a["missing"]] + [x["arg"] for x in a["ambiguous"]]
            lines.append(f"- `{a['model_id']}` — {len(gaps)} input(s) to resolve: "
                         + ", ".join(gaps) + ".")
    return "\n".join(lines)
