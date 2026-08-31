# invest-mcp

An **[MCP](https://modelcontextprotocol.io) server** that lets an AI assistant
drive **Natural Capital Project
[InVEST](https://naturalcapitalproject.stanford.edu/software/invest)** models
through natural language: browse models, understand and validate their inputs,
run them on data from disk, check coordinate systems, calibrate the hydrological
models against observations, and inspect the outputs.

It is a plain MCP server — **not tied to any one AI client**. Works with Claude
Desktop / Claude Code, Cline, Continue, Cursor, Zed, LibreChat, `mcphost` for
Ollama, the OpenAI Agents SDK, or anything else that speaks MCP.

## How it works

```
 any MCP client ──stdio | http──▶  invest_mcp.server
                                     │  (light: mcp + pydantic only; never imports natcap.invest)
                                     ├─ models/        MODEL_SPEC ▶ JSON Schema + briefing
                                     ├─ execution/     job store + one-process-per-run supervisor
                                     ├─ workspace/     input-path sandbox + output catalog
                                     ├─ geo/           geospatial preflight (CRS / overlap / pixel)
                                     ├─ calibration/   spotpy calibration (shared engine)
                                     └─ provenance     per-run reproducibility manifest
                                     │
                   ┌─────────────────┼───────────────────┬────────────────────┐
                   ▼                 ▼                    ▼                    ▼
           invest.exe          invest-geo env       invest-cal env        JobStore
        (InVEST Workbench)   GDAL/rasterio/pyproj   natcap.invest+spotpy  (persistent)
```

The server shells out to the `invest` command line, one OS process per run — GDAL
crashes stay contained, cancellation is a real kill, and **running models needs
no conda/GDAL setup** as long as the InVEST Workbench is installed. The
`invest-geo` (preflight) and `invest-cal` (calibration) conda envs are **optional
sidecars**; without them those tools report "env missing" and everything else
still works.

## Install

- **Beginners / from-zero on Windows:** step-by-step guide (Spanish) →
  [docs/INSTALACION-PASO-A-PASO.md](docs/INSTALACION-PASO-A-PASO.md).
- **One-shot script:** from a clone, run
  `powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1`
  (or `scripts/bootstrap.sh` on POSIX). It picks the server env (a `.venv` if you
  have a system Python ≥ 3.10, else a **conda `invest-mcp` env** — no Python to
  install; `-CondaServer` forces it), installs the package, builds the
  `invest-geo` / `invest-cal` conda envs, runs `doctor`, prints the client config.
- **Full reference** (every client's snippet, security notes): [INSTALL.md](INSTALL.md).

Short version by hand:

```bash
git clone https://github.com/nogales02/invest-mcp && cd invest-mcp
# server env -- pick one:
python -m venv .venv && .venv/Scripts/pip install -e .          # A) system Python
micromamba create -f environment-server.yml -y && \
  micromamba run -n invest-mcp pip install -e .                 # B) no system Python
invest-mcp setup      # (optional) build the invest-geo + invest-cal conda envs
invest-mcp doctor     # check what's wired
invest-mcp mcp-config # print the block to paste into your client
```

> A bare `pip install git+…` works for browse/validate/run but **not** for
> `invest-mcp setup` (it needs the repo's `environment-*.yml`) — clone for that.

`invest-mcp` with no subcommand runs the server over stdio. `invest-mcp serve
--transport streamable-http --port 8000` runs a shared HTTP server (needs the
`[http]` extra).

## Configuration

All optional (`INVEST_MCP_*` env vars or a `.env`) — see `.env.example` and the
table in [INSTALL.md](INSTALL.md#6-configuration-reference-invest_mcp_). The input
**allow-list** (default: data root + working dir; widen with
`INVEST_MCP_ALLOWED_INPUT_DIRS` or the `allow_input_dir` tool) is the key safety
boundary — never point it at a drive root.

## Tools

> **Full manual** (every tool, resource and prompt — signature, parameters,
> return fields, example, gotchas; plus base concepts, the typical flow, a
> cookbook and a troubleshooting table; Spanish):
> [docs/MANUAL-HERRAMIENTAS.md](docs/MANUAL-HERRAMIENTAS.md).

| Tool | Purpose |
|---|---|
| `invest_env` | Confirm InVEST is reachable; show paths/version and which sidecars are available. |
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
| `summarize_results` | Per-raster stats + per-feature zonal stats over an AOI + InVEST's own totals + a natural-language digest + a preview PNG. |
| `validate_calibration_config` | Check a calibration config (params, observed columns, `Status_Cal_*`, sandbox). |
| `run_calibration` | Calibrate AWY / SWY / SDR / NDR_N / NDR_P (spotpy DDS/LHS/SCE-UA); returns `job_id`. |
| `get_calibration_job` | Best parameters, objective, observed-vs-simulated, per-parameter diagnostics, dotty-plot data. |
| `cancel_calibration_job` | Kill a calibration job. |

## Roadmap

* ~~Geospatial preflight~~ · ~~model calibration (5 hydro models)~~ ·
  ~~end-to-end carbon run~~ · ~~`summarize_results` (per-raster + zonal stats + preview)~~ — done.
* `compare_scenarios` (baseline vs alternative).
* Data-prep routines (fetch DEM/land cover, reproject, clip, align) as deterministic tools + guided prompts.
* Content-addressed run cache; schema snapshots diffed in CI.

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

The calibration engine lives in a separate repo
([Invest_Plugin_Calibration](https://github.com/N4W-Facility/Invest_Plugin_Calibration),
branch `refactor/shared-core`) and is shared with the InVEST Workbench
"Calibration Assistant" plugin.
