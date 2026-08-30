# Installing invest-mcp

> **New to this / on Windows from zero?** Follow the hand-held, step-by-step
> guide instead: [docs/INSTALACION-PASO-A-PASO.md](docs/INSTALACION-PASO-A-PASO.md)
> (Spanish). It covers installing Python and the InVEST Workbench, the
> `scripts/bootstrap.ps1` one-shot installer, wiring Claude Desktop / Claude
> Code, and a first test run. This file is the terse reference.

`invest-mcp` is a standard [MCP](https://modelcontextprotocol.io) server. It has
no dependency on any particular AI client — anything that speaks MCP can use it
(Claude Desktop / Claude Code, Cline, Continue, Cursor, Zed, LibreChat, `mcphost`
for Ollama, the OpenAI Agents SDK, …).

There is nothing to "activate" inside InVEST. The server talks to the InVEST
command line (`invest.exe`, bundled with the **InVEST Workbench**) as a
subprocess. Your only setup is: install this package, optionally create two conda
environments for the geospatial/calibration features, and register the server
with your client.

---

## 1. Prerequisites

| For | You need |
|-----|----------|
| Model browse / validate / **run** | The **InVEST Workbench** installed (any recent version), or an `invest` on `PATH` (e.g. conda-forge `natcap.invest` on Linux/macOS). |
| `preflight_geo` | A `conda` / `mamba` / `micromamba` (Miniforge recommended). |
| `run_calibration` | Same conda tool. |
| `--transport streamable-http` | `pip install "invest-mcp[http] @ git+…"` (adds uvicorn). |

Python ≥ 3.10 for the server itself.

---

## 2. Install the server (from git — no PyPI)

**Option 0 — the one-shot script (clone first):** does §2 + §3 + §4 and prints §5.

```bash
git clone https://github.com/nogales02/invest-mcp
cd invest-mcp
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1   # Windows
./scripts/bootstrap.sh                                           # POSIX
#   flags: -SkipEnvs / --skip-envs, -GeoOnly, -CalOnly, -Conda <path>, -Http
```

**Option A — pip from the repo** (browse/validate/run only; `setup` won't work
without the repo's `environment-*.yml`):

```bash
pip install "git+https://github.com/nogales02/invest-mcp"
```

**Option B — clone by hand (lets you edit / update with `git pull`):**

```bash
git clone https://github.com/nogales02/invest-mcp
cd invest-mcp
python -m venv .venv
# Windows:  .venv\Scripts\pip install -e .
# POSIX:    .venv/bin/pip install -e .
```

Any of these gives you an `invest-mcp` command and `python -m invest_mcp`.

---

## 3. (optional) Create the geospatial + calibration environments

These carry GDAL / `natcap.invest` / `spotpy` and can't be `pip install`ed
cleanly on Windows, so they live in their own conda envs (`invest-geo`,
`invest-cal`). One command builds them from the repo's `environment-*.yml`:

```bash
invest-mcp setup            # both envs
invest-mcp setup --geo      # just preflight_geo
invest-mcp setup --cal      # just calibration
```

**What `setup` uses to build them**, in order: `CONDA_EXE` / `MAMBA_EXE`, a
`micromamba` on `PATH`, **the `micromamba.exe` that the InVEST Workbench bundles**
(so if you have the Workbench you usually need nothing else), or a real
`conda`/`mamba` executable under a detected install. It never uses a
`condabin\*.bat`/`.cmd` shim (those crash on `env create` on some Windows
setups). Force one with `--conda <path>`.

**Where the envs land:** conda/mamba put them under `<conda-root>/envs/`;
**micromamba puts them under `%APPDATA%\mamba\envs\`** (Windows) or
`~/micromamba/envs/` (POSIX). Either way `invest-mcp doctor` and the server find
them automatically. If a nonstandard location isn't picked up, point
`INVEST_MCP_GEO_PYTHON` / `INVEST_MCP_CAL_PYTHON` at the env's `python`
(`invest-mcp setup` prints the exact path it used).

**Manual equivalent** (if you'd rather not use `setup`):

```bash
conda env create -f environment-geo.yml          # or: micromamba create -f environment-geo.yml -y
conda run -n invest-geo  pip install -e . --no-deps
conda env create -f environment-cal.yml
conda run -n invest-cal  pip install "spotpy>=1.6.2" "invest-calibration-assistant @ git+https://github.com/N4W-Facility/Invest_Plugin_Calibration.git@refactor/shared-core"
conda run -n invest-cal  pip install -e . --no-deps
```

The calibration engine is the shared core of the InVEST Workbench "Calibration
Assistant" plugin; `setup --cal` pip-installs it. Override the source with
`--plugin-spec` or `INVEST_MCP_CAL_PLUGIN_SPEC` (e.g. point at a fork or a local
path) until it is merged upstream.

`setup` is idempotent: re-run it after a `git pull`. If an env already exists the
`create` step is a no-op and it just refreshes the `pip install -e .` into it.

---

## 4. Check the install

```bash
invest-mcp doctor
```

```
[ok]   invest      C:\Program Files\InVEST 3.20.1 Workbench\...\invest.exe  (v3.20.1)
[ok]   invest-geo  C:\Users\you\AppData\Roaming\mamba\envs\invest-geo\python.exe
[ok]   invest-cal  C:\Users\you\.conda\envs\invest-cal\python.exe
[ok]   data root   C:\Users\you\invest-mcp-data
all good
```

`[warn]` for `invest-geo` / `invest-cal` just means those features are off until
you run `invest-mcp setup` — the exit code is driven only by `invest`.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `doctor` shows `invest [FAIL]` | Install the InVEST Workbench, or set `INVEST_MCP_INVEST_EXE` to an `invest` / `invest.exe`. |
| `setup`: *No conda/mamba/micromamba found* | Install [Miniforge](https://github.com/conda-forge/miniforge) or micromamba, or `--conda <path>`. If the InVEST Workbench is installed its bundled micromamba is used automatically. |
| `setup` created an env but `doctor` still says *not found* | The env is in a spot detection missed. `setup` prints the path it used — set `INVEST_MCP_GEO_PYTHON` / `INVEST_MCP_CAL_PYTHON` to it. |
| `setup` fails with a `condabin\*.bat` crash / `Could not open lockfile` | Old `invest-mcp`; update. It now avoids the `.bat` shims. Or `--conda` a real `conda.exe` / `micromamba.exe`. |
| calibration dotty-plot `.jpg` never appears | Known: some headless conda matplotlib builds crash in the Agg backend. The machine-readable `FIGURES/dotty_data_<MODEL>.json` is always written; the run is unaffected. |

---

## 5. Register with your client

`invest-mcp mcp-config` prints a ready-to-use block.

### Claude Desktop — `claude_desktop_config.json`

```jsonc
{
  "mcpServers": {
    "invest": {
      "command": "/abs/path/to/python",         // the venv python, or just "python"
      "args": ["-m", "invest_mcp"],
      "env": {
        "INVEST_MCP_INVEST_EXE": "C:\\Program Files\\InVEST 3.20.1 Workbench\\resources\\invest\\invest.exe"
      }
    }
  }
}
```

`INVEST_MCP_INVEST_EXE` is optional if the Workbench is auto-detected. Restart the app.

### Claude Code

```bash
invest-mcp mcp-config --client claude-code   # prints the exact command
# e.g.
claude mcp add invest --scope user -- /abs/path/to/python -m invest_mcp
```

### Cline / Continue / Cursor / Zed / LibreChat

Same shape as the Claude Desktop block, in that client's MCP settings
(`command` + `args` + optional `env`).

### Ollama

Ollama has no native MCP; use a bridge that does
([`mcphost`](https://github.com/mark3labs/mcphost), `oterm`, LibreChat, …). Point
the bridge's MCP config at `command: python`, `args: ["-m", "invest_mcp"]`.

### OpenAI Agents SDK / any HTTP client

Run a shared server and connect over HTTP:

```bash
pip install "invest-mcp[http] @ git+https://github.com/nogales02/invest-mcp"
invest-mcp serve --transport streamable-http --host 0.0.0.0 --port 8000
# MCP endpoint:  http://<host>:8000/mcp
```

---

## 6. Configuration reference (`INVEST_MCP_*`)

All optional; set as env vars or in a `.env` next to where you launch the server.
See `.env.example`.

| Variable | Default |
|----------|---------|
| `INVEST_MCP_INVEST_EXE` | auto-detected InVEST Workbench |
| `INVEST_MCP_GEO_PYTHON` | auto-detected `invest-geo` env |
| `INVEST_MCP_CAL_PYTHON` | auto-detected `invest-cal` env |
| `INVEST_MCP_DATA_ROOT` | `~/invest-mcp-data` (jobs, logs, provenance) |
| `INVEST_MCP_ALLOWED_INPUT_DIRS` | — (see **Security**) |
| `INVEST_MCP_MAX_CONCURRENT_JOBS` | `2` |
| `INVEST_MCP_INVEST_TIMEOUT_SECONDS` | `21600` |
| `INVEST_MCP_TRANSPORT` / `_HOST` / `_PORT` | `stdio` / `127.0.0.1` / `8000` |
| `INVEST_MCP_LOCALE` | — (`es` / `en` / `zh`) |

---

## Security

The server reads whatever files a model run references. It only accepts input
paths under an **allow-list**: by default the data root and the working
directory. Widen it with `INVEST_MCP_ALLOWED_INPUT_DIRS` (`;`-separated on
Windows, `:` on POSIX) or, per session, the `allow_input_dir` tool.

**Never** point the allow-list at a drive root (`C:\`, `/`). Runs execute as
subprocesses with a clean environment, a timeout and a hard job limit; the
server itself runs no arbitrary code and makes no network calls beyond `invest`
and (during `setup`) conda.
