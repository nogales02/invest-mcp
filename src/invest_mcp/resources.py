"""MCP resources -- reference material a client can read without a tool call.

Everything here is either static curated text or derived deterministically from
an InVEST MODEL_SPEC (catalogs, cheat-sheets). A resource never makes a
decision; it hands the model facts to reason with. See ``CLAUDE.md`` section 6.
"""

from __future__ import annotations

import json

from invest_mcp.knowledge import coefficients as coeff_kb
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
| SoilGrids (ISRIC) | Sand/silt/clay, SOC, depth to bedrock. Derive PAWC, K-factor, hydrologic soil group. **Wired: `fetch_soil` (`variable=texture` / `hydrologic_soil_group` / `usle_k` from SoilGrids 2.0; `depth_to_bedrock` from SoilGrids 2017 BDTICM).** |
| HWSD v2 (FAO) | Harmonised World Soil Database. |
| FutureWater HiHydroSoil v2 | Ready-made hydraulic properties incl. hydrologic soil group. |

## Hydrography
| Source | Notes |
|---|---|
| HydroSHEDS / HydroRIVERS / HydroBASINS | Rivers, basin polygons, flow dir/acc. `hydrosheds.org`. **Wired: `fetch_hydrography` (`product=rivers` / `basins`, HydroSHEDS v1).** |
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

_MODEL_GUIDE_MD = """\
# InVEST model guide — which model answers which question

A starting map from a real-world question to InVEST model(s). It does **not**
choose for you: confirm every candidate against `invest://model/{id}/cheatsheet`
(or `describe_invest_model`) and check it is installed via `invest://models`.
Each entry: **answers** / **needs** (headline inputs — the cheatsheet has the
full list with units) / **gives** / optional **pair with** / **not for**.

## Terrestrial water & soil (watershed scale; share DEM + LULC + watersheds)

### `annual_water_yield` (aliases: awy, hwy) — Annual Water Yield
- **answers:** long-term *mean annual* water supply per watershed; hydropower
  production and value; water-scarcity / allocation baselines.
- **needs:** annual precipitation, reference ET (ETo), plant-available water
  content, root-restricting layer depth, LULC, watersheds + sub-watersheds,
  biophysical table (`Kc`, `root_depth`, `LULC_veg`), seasonality factor `Z`;
  optional demand table + hydropower valuation.
- **gives:** water yield (mm and m³) per pixel and per (sub)watershed; with
  valuation, energy (kWh) and revenue.
- **pair with:** `seasonal_water_yield` for timing.
- **not for:** daily/event flows, flood peaks, groundwater levels.

### `seasonal_water_yield` (alias: swy) — Seasonal Water Yield
- **answers:** intra-annual flow — quickflow vs baseflow, dry-season water
  availability, groundwater recharge.
- **needs:** 12 monthly precipitation rasters, 12 monthly ETo, DEM, LULC,
  hydrologic soil group, AOI, biophysical table (`CN_A..D`, `Kc_1..12`), rain-
  events table, climate-zone table, `alpha`/`beta`/`gamma`.
- **gives:** monthly quickflow (QF), local recharge, and baseflow (B) per pixel.
- **not for:** routing a specific storm hydrograph.

### `sdr` — Sediment Delivery Ratio
- **answers:** hillslope soil loss (USLE) and sediment delivered to streams;
  reservoir sedimentation; where erosion-control BMPs pay off.
- **needs:** DEM, rainfall erosivity `R`, soil erodibility `K`, LULC, watersheds,
  biophysical table (`usle_c`, `usle_p`), thresholds (`IC0`, `k`, `SDR_max`);
  optional drainage layer.
- **gives:** USLE soil loss (t/ha·yr), sediment export and retention per pixel
  and watershed.
- **pair with:** `ndr` (same DEM/LULC/watersheds), `stormwater`.
- **not for:** gully / streambank / landslide erosion, single-event sediment.

### `ndr` — Nutrient Delivery Ratio
- **answers:** nitrogen and/or phosphorus export and retention to streams; water-
  quality and eutrophication risk; riparian-buffer and load-reduction scenarios.
- **needs:** DEM, LULC, a nutrient runoff proxy (annual precip or SWY quickflow),
  watersheds, biophysical table (`load_n/p`, `eff_n/p`, `crit_len_n/p`,
  `proportion_subsurface_n`), Borselli `k` + threshold, subsurface parameters.
- **gives:** N/P export and retention per pixel and per watershed.
- **pair with:** `sdr`.
- **not for:** in-stream transformation, point sources not expressed as loads,
  groundwater plumes.

## Land carbon & habitat

### `carbon` — Carbon Storage and Sequestration
- **answers:** carbon stored now in four pools; sequestration (Δ) between two
  LULC maps; REDD+ baselines; social value of carbon.
- **needs:** LULC (baseline, optional alternative/REDD), carbon pools table
  (`c_above/c_below/c_soil/c_dead`, Mg C/ha).
- **pair with:** `forest_carbon_edge_effect` (tropical AGB realism),
  `habitat_quality`.
- **not for:** carbon fluxes through time (wetlands → `coastal_blue_carbon`),
  soil-carbon dynamics.

### `forest_carbon_edge_effect` (alias: fc) — Forest Carbon Edge Effect
- **answers:** tropical-forest carbon with aboveground biomass corrected for
  degradation near forest edges.
- **needs:** LULC, biophysical table, edge-effect regression parameters (bundled
  for the tropics); optional non-forest pools.
- **not for:** temperate/boreal or non-forest landscapes → use `carbon`.

### `habitat_quality` (alias: hq) — Habitat Quality
- **answers:** relative habitat quality and degradation driven by land-use
  threats; biodiversity co-benefit of a land-use scenario.
- **needs:** LULC (current/baseline/future), threat rasters + threats table
  (max distance, weight, decay), sensitivity table, half-saturation constant.
- **gives:** habitat quality (0–1) and degradation per pixel; optional rarity.
- **not for:** species-specific viability or actual biodiversity counts.

### `habitat_risk_assessment` (alias: hra) — Habitat Risk Assessment
- **answers:** cumulative risk to multiple habitats from multiple human
  stressors (often marine/coastal spatial planning).
- **needs:** habitat and stressor layers, exposure/consequence criteria tables
  with data-quality ratings, AOI, resolution.
- **gives:** per-habitat and cumulative risk maps + risk classes.
- **not for:** quantifying a service — this is a risk screen.

## Urban

### `urban_flood_risk_mitigation` (alias: ufrm) — Urban Flood Risk Mitigation
- **answers:** stormwater runoff retained by green infrastructure for a *design
  storm*, and the flood damage value avoided.
- **needs:** AOI watersheds, LULC, hydrologic soil group, design-storm depth
  (mm), biophysical table (curve numbers); optional built infrastructure +
  damage-loss table.
- **not for:** flood inundation extent / depth (no hydraulics).

### `stormwater` — Urban Stormwater Retention
- **answers:** *annual* runoff and pollutant load retained by land cover;
  recharge; retrofit value.
- **needs:** LULC, hydrologic soil group, annual precipitation, biophysical
  table (retention ratios + event mean concentrations); optional roads /
  impervious, AOI.
- **pair with:** `urban_flood_risk_mitigation` (event vs annual).

### `urban_cooling_model` — Urban Cooling
- **answers:** heat mitigation from shade, albedo and evapotranspiration; air-
  temperature reduction and its health / energy value.
- **needs:** LULC, biophysical table (shade, albedo, ET, green-area flag),
  reference ET, reference air temperature + UHI magnitude, AOI; optional
  building footprints for energy/mortality valuation.
- **not for:** street-level microclimate / CFD.

### `urban_nature_access` (alias: una) — Urban Nature Access
- **answers:** supply of and demand for accessible green space; which population
  groups are under-served (equity).
- **needs:** LULC (nature classes), population raster, admin units, search
  radius, per-group weights.

### `urban_mental_health` (alias: umh) — Urban Mental Health
- **answers:** population mental-health burden attributable to greenspace
  exposure (newer / evolving model).
- **needs:** greenspace + population layers, dose-response parameters, admin
  units.

## Coastal & marine

### `coastal_vulnerability` (alias: cv) — Coastal Vulnerability
- **answers:** *relative* exposure of the coastline to erosion and storm
  flooding, and how much habitat reduces it.
- **needs:** shoreline/AOI, bathymetry, geomorphology, natural habitats, wind &
  wave climate (WaveWatch III), sea-level-rise, DEM, population.
- **not for:** absolute erosion rates or inundation depth.

### `coastal_blue_carbon` (alias: cbc) + `coastal_blue_carbon_preprocessor` (cbc_pre)
- **answers:** carbon accumulation and loss in mangroves / salt marsh /
  seagrass over time under land-cover transitions, and its value.
- **needs:** LULC time points (the preprocessor builds the transition table),
  carbon-pool and accumulation-rate tables, price + discount rate.
- **not for:** upland/terrestrial carbon → use `carbon`.

### `wave_energy` — Wave Energy Production
- **answers:** harvestable wave energy and economic value at candidate sites.
- **needs:** wave climate (WaveWatch III), bathymetry, machine-performance and
  economic tables, AOI grid, landing points.

### `wind_energy` — Wind Energy Production
- **answers:** offshore wind energy output and economic value (NPV, levelized
  cost).
- **needs:** wind time series, bathymetry, turbine and economic parameters, AOI,
  grid connection / landing points.

## Agriculture & pollination

### `pollination` — Crop Pollination
- **answers:** wild-bee abundance supported by the landscape and its
  contribution to pollinator-dependent crop yield.
- **needs:** LULC, guild table (nesting, floral seasons, flight range,
  activity), biophysical table (floral resources & nesting suitability by LULC
  and season); optional farm vector for the yield step.
- **pair with:** `crop_production_regression`.

### `crop_production_percentile` (cpp) / `crop_production_regression` (cpr)
- **answers:** attainable vs actual yield, production and nutrient output for
  ~175 crops; the regression variant responds to fertiliser and irrigation.
- **needs:** LULC, crop-to-LULC map; (regression) fertiliser + irrigation
  rasters.

## Recreation & scenery

### `recreation` — Visitation: Recreation and Tourism
- **answers:** predicted visitor-days as a function of natural and built
  attributes; how visitation shifts under a scenario.
- **needs:** AOI grid, predictor layers, year range.

### `scenic_quality` (alias: sq) — Scenic Quality
- **answers:** visual impact / viewshed of built features (wind turbines,
  development) over a landscape and its viewers.
- **needs:** DEM, feature points/lines, AOI; optional weights, population.

## Terrain & scenario tools (prerequisites, not services)

- **`delineateit`** — watersheds / pour-point catchments from a DEM (feeds the
  `watersheds` input of SDR / NDR / SWY). This server also has the
  `delineate_watersheds` tool.
- **`routedem`** — pit-fill, flow direction / accumulation, stream extraction
  from a DEM.
- **`scenario_generator_proximity`** (alias: sgp) — builds land-use-change maps
  by proximity rules, to feed a baseline-vs-scenario comparison.

## Outside InVEST's scope

Real-time or forecast hydrology; hydrodynamic flood inundation; species
distribution / population viability models; air-quality dispersion; detailed
groundwater flow; economy-wide (CGE) analysis. Say so plainly when the question
lands here.
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


def model_guide() -> str:
    return _MODEL_GUIDE_MD


def coefficients_index() -> str:
    return coeff_kb.index()


def coefficients_entry(name: str) -> str:
    try:
        return coeff_kb.entry(name)
    except (coeff_kb.UnknownEntryError, FileNotFoundError):
        known = list(coeff_kb.PARAMETERS) + list(coeff_kb.PROFILES) + ["sources", "readme"]
        return json.dumps(
            {
                "error": f"unknown coefficient entry {name!r}",
                "known": known,
                "hint": "read invest://coefficients for the catalog",
            },
            indent=2,
        )


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
    server.resource(
        "invest://model-guide",
        name="InVEST model guide",
        mime_type="text/markdown",
        description="Which InVEST model answers which real-world question, with "
                    "each model's headline inputs, outputs and common pairings. "
                    "A starting map for recommend_model; confirm against the "
                    "per-model cheat-sheet.",
    )(model_guide)
    server.resource(
        "invest://coefficients",
        name="Cited coefficient knowledge base",
        mime_type="application/json",
        description=(
            "Catalog of cited starting-point coefficients for InVEST biophysical / "
            "lookup tables (usle_c, usle_p, NDR loads/efficiencies, curve numbers, "
            "Kc, root_depth, carbon pools). Records are keyed by semantic cover "
            "attributes, not one land-cover legend, and every value carries a source."
        ),
    )(coefficients_index)
    server.resource(
        "invest://coefficients/{name}",
        name="Coefficient table / bibliography / worked profile",
        mime_type="application/json",
        description=(
            "One coefficient file: a parameter (usle_c, usle_p, ndr_nutrient, "
            "curve_number, kc, root_depth, carbon_pools), 'sources' (the "
            "bibliography), 'readme' (how to choose a value), or a worked profile "
            "(e.g. moorabool_fs28)."
        ),
    )(coefficients_entry)
