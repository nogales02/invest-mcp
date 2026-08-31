# invest-mcp — curated project-state snapshot (2026-08-31, rev 5)

Point-in-time synthesis so claude-mem and future sessions have the project history,
not just today's tooling meta. Source of truth remains `CLAUDE.md`; this is the
distilled state.

## ⟳ RESUME POINT (2026-08-31) — read this first

Latest session: the **cited-coefficient knowledge base** (`[resource]`, roadmap
item 10) — `src/invest_mcp/knowledge/coefficients/`, 7 parameter files
(`usle_c`, `usle_p`, `ndr_nutrient`, `curve_number`, `kc`, `root_depth`,
`carbon_pools`) + `sources.json` (verified bibliography) + `README.md` + a
worked `moorabool_fs28` profile. Built mostly by working through
`FS28-Moorabool_MasterFile.xlsx` sheet by sheet; `root_depth` + `carbon_pools`
from standard refs (Canadell 1996, Schenk & Jackson 2002, FAO-56, IPCC
2006/2019 Vol 4, Ruesch & Gibbs 2008). Records are **legend-agnostic** — keyed
by semantic cover attributes (form, canopy density, condition, management,
biome, region, scale), not one land-cover legend; `crosswalk` hints advisory
only; every value carries `source_key` + `confidence` + `verified`
(`web`/`excel`/`standard`). Pure-stdlib loader `knowledge/coefficients.py`; two
new resources (`invest://coefficients`, `invest://coefficients/{name}`); 11 new
tests. **Not committed.**

Prior two long sessions: the **data-preparation layer** (roadmap 8–10) then the
**Workbench hand-off** + hydrography + a soil refinement. Now **32 MCP tools +
6 resources + 2 prompts, 152 tests green** (146 pass + 6 skip — numpy helpers
absent from `.venv`, verified in `invest-geo`).

**The 11-branch stack is MERGED into `main`** (2026-08-30). `main` = `8a56650`
(`origin/main`), 11 bottom-up merge commits (PRs #2, #12, #4, #5, #6, #7, #8, #1,
#9, #10, #11 — the original #3 auto-closed when its base branch was deleted and
was recreated as #12). Every feature branch deleted, local and `origin`; only
`main` remains. Merges via the GitHub REST API with the git credential-manager
token; branch deletes via `git push origin --delete`. `gh` CLI is NOT installed.
Merge order (linear stack, zero conflicts): data-prep-routines →
readiness-resources-prompts → delineate-watersheds → tables-from-template →
fetch-dem → fetch-landcover → fetch-climate → fetch-soil → fetch-hydrography →
datastack-io → fetch-soil-bedrock.

**Data-prep chain, all built & verified end-to-end:** `scaffold_project` →
`fetch_dem` / `fetch_landcover` / `fetch_climate` / `fetch_soil` /
`fetch_hydrography` → `reproject_layer` / `clip_to_aoi` / `align_raster_stack` →
`delineate_watersheds` → `tables_from_template` → `project_readiness` →
`import_datastack` / `export_datastack` (Workbench round-trip) →
`validate_invest_args` → `run_invest_model` →
`summarize_results` / `compare_scenarios`.

**Gotcha:** remote `/vsicurl/` access is intermittently slow — S3
(`*.s3.amazonaws.com`) tile opens sometimes hung >180 s; `files.isric.org`
(SoilGrids) VRT reads take ~2–3 min for a small AOI; the SoilGrids-2017 BDTICM
GeoTIFF (~8.5 GB, non-COG) can truncate a windowed read (`TIFFReadEncodedStrip
failed`) → `_read_bdticm` retries 4×. HydroSHEDS (`data.hydrosheds.org`) was
fast & reliable but **blocks HEAD** → `CPL_VSIL_CURL_USE_HEAD=NO`. Not code
bugs; retry a hung `fetch_*` (worker timeout 1800 s).

**Next candidates** (CLAUDE.md §6): extend the coefficient KB (NDR
`eff`/`crit_len` outside semi-arid tropics, C/P for more regions, per-output
units, glossary) + `check_table_vs_raster` `[tool]` against it; `fetch_soil`
PAWC (SoilGrids 2017 `AWCh1..3`/`WWP`), HSG refined with Ksat/depth;
`fetch_climate` `source=terraclimate|chirps`; `clone_job`; `build_report`;
`recommend_model` `[prompt]`.

## What the project is

`invest-mcp` — an MCP server that lets an AI assistant (Claude Code, Claude
Desktop, any MCP client) drive **Natural Capital Project InVEST** models by
natural language: browse models, translate `MODEL_SPEC` to JSON Schema, validate
args, run a model against files on the user's disk, and review/interpret outputs.
It is a *sibling* of the InVEST Workbench, not a plugin — both sit on
`natcap.invest`; the integration point is the **datastack** format
(`.invest.json`), which this server reads and writes.

Repo: `github.com/nogales02/invest-mcp`, branch `main`, pushed. Git-only, no PyPI.
Client-agnostic. Author: Jonathan Nogales.

## Core architecture decision (v0.1)

The server is a **pure subprocess wrapper** over the `invest.exe` bundled with the
installed **InVEST Workbench** (`C:\Program Files\InVEST 3.20.1 Workbench\
resources\invest\invest.exe`, v3.20.1). It does **not** import `natcap.invest`.
Why: system Python is 3.14 with no GDAL wheels, so `pip install natcap.invest` is
impossible; subprocess isolation also contains GDAL segfaults and makes run
cancellation a real kill. One OS process per run, supervised by a daemon thread +
`BoundedSemaphore` for concurrency. Jobs persist to
`%USERPROFILE%\invest-mcp-data\jobs\<job_id>\`.

SDK: `mcp` 2.x — `FastMCP` was renamed to `MCPServer`
(`from mcp.server.mcpserver import MCPServer`); decorator API otherwise unchanged.

## Environments

- **Server** runs in `.venv` (Python 3.14) with only `mcp`, `pydantic`,
  `pydantic-settings`. No system Python is required any more: `environment-server.yml`
  (conda env `invest-mcp`, py3.12+pip) + `scripts/bootstrap.{ps1,sh}` can host the
  server in a conda env built with the Workbench's bundled micromamba. bootstrap
  auto-picks `.venv` vs conda; `-CondaServer`/`-Venv` force it; also runs
  `setup` + `doctor` + prints the client-registration snippet.
- **`invest-geo`** (conda-forge, py3.12) at `C:\Users\Nogales\.conda\envs\invest-geo`
  — gdal 3.13, rasterio, pyproj, shapely, pyogrio, pygeoprocessing 2.4.
  `invest_mcp` installed with `pip install -e . --no-deps`. Runs the geospatial
  workers. The server calls `<invest-geo>\python.exe -m invest_mcp.geo.preflight`
  by subprocess, with `GDAL_DATA`/`PROJ_DATA`/`PATH` injected by
  `config.geo_subprocess_env()`.
- **`invest-cal`** (conda-forge, py3.12) at `C:\Users\Nogales\.conda\envs\invest-cal`
  — natcap.invest 3.20.1, gdal 3.12, geopandas, rasterstats, matplotlib-base,
  numpy `<2.3`, `spotpy` (pip), `invest_mcp` + `invest-calibration-assistant`
  (pip -e). Separate env because natcap.invest's `gdal==3.10.*` pin conflicts with
  `invest-geo`.
- conda `defaults` channels need ToS acceptance — always create envs with
  `-c conda-forge --override-channels` (and `nodefaults` in the yml files).
- **matplotlib Agg is broken in every conda env on this machine** (`savefig` ->
  `0xc06d007f`, a system-DLL issue, not the code). Every plot path therefore
  writes a numpy JSON first and renders the image in a separate process that can
  crash without taking the run down. Images render fine on Workbench/CI/Linux.

## Tool surface — 32 MCP tools (+ 6 resources, 2 prompts)

Env/discovery: `invest_env`, `allow_input_dir`.
Model introspection: `list_invest_models`, `describe_invest_model`,
`validate_invest_args`, `preflight_geo`.
Runs: `run_invest_model`, `get_invest_job`, `get_invest_job_logs`,
`list_invest_jobs`, `cancel_invest_job`, `list_invest_job_artifacts`,
`summarize_results`, `compare_scenarios`.
Data prep (`invest-geo` subprocess unless noted): `scaffold_project` (stdlib),
`project_readiness` (stdlib), `fetch_dem` (Copernicus GLO-30), `fetch_landcover`
(ESA WorldCover), `fetch_climate` (WorldClim precip + Hargreaves ETo),
`fetch_soil` (SoilGrids 2.0: texture / hydrologic soil group / USLE K;
SoilGrids 2017: depth_to_bedrock), `fetch_hydrography` (HydroSHEDS v1
rivers / basins), `reproject_layer`, `clip_to_aoi`, `align_raster_stack`,
`delineate_watersheds` (pygeoprocessing D8), `tables_from_template`
(biophysical-table skeleton from LULC + MODEL_SPEC).
Datastack (stdlib, Workbench round-trip): `import_datastack`, `export_datastack`.
Calibration: `validate_calibration_config`, `run_calibration`,
`get_calibration_job`, `cancel_calibration_job`.

Resources: `invest://models`, `invest://model/{id}/cheatsheet`,
`invest://conventions`, `invest://data-sources`, `invest://coefficients`
(cited-coefficient KB index), `invest://coefficients/{name}` (one parameter file
/ `sources` / `readme` / a worked profile).
Prompts: `prepare_and_run_model`, `compare_land_use_scenarios`.

Reference data (not a tool): `src/invest_mcp/knowledge/coefficients/` — served
by the two `invest://coefficients` resources via the stdlib loader
`knowledge/coefficients.py`. Seven parameter JSONs (`usle_c`, `usle_p`,
`ndr_nutrient`, `curve_number`, `kc`, `root_depth`, `carbon_pools`),
`sources.json` (bibliography: per source citation/doi/url/type/scope/provides/
context/verified), `README.md` (how to choose a value; why not keyed to a
legend), `profiles/moorabool_fs28.json` (the worked FS28 Moorabool
parameterization — explicitly NOT a set of defaults). Legend-agnostic records:
keyed by cover semantics, advisory-only `crosswalk`, every value cited.

Data-prep design guardrail (agreed this session): the MCP is the capability
surface; planning / judgement / LLM-in-the-loop stays in the client. Every tool
is a deterministic, testable transform; reference goes in resources; suggested
workflows go in prompts. See memory `mcp-vs-agent-boundary`.

Conventions: `args` is InVEST's own args dict, absolute paths; the client must NOT
pass `workspace_dir` (server-managed); `required` in a spec may be `True`/`False`
or a **string** (conditional) — the schema only lists `True` as required and
documents conditionals in the description.

## What is verified working (as of 2026-08-30)

- Autodetection of `invest.exe` + `invest-geo`/`invest-cal`; `invest_env`,
  `list_invest_models`, `describe_invest_model('carbon')` -> correct schema.
- `validate_invest_args` = required + sandbox + `invest validate` + geo preflight.
- **Geospatial preflight** end-to-end (.venv -> subprocess -> invest-geo) with
  synthetic data: catches `crs_not_projected`, `crs_units_not_meters`,
  `crs_mismatch`, `no_spatial_overlap`, `nodata_undefined`, `pixel_size_mismatch`;
  clean scenario -> `ok: True`.
- **Full job lifecycle**: submit -> subprocess -> terminal state -> log ->
  `provenance.json` (versions + sha256 of every input). `wait_seconds` uses
  `threading.Event` and blocks until *finalized* (provenance written).
- **Calibration — all 5 models** (AWY, SWY, SDR, NDR_N, NDR_P) end-to-end
  (.venv -> subprocess -> invest-cal -> `invest_calibration_assistant.core.calibrate`)
  against the real `Dummy_InVEST` dataset: SDR 40 LHS iters -> 5.6% err,
  AWY 30 -> 2%, SWY ~3%, NDR_P close, NDR_N loose (short search). Returns
  `best_parameters`, `best_objective`, `obs_vs_sim`, `diagnostics` (Spearman
  sensitivity per param), `warnings`, `dotty_data_<MODEL>.json`.
- **CLI** `invest-mcp {serve,doctor,setup,mcp-config}` verified from a **clean
  clone**: git clone -> venv -> `pip install -e .` -> `doctor` -> `setup --geo`
  (builds invest-geo with the Workbench's micromamba) -> `doctor` green. Fixed 3
  conda-discovery bugs in the process: `condabin\*.BAT` wrappers crash on
  `env create` (0xC0000409) so `_find_conda` returns real exes only (prefers the
  Workbench's bundled `micromamba.exe`); micromamba uses `create` not `env create`
  and drops envs under `%APPDATA%\mamba\envs` -> added to `_conda_env_roots` +
  `setup` falls back to `<conda> run -n <name> python`.
- **Carbon model run** end-to-end (2026-08-30): job
  `carbon-20260830T103940-174b40` -> `succeeded` in ~4 s with `Dummy_InVEST`
  inputs (`INPUTS/LULC/LULC.tif` + a `Carbon_Pools.csv` derived from the
  biophysical table). 5 rasters + `raster_values_summary.csv` (Baseline Carbon
  Storage 4,061,556 t) + `provenance.json`. Closes the last "normal model" gap.
- **`summarize_results`** (tool #17) end-to-end on the Carbon job: per-raster stats
  (valid/nodata, min/max/mean/std/sum, 10-bin histogram), zonal per feature over
  `SubBasin.shp` (reprojected to the raster CRS), surfaces InVEST's
  `raster_values_summary.csv`, NL digest, best-effort preview PNG (isolated
  subprocess `geo/_preview_worker.py`). Cross-check: our raster sum
  (`c_storage_bas.tif` = 4,061,555.98) matches InVEST's CSV total exactly; the 4
  pools sum to the total. Sidecar `<jobdir>/summary/summary.json`.
- **`compare_scenarios`** (tool #18) end-to-end on Carbon: baseline vs a
  deforestation scenario (LULC 10→30, 1717 px). `geo/compare.py` matches output
  rasters by relpath, aligns scenario→baseline grid, writes `diff_<name>.tif` =
  scenario−baseline, delta stats (total before/after, Δ, %Δ, px up/down/same,
  histogram), zonal Δ over an AOI, diverging RdBu_r preview. Δ = −130,285.96 t
  (−3.21%); `delta_min` −75.88 t/ha exact = class-10 minus class-30 pool sums.
- **Data-prep layer (this session)** — all verified end-to-end against
  `Dummy_InVEST` or an Alps bbox:
  - `scaffold_project` — project tree (`data/{raw,processed}`, `tables`,
    `datastacks`, `jobs`, `logs`) + `project.json`; trusts root for the session.
    New write-sandbox `sandbox.resolve_output_path`.
  - `reproject_layer` / `clip_to_aoi` / `align_raster_stack` — `geo/prep.py` ops
    (raster + vector); align grid from a reference raster or crs+res+extent.
  - `delineate_watersheds` — `geo/hydro.py`, full **pygeoprocessing** D8 chain
    (fill_pits → flow_dir_d8 → flow_accum → extract_streams_d8 → snap outlets →
    delineate_watersheds_d8). Same engine InVEST uses. Outlet basin area matched
    flow-accum pixel count exactly.
  - `tables_from_template` — `workspace/biotable.py` + `op=raster_classes` +
    `spec_translate.table_arg_specs`. One row per lucode, InVEST's own column
    headers, blank cells, `[MONTH]`/`[SOIL_GROUP]` expanded, optional legend.
  - `fetch_dem` / `fetch_landcover` — `geo/fetch.py`, public AWS buckets via GDAL
    `/vsicurl/`, no credentials (Copernicus GLO-30 1° tiles; ESA WorldCover 3°
    tiles). Shared `_download_layer` + `reproject_clip_describe`.
  - `fetch_climate` — `geo/climate.py`, WorldClim v2.1 via `/vsizip//vsicurl/`,
    no auth. `precipitation` (prec members) or `eto` (Hargreaves-Samani from
    tmin/tmax/tavg + analytic Ra). monthly (`{month}` placeholder, 12 files) or
    annual (sum). Alps check: 1547 mm/yr precip, ETo Jun 86 / Jan 11 mm/month.
  - `fetch_soil` — `geo/soil.py`, SoilGrids 2.0 (ISRIC, 250 m, no auth) global
    property VRTs read over `/vsicurl/` + `rasterio.vrt.WarpedVRT` (IGH→EPSG:4326)
    windowed to the bbox, then the shared `fetch.reproject_clip_describe` tail.
    `variable`: `texture` (sand/silt/clay % raw, needs `{fraction}` in dst_path) /
    `hydrologic_soil_group` (HSG 1..4=A..D from the USDA 12-class texture triangle
    only — Ksat/depth/water-table ignored; uint8 nodata 0) / `usle_k` (Williams/
    EPIC 1995 pedotransfer from sand/silt/clay/SOC → SI ×0.1317, float32).
    `depth` 0-5cm..100-200cm, `stat` mean/Q0.05/Q0.5/Q0.95. Alps check: HSG mostly
    group B in the valley + nodata on the high massif; K 0.030–0.035 (global range
    0.01–0.07); all 12 USDA texture classes verified. Later added
    `variable=depth_to_bedrock` (SoilGrids **2017** `BDTICM`, a single global
    EPSG:4326 GeoTIFF, cm→mm for AWY `depth_to_root_rest_layer_path`;
    `_read_bdticm` retries the windowed read — the file is non-COG strip-organised
    and truncates intermittently).
  - `fetch_hydrography` — `geo/hydrography.py`, HydroSHEDS v1, no auth.
    `product=rivers` (HydroRIVERS v1.0 lines) / `basins` (HydroBASINS v1c
    standard polygons, Pfafstetter `level` 1..12). Reads the shapefile out of the
    remote zip via GDAL `/vsizip//vsicurl/` + a bbox filter (the `.sbn` index
    keeps the transfer local; `CPL_VSIL_CURL_USE_HEAD=NO` because the server
    blocks HEAD). `region` auto-detected from the AOI centroid vs 9 continental
    envelopes (`af ar as au eu gr na sa si`); an ambiguous centroid errors asking
    for explicit `region=`. `clip_to_aoi` defaults False (whole intersecting
    features). Alps check: 100 rivers / 10 basins@lev9 → EPSG:32632 with real
    HydroSHEDS attributes; region auto → `eu`; GPKG + GeoJSON out.
  - `import_datastack` / `export_datastack` — `workspace/datastack.py` (pure
    stdlib), the declared Workbench integration point. Read / write / build the
    `.invest.json` parameter set (`{args, model_id, invest_version}`); tolerate
    legacy `model_name: natcap.invest.<id>`; `relativize`/`absolutize` path args.
    `export` takes `model_id`+`args` or a `job_id` (lifts them from the job's
    `datastack.json`); `relative=True` for a portable stack. `import` resolves
    the model spec and reports `required_missing` / `files_missing` /
    `paths_outside_sandbox` / `ready`, feeding `validate_invest_args` →
    `run_invest_model`. Verified end-to-end with the real registry: export from a
    finished carbon job → valid stack → re-import → model resolved, ready.
  - `project_readiness` — `workspace/readiness.py`, scans `data/`+`tables/`,
    guesses each file's role by name, matches against each model's required file
    inputs → ready / gaps per model. Name-based & best-effort by design.
  - 4 MCP resources + 2 prompts wired into `build_server()`.
- **Cited-coefficient knowledge base** (2026-08-31) — `knowledge/coefficients/`
  (7 parameter JSONs + `sources.json` + `README.md` + `moorabool_fs28` profile),
  pure-stdlib loader `knowledge/coefficients.py`, two `invest://coefficients`
  resources. 11 tests: every file parses + has core keys; every `source_key`
  used (incl. the profile's `sources_used`) is defined in `sources.json`; every
  bibliography entry carries `context`+`verified`+`scope`; physical sanity
  (C ∈ [0,1]; CN ordered A≤B≤C≤D, ∈ (0,100]; eff / proportion_subsurface_n
  ∈ [0,1]); `index()` lists all 7 with record_count>0; `resources.register`
  wires both URIs; `coefficients_entry("bogus")` returns error JSON with the
  `known` list (does not raise). `pyproject.toml` ships the files as
  package-data.
- 152 tests green (146 pass + 6 skipped: numpy helpers — `_ra_mm_per_day`, USDA
  texture triangle, EPIC K — with numpy absent from `.venv`; verified in
  `invest-geo`). Registered and "Connected" in Claude Code as `invest`.

## Calibration engine — shared core

The engine is a separate repo `github.com/N4W-Facility/Invest_Plugin_Calibration`,
branch `refactor/shared-core` (pushed: 59a6665 SDR, cfd2818 the other 4 models,
d59988b dotty plots). Package `invest_calibration_assistant.core`
(`config`/`engine`/`models/{awy,swy,sdr,ndr}`/`biotable`/`metrics`/`zonal`/
`selection`/`plots`+`_plot_worker`). It was extracted from the user's InVEST
Workbench plugin so BOTH the Workbench plugin adapter and invest-mcp consume one
UI-independent core. Contract: `core.calibrate(config, progress_cb, log) -> result`
and `core.validate_config(config) -> [issues]`. Local clone at
`Y:\Server-UserFolder\Escritorio\Invest_Plugin_Calibration`; `invest-cal`'s
`pip -e` points at it. When the PR merges to `main`, switch
`INVEST_MCP_CAL_PLUGIN_SPEC` / `environment-cal.yml` from `@refactor/shared-core`
to `@main`. invest-mcp side: `calibration/client.py` (CalibrationRunner) +
`calibration/worker.py` (runs in invest-cal). invest-mcp commit 2862287.

Notable core fixes: `Factor_BioTable` float-coerces gated columns (pandas 2.1
`LossySetitemError` on all-int columns); watershed table read via CSV/pyogrio
(dropped simpledbf); best-params selection split out of the `Plot_*` funcs; the 5
`Plot_<MODEL>` funcs became one generic renderer in `core/_plot_worker.py`.

## invest.exe quirks (bundled CLI)

- `invest --help` / `invest <sub> --help` **crash** with `UnicodeEncodeError`
  (cp1252 console). Run with `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8`, capture as
  utf-8. `invest_mcp.invest_cli._env()` does this.
- `invest list` — no `--json`; parse text lines `  <id>  (<alias,alias>)  <Title>`.
- `invest getspec <model> --json` — JSON with `args`, `outputs`,
  `input_field_order`, `validate_spatial_overlap`, `different_projections_ok`,
  `aliases`, `about`, `model_title`, `userguide`.
- `invest validate --json <datastack>` -> `{"validation_results": [[[keys...],
  msg], ...]}`; **exits non-zero** when it finds problems — rely on the JSON.
- Datastack: `{"model_id": "<id>", "args": {...}}`. The key is `model_id`;
  `model_name` makes it `import <id>` and fail.
- `invest run <model> -d <datastack> -w <workspace> --no-report` runs headless
  directly (no `--headless` flag despite the help text).

## Roadmap

**Done since rev 1:** `compare_scenarios` (#18); the data-prep layer (#19–29:
scaffold/readiness/fetch_dem/fetch_landcover/fetch_climate/fetch_soil/reproject/
clip/align/delineate_watersheds/tables_from_template); 4 MCP resources + 2
prompts. **Rev 3→4:** `fetch_hydrography` (#30, HydroSHEDS rivers/basins);
`import_datastack`/`export_datastack` (#31–32, Workbench `.invest.json`
round-trip); `fetch_soil` `variable=depth_to_bedrock` (SoilGrids 2017 BDTICM).
**Rev 4→5:** cited-coefficient knowledge base (`knowledge/coefficients/`, 7
parameters + bibliography + Moorabool profile, two `invest://coefficients`
resources) — roadmap item 2 below now largely done.

**Still open** (CLAUDE.md §6 has the full tagged list):
1. More `fetch_*`: `fetch_soil` PAWC (SoilGrids 2017 `AWCh1..3`/`WWP`) +
   `stat`≠`mean` + HSG refined with Ksat/depth (HYSOGs250m / HiHydroSoil);
   `fetch_climate` `source=terraclimate` (real years, netCDF) / `chirps`
   (tropics); `fetch_dem` `source=srtm|nasadem` (Earthdata token);
   `fetch_hydrography` HydroBASINS lakes / nested subwatersheds.
2. Extend the cited-coefficient KB: NDR `eff`/`crit_len` outside semi-arid
   tropics, USLE C/P for more regions/biomes, per-output units, a glossary;
   `check_table_vs_raster` `[tool]` cross-checking a filled table against it.
3. `clone_job` (copy a finished run's args, re-run with edits — feeds
   `compare_scenarios`); datastack "archive" (`.invest.tar.gz`).
4. Output side: `compare_scenarios_multi`, `aggregate_to_units`, `build_report`
   (methods+results memo), `export_map`, `run_uncertainty` (MC over coefficients).
5. Content-addressed run cache by input hash; JSON Schema snapshots + CI diff for
   InVEST version bumps; `estimate_run_cost`; `manage_workspaces`.
6. `conda-lock` (win-64/linux-64/osx-arm64) + Docker (Linux, `invest` from
   conda-forge, no Workbench dependency).
7. Calibration extensions: validation split / spatial CV, multi-gauge,
   multi-objective (Pareto), GLUE/DREAM uncertainty, regionalization.
8. Merge the plugin PR to `main`, then flip `INVEST_MCP_CAL_PLUGIN_SPEC` /
   `environment-cal.yml` off `@refactor/shared-core`.
9. Fine-tuning: chunking in `geo/prep.py` / `geo/fetch.py` / `geo/compare.py` for
   huge rasters (they read whole bands / mosaic in memory); populate
   `project.json`'s `datasets: []` from the fetch/prep routines; nested
   subwatersheds (`pygeoprocessing.routing.calculate_subwatershed_boundary`).

## Tooling meta (this session, 2026-08-30)

- claude-mem v13.18.0 installed today; captures sessions from now on. Backfill of
  pre-install transcripts (Aug 29-30, ~7 MB) did NOT run — only this snapshot and
  future sessions are in claude-mem.
- `uv` 0.12.7 installed via winget to fix claude-mem's semantic search / Chroma
  sync (`uvx` was missing). Lives at
  `%LOCALAPPDATA%\Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe`,
  added to the persisted user PATH. Restart Claude Code so the hooks pick it up.
- Built-in per-project memory at
  `C:\Users\Nogales\.claude\projects\Y--Server-UserFolder-Escritorio-MCP-InVEST\memory\`
  is separate from claude-mem and already reflects the project state.
