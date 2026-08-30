# invest-mcp

An **MCP server** that lets an AI assistant (Claude Code, Claude Desktop, …)
drive **Natural Capital Project [InVEST](https://naturalcapitalproject.stanford.edu/software/invest)**
models through natural language: browse models, understand their inputs,
validate a parameter set, run a model, and inspect the outputs.

## How it works

```
Claude Code  ──stdio/JSON-RPC──▶  invest_mcp.server
                                     │
                                     ├─ models/      MODEL_SPEC ▶ JSON Schema + briefing
                                     ├─ execution/   job store + subprocess supervisor
                                     ├─ workspace/   input path sandbox + output catalog
                                     ├─ geo/         geospatial preflight (CRS/overlap/pixel)
                                     └─ provenance   per-run reproducibility manifest
                                     │
                          ┌──────────┴───────────┐
                          ▼                      ▼
              invest.exe (InVEST Workbench)   invest-geo conda env
                  model runs                  GDAL/rasterio/pyproj
```

The server never imports `natcap.invest`. It shells out to the `invest`
command-line tool, one OS process per model run. That keeps GDAL crashes
contained, makes cancellation a real kill, and means **no conda/GDAL setup is
required for running models** as long as the InVEST Workbench is installed.

Anything that needs GDAL directly — currently the **geospatial preflight**, later
the data-prep routines — runs in a separate `invest-geo` conda env, also invoked
as a subprocess. It is optional: without it, `preflight_geo` reports "env missing"
and `validate_invest_args` marks the geo section as skipped.

## Requirements

* Python ≥ 3.10 for the server itself (only `mcp`, `pydantic`, `pydantic-settings`).
* An `invest` executable — automatically found if the **InVEST Workbench** is
  installed, otherwise set `INVEST_MCP_INVEST_EXE`.

## Install

```powershell
# 1. the server itself (light: mcp + pydantic only)
python -m venv .venv
.\.venv\Scripts\pip install -e .

# 2. (optional) the geospatial sidecar env, for preflight_geo
conda env create -f environment.yml
conda run -n invest-geo pip install -e . --no-deps
```

## Register with Claude Code

```powershell
claude mcp add invest -- "Y:\Server-UserFolder\Escritorio\MCP_InVEST\.venv\Scripts\python.exe" -m invest_mcp
```

Then in a session: *"list the InVEST models"*, *"describe the carbon model"*,
*"run carbon with lulc = … and the pools table = …"*.

## Configuration

All optional — see `.env.example`. Key ones:

| Variable | Meaning |
|---|---|
| `INVEST_MCP_INVEST_EXE` | Path to `invest.exe` (auto-detected otherwise). |
| `INVEST_MCP_GEO_PYTHON` | `python.exe` of the `invest-geo` env (auto-detected otherwise). |
| `INVEST_MCP_DATA_ROOT` | Where job workspaces/logs go. Default `%USERPROFILE%\invest-mcp-data`. |
| `INVEST_MCP_ALLOWED_INPUT_DIRS` | Extra folders the server may read inputs from (`;`-separated). |
| `INVEST_MCP_MAX_CONCURRENT_JOBS` | Parallel runs (default 2). |

The data root and the server's working directory are always readable. Any other
input path is rejected until you trust its folder — at runtime with the
`allow_input_dir` tool, or permanently via `INVEST_MCP_ALLOWED_INPUT_DIRS`.

## Tools (v0.1)

| Tool | Purpose |
|---|---|
| `invest_env` | Confirm InVEST is reachable; show paths/version. |
| `allow_input_dir` | Trust an extra input folder for this session. |
| `list_invest_models` | All models: id, aliases, title. |
| `describe_invest_model` | Briefing + `args` JSON Schema + inputs/outputs. |
| `validate_invest_args` | Required-field + sandbox + `invest validate` + geo preflight. |
| `preflight_geo` | Deep geospatial check: CRS defined/projected, layer overlap, pixel-size sanity. |
| `run_invest_model` | Start a run; returns `job_id`. |
| `get_invest_job` | Poll status; artifact summary + failure log tail. |
| `get_invest_job_logs` | Captured stdout/stderr of a run. |
| `list_invest_jobs` | Recent runs. |
| `cancel_invest_job` | Kill a queued/running run. |
| `list_invest_job_artifacts` | Catalog every output file. |

## Roadmap

* ~~Geospatial preflight (CRS/extent/pixel/nodata lint)~~ — done (`preflight_geo`).
* Result summarisation: zonal stats over an AOI + raster previews.
* `compare_scenarios` (baseline vs alternative).
* Data-prep routines (download DEM/land cover, reproject, clip, align) exposed
  as deterministic tools + guided prompts.
* Content-addressed run cache; schema snapshots diffed in CI.

## Tests

```powershell
.\.venv\Scripts\pip install -e ".[dev]"
.\.venv\Scripts\pytest -q
```
