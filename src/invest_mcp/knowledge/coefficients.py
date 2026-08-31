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
