"""Loader for the cited coefficient knowledge base (``knowledge/coefficients/``).

Pure stdlib. Reads the JSON/Markdown files shipped beside this module and hands
them back as text (they are already the wire format). Nothing here decides which
coefficient to use -- it surfaces the records, their context and their citations
so the client can choose and report the source.

Layout::

    knowledge/coefficients/
      README.md            how the KB is organised + how to choose a value
      sources.json         bibliography: every source_key -> full citation + context
      usle_c.json          SDR   cover-management factor
      usle_p.json          SDR   support-practice factor
      ndr_nutrient.json    NDR   load_n/p, eff_n/p, crit_len_n/p, proportion_subsurface_n
      curve_number.json    SWY   CN_A..D (SCS curve number by hydrologic soil group)
      kc.json              SWY/AWY  evapotranspiration coefficient (monthly + annual)
      root_depth.json      AWY   maximum / 95%-biomass rooting depth (mm)
      carbon_pools.json    Carbon  c_above, c_below, c_soil, c_dead (Mg C / ha)
      profiles/
        moorabool_fs28.json   a fully worked local table, kept as a documented example
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_DIR = Path(__file__).parent / "coefficients"

# name -> file, for invest://coefficients/{name}
PARAMETERS: tuple[str, ...] = (
    "usle_c",
    "usle_p",
    "ndr_nutrient",
    "curve_number",
    "kc",
    "root_depth",
    "carbon_pools",
)
PROFILES: tuple[str, ...] = ("moorabool_fs28",)

_EXTRA = {"sources", "readme"}


class UnknownEntryError(KeyError):
    """Asked for a coefficient file that does not exist."""


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _param_payload(name: str) -> dict:
    return json.loads(_read(_DIR / f"{name}.json"))


def entry(name: str) -> str:
    """Raw text of one KB file. ``name`` is a parameter (see ``PARAMETERS``), a
    profile name, ``sources`` or ``readme``. Raises ``UnknownEntryError``."""
    key = name.strip().lower().removesuffix(".json").removesuffix(".md")
    if key in PARAMETERS:
        return _read(_DIR / f"{key}.json")
    if key in PROFILES:
        return _read(_DIR / "profiles" / f"{key}.json")
    if key == "sources":
        return _read(_DIR / "sources.json")
    if key in ("readme", "read-me", "index-doc"):
        return _read(_DIR / "README.md")
    raise UnknownEntryError(name)


# lowercased InVEST table column -> KB parameter file it is described by.
# kc / kc_1..kc_12 are matched separately (see _KC_RE).
_COLUMN_TO_PARAM: dict[str, str] = {
    "usle_c": "usle_c",
    "usle_p": "usle_p",
    "load_n": "ndr_nutrient",
    "load_p": "ndr_nutrient",
    "eff_n": "ndr_nutrient",
    "eff_p": "ndr_nutrient",
    "crit_len_n": "ndr_nutrient",
    "crit_len_p": "ndr_nutrient",
    "proportion_subsurface_n": "ndr_nutrient",
    "cn_a": "curve_number",
    "cn_b": "curve_number",
    "cn_c": "curve_number",
    "cn_d": "curve_number",
    "root_depth": "root_depth",
    "c_above": "carbon_pools",
    "c_below": "carbon_pools",
    "c_soil": "carbon_pools",
    "c_dead": "carbon_pools",
}
_KC_RE = re.compile(r"^kc(_(1[0-2]|[1-9]))?$")


def parameter_for_column(column: str) -> str | None:
    """Which KB parameter file (if any) describes an InVEST table column.

    Case-insensitive. ``kc`` and ``kc_1``..``kc_12`` map to ``kc``; everything
    else is a fixed lookup. Returns ``None`` for a column the KB has no bearing
    on (``lucode``, ``description``, ``is_tropical``, ...)."""
    c = column.strip().lower()
    if c in _COLUMN_TO_PARAM:
        return _COLUMN_TO_PARAM[c]
    if _KC_RE.match(c):
        return "kc"
    return None


def column_ranges() -> dict[str, dict]:
    """Lowercased InVEST column -> ``{"parameter", "resource", "typical": [lo, hi]}``.

    The ``typical`` band is the KB parameter's own ``typical_range`` -- a dict
    keyed by sub-parameter for NDR / carbon, a scalar ``[lo, hi]`` otherwise. It
    is the cited-literature range, meant for a **soft** out-of-band warning, not
    a hard bound. Columns whose parameter file gives no usable band are omitted.
    """
    cache: dict[str, dict] = {}
    cols = (
        list(_COLUMN_TO_PARAM)
        + ["kc"]
        + [f"kc_{m}" for m in range(1, 13)]
    )
    out: dict[str, dict] = {}
    for col in cols:
        param = parameter_for_column(col)
        if not param:
            continue
        payload = cache.get(param)
        if payload is None:
            try:
                payload = cache[param] = _param_payload(param)
            except FileNotFoundError:  # pragma: no cover - files always shipped
                continue
        tr = payload.get("typical_range")
        band = tr.get(col if col in tr else col.lower()) if isinstance(tr, dict) else tr
        if not (isinstance(band, list) and len(band) == 2
                and all(isinstance(x, (int, float)) for x in band)):
            continue
        out[col] = {
            "parameter": param,
            "resource": f"invest://coefficients/{param}",
            "typical": [band[0], band[1]],
        }
    return out


def index() -> str:
    """A compact catalog of what the KB holds -- parameter, models, definition,
    record count, and the source keys each parameter leans on."""
    params = []
    for name in PARAMETERS:
        try:
            p = _param_payload(name)
        except FileNotFoundError:  # pragma: no cover - file always shipped
            continue
        recs = p.get("records", [])
        params.append(
            {
                "parameter": p.get("parameter", name),
                "resource": f"invest://coefficients/{name}",
                "aka": p.get("aka", []),
                "models": p.get("models", []),
                "invest_column": p.get("invest_column"),
                "units": p.get("units"),
                "definition": p.get("definition"),
                "record_count": len(recs),
                "source_keys": sorted({r["source_key"] for r in recs if r.get("source_key")}),
            }
        )
    try:
        src_keys = sorted(json.loads(_read(_DIR / "sources.json")).get("sources", {}))
    except FileNotFoundError:  # pragma: no cover
        src_keys = []
    return json.dumps(
        {
            "about": (
                "Cited starting-point coefficients for InVEST biophysical / lookup "
                "tables. Records are described by semantic attributes (vegetation "
                "form, canopy density, condition, management, biome, region, scale) "
                "-- not tied to one land-cover legend. Match your class against "
                "those attributes, prefer a source whose biome/region/scale is "
                "closest to your site, and always carry the citation through. "
                "Every value is a starting point to verify, not an answer."
            ),
            "how_to_choose": "invest://coefficients/readme",
            "bibliography": "invest://coefficients/sources",
            "parameters": params,
            "profiles": [
                {
                    "name": n,
                    "resource": f"invest://coefficients/{n}",
                    "note": "a fully worked local table, kept as a documented example -- "
                    "not a default; read its 'context' before reusing any number",
                }
                for n in PROFILES
            ],
            "source_key_count": len(src_keys),
        },
        indent=2,
    )
