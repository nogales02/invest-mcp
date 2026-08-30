"""MCP resources -- reference material a client can read without a tool call.

Everything here is either static curated text or derived deterministically from
an InVEST MODEL_SPEC (catalogs, cheat-sheets). A resource never makes a
decision; it hands the model facts to reason with. See ``CLAUDE.md`` section 6.
"""

from __future__ import annotations

import json

from invest_mcp.models import registry, spec_translate
from invest_mcp.workspace import project as project_layout

_CONVENTIONS_MD = """\
# invest-mcp project layout

`scaffold_project(root, ...)` creates this tree and a `project.json` manifest.
Every data-prep and run tool expects paths inside it (or another allowed folder).

{subdirs}

- **data/raw/** — exactly what you downloaded, never edited. Traceability starts here.
- **data/processed/** — everything derived: reprojected, clipped, aligned. Safe to delete
  and regenerate.
- **tables/** — biophysical / lookup CSVs.
- **datastacks/** — `{{model}}.invest.json` files (round-trip with the InVEST Workbench).
- **jobs/** — one folder per run: the InVEST workspace + `provenance.json`.
- **logs/** — free-form notes / run logs.

`project.json` fields: `name`, `created_utc`, `target_crs` (the CRS the whole study
works in), `aoi` (`{{path, relpath}}`), `subdirs`, `datasets` (filled by the
data-prep routines: one entry per dataset with its role and provenance).
"""

_DATA_SOURCES_MD = """\
# Data-source catalog (global, open)

Pick a source, download into `data/raw/`, then `reproject_layer` / `clip_to_aoi` /
`align_raster_stack` into `data/processed/` on the project's `target_crs`.

## Elevation (DEM)
| Source | Res | Notes |
|---|---|---|
| Copernicus DEM GLO-30 | 30 m | Best global default. AWS `copernicus-dem-30m` bucket; OpenTopography API. |
| NASADEM / SRTM v3 | 30 m | `earthdata.nasa.gov` (Earthdata login). 60N–56S only. |
| FABDEM | 30 m | Forest/building-removed SRTM — better for hydrology. Bristol data repository. |
| ASTER GDEM v3 | 30 m | Full latitude coverage; noisier. |

Hydrology models (SDR, NDR, SWY): fill pits / condition the DEM before use.

## Land cover / land use
| Source | Res / year | Notes |
|---|---|---|
| ESA WorldCover | 10 m, 2020/2021 | 11 classes. `esa-worldcover.org`. |
| ESRI Land Cover | 10 m, annual 2017– | 9 classes. `livingatlas.arcgis.com`. |
| Copernicus Global Land Cover | 100 m, 2015–2019 | 23 classes. |
| Dynamic World | 10 m, near-real-time | Google Earth Engine, per-scene probabilities. |
| National maps | varies | Usually preferred where they exist. |

Reproject land cover with **`resampling="nearest"`** (categorical).

## Precipitation / climate
| Source | Notes |
|---|---|
| CHIRPS | 0.05°, 1981–, daily/monthly. Good for the tropics. `chc.ucsb.edu/data/chirps`. |
| WorldClim v2 | 1 km climatology (1970–2000): monthly precip, tmin/tmax. |
| CHELSA v2 | 1 km climatology, better mountain skill. |
| TerraClimate | ~4 km monthly incl. reference ET (`pet`). |
| ERA5 / ERA5-Land | Copernicus CDS, hourly reanalysis. |

Annual Water Yield needs annual precip + reference ET (ETo). Seasonal Water Yield
needs 12 monthly precip rasters + 12 monthly ETo (or the raster-table CSVs).

## Reference evapotranspiration (ETo)
Global Aridity & ET0 Database (CGIAR-CSI); TerraClimate `pet`; or compute
(Hargreaves / modified Hargreaves) from WorldClim tmin/tmax/precip.

## Soil
| Source | Notes |
|---|---|
| SoilGrids 250 m (ISRIC) | Sand/silt/clay, SOC, depth to bedrock. Derive PAWC, K-factor, hydrologic soil group. |
| HWSD v2 (FAO) | Harmonised World Soil Database. |
| FutureWater HiHydroSoil v2 | Ready-made hydraulic properties incl. hydrologic soil group. |

## Hydrography
| Source | Notes |
|---|---|
| HydroSHEDS / HydroRIVERS / HydroBASINS | Rivers, basin polygons, flow dir/acc. `hydrosheds.org`. |
| MERIT Hydro | 90 m hydrography derived from MERIT DEM. |

Prefer deriving watersheds from your own conditioned DEM + pour points when a
`delineate_watersheds` routine is available (roadmap).

## Erosivity (R) and erodibility (K) for SDR
- **R**: Global Rainfall Erosivity (JRC/ESDAC, ~1 km), or regression on annual precip.
- **K**: derive from SoilGrids texture + SOC (Wischmeier & Smith / EPIC equations).

## Administrative boundaries (for AOI)
GADM (`gadm.org`), FAO GAUL, Natural Earth, OpenStreetMap (`osm-boundaries.com`).

_Cite the source, version and access date in `logs/` — InVEST results are only as
defensible as their inputs._
"""


def models_catalog() -> str:
    return json.dumps(
        {"models": [m.as_dict() for m in registry.list_models()]}, indent=2
    )


def model_cheatsheet(model_id: str) -> str:
    canonical = registry.resolve_model_id(model_id)
    spec = registry.get_spec(canonical)
    return spec_translate.human_briefing(spec)


def project_conventions() -> str:
    subdirs = "\n".join(f"- `{s}/`" for s in project_layout.SUBDIRS)
    return _CONVENTIONS_MD.format(subdirs=subdirs)


def data_sources() -> str:
    return _DATA_SOURCES_MD


def register(server) -> None:
    server.resource(
        "invest://models",
        name="InVEST model catalog",
        mime_type="application/json",
        description="Every InVEST model installed here: id, aliases, title.",
    )(models_catalog)
    server.resource(
        "invest://model/{model_id}/cheatsheet",
        name="InVEST model cheat-sheet",
        mime_type="text/markdown",
        description="Purpose, inputs (grouped, with units) and key outputs for one model.",
    )(model_cheatsheet)
    server.resource(
        "invest://conventions",
        name="Project folder convention",
        mime_type="text/markdown",
        description="The standard invest-mcp project layout and project.json fields.",
    )(project_conventions)
    server.resource(
        "invest://data-sources",
        name="Data-source catalog",
        mime_type="text/markdown",
        description="Where to get DEM / land cover / climate / soil / hydrography inputs.",
    )(data_sources)
