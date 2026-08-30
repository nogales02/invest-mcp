# CLAUDE.md — invest-mcp

Contexto para retomar el proyecto en futuras sesiones. Léelo entero antes de trabajar.

---

## 1. Qué es esto y para qué

Un **servidor MCP** que permite a un asistente IA (Claude Code, Claude Desktop)
manejar los modelos de **InVEST** (Natural Capital Project, Stanford) por lenguaje
natural: explorar modelos, entender sus entradas, validar parámetros, ejecutar un
modelo tomando datos del disco del usuario, y revisar/interpretar las salidas.

**Visión a medio plazo:** además de los "verbos" para ejecutar InVEST, dar a la IA
una base de **rutinas** — deterministas (descargar DEM/land cover, reproyectar,
recortar, alinear rásters, construir tablas biofísicas) y de procedimiento
(playbooks guiados) — para que pueda preparar un caso de estudio de principio a fin.

**El MCP NO es un plugin del Workbench.** Es un hermano: ambos usan el mismo core
`natcap.invest`. El punto de integración con el Workbench es el formato **datastack**
(`.invest.json`), que este servidor lee y escribe.

---

## 2. Decisión de arquitectura central (v0.1)

El servidor es un **wrapper de subproceso puro** sobre el `invest.exe` que trae
instalado el **InVEST Workbench**. **No importa `natcap.invest`.**

Motivos:
- No hay env de conda configurado en la máquina (sí está `C:\ProgramData\miniconda3`
  pero sin ambiente creado).
- El Python del sistema es **3.14** y no hay wheels de GDAL para 3.14/Windows, así
  que `pip install natcap.invest` es **imposible** (pin `gdal==3.10.*` sin wheel).
- El aislamiento por subproceso contiene segfaults de GDAL y hace que cancelar un
  run sea un kill real.

Un proceso del SO por run, supervisado por un hilo daemon + un `BoundedSemaphore`
para limitar concurrencia. Los jobs persisten en
`%USERPROFILE%\invest-mcp-data\jobs\<job_id>\`.

### Hechos del entorno (verificados 2026-08-29)
- `invest.exe`: `C:\Program Files\InVEST 3.20.1 Workbench\resources\invest\invest.exe` (v3.20.1)
- SDK: **`mcp` 2.1.1** — OJO: `FastMCP` se renombró a `MCPServer`
  (`from mcp.server.mcpserver import MCPServer`). Los decoradores (`.tool()`,
  `.resource()`) son iguales que en FastMCP v1.
- El servidor corre en `.venv` (Python 3.14) con solo `mcp`, `pydantic`,
  `pydantic-settings`.
- **Env geoespacial**: `invest-geo` (conda-forge, Python 3.12) en
  `C:\Users\Nogales\.conda\envs\invest-geo`. Trae gdal 3.13, rasterio, pyproj,
  shapely, pyogrio, numpy, pygeoprocessing 2.4. `invest_mcp` instalado ahí con
  `pip install -e . --no-deps`. Creado con
  `conda create -n invest-geo -c conda-forge --override-channels ...` (los canales
  `defaults` de Anaconda piden aceptar ToS — usar siempre `-c conda-forge
  --override-channels`, y `nodefaults` en environment-geo.yml / environment-cal.yml).
- **Env de calibración**: `invest-cal` (conda-forge, Python 3.12) en
  `C:\Users\Nogales\.conda\envs\invest-cal`. Trae **natcap.invest 3.20.1**,
  gdal 3.12, geopandas, rasterstats, matplotlib-base, pandas + `spotpy` (pip) +
  `invest_mcp` y `invest-calibration-assistant` (pip -e, apunta a
  `Y:\Server-UserFolder\Escritorio\Invest_Plugin_Calibration`). No se pudo
  añadir natcap.invest a `invest-geo` (choca el pin `gdal==3.10.*`), de ahí el
  env aparte.
- **matplotlib está roto en las envs conda de esta máquina** (Agg crashea en
  `savefig` con `0xc06d007f`, un lío de DLLs nativas del sistema, no del código).
  Por eso el core: (a) siempre escribe `FIGURES/dotty_data_<MODELO>.json` (numpy
  puro), (b) intenta el JPG en un **subproceso aparte** (`core/_plot_worker.py`)
  que si crashea no tumba la calibración. Los JPG sí salen donde matplotlib
  funcione (Workbench, CI, Linux).
- Registrado en Claude Code, scope local (`C:\Users\Nogales\.claude.json`), nombre `invest`.
- El servidor (.venv) llama al worker geo por subproceso:
  `<invest-geo>\python.exe -m invest_mcp.geo.preflight` con `GDAL_DATA` / `PROJ_DATA`
  / `PATH`(Library\bin) inyectados por `config.geo_subprocess_env()`.

---

## 3. Estructura del repo

```
src/invest_mcp/
  config.py          Settings (env INVEST_MCP_*) + autodetección de invest.exe
  invest_cli.py      subproceso: version / list / getspec / validate  (fuerza UTF-8)
  models/
    registry.py      lista modelos, cachea specs (lru_cache), resuelve alias
    spec_translate.py  MODEL_SPEC -> JSON Schema del `args` + briefing markdown
  execution/
    jobs.py          Job (dataclass) + JobStore persistente (job.json por job)
    runner.py        lanza `invest run`, semáforo, cancel (taskkill /T), wait (Event)
  workspace/
    sandbox.py       allow-list de rutas de entrada (+ allow_dir de sesión)
    artifacts.py     catálogo de ficheros de salida (path, kind, size)
  geo/
    client.py        (server-side) build_payload + subproceso al env invest-geo
    preflight.py     (CORRE EN invest-geo) lee JSON de stdin, chequea CRS/overlap/pixel
  calibration/
    client.py        (server-side) CalibrationRunner: job + subproceso al env invest-cal
    worker.py        (CORRE EN invest-cal) lee JSON, llama invest_calibration_assistant.core
  provenance.py      provenance.json por run: versiones + sha256 de cada input
  tools.py           las 16 tools MCP + register(server)
  server.py          build_server() -> MCPServer; main() corre stdio
tests/               test_spec_translate.py, test_sandbox.py, test_geo_payload.py  (13 tests)
```

`geo/preflight.py` **solo importa stdlib al cargar**; rasterio/pyproj/shapely/
pyogrio se importan dentro de las funciones (para que el env `.venv` pueda
importar el módulo sin GDAL, aunque nunca lo ejecuta).

---

## 4. Tool surface (v0.1)

| Tool | Para qué |
|---|---|
| `invest_env` | Confirmar que InVEST es alcanzable; rutas y versión. |
| `allow_input_dir(path)` | Confiar una carpeta extra como origen de inputs (solo la sesión). |
| `list_invest_models` | Todos los modelos: id, alias, título. |
| `describe_invest_model(model_id)` | Briefing + JSON Schema de `args` + inputs/outputs. |
| `validate_invest_args(model_id, args)` | required + sandbox + `invest validate` + preflight geo. |
| `preflight_geo(model_id, args)` | Chequeo geoespacial profundo: CRS definido/proyectado, solapamiento de capas, cordura de tamaño de píxel. Necesita el env `invest-geo`. |
| `run_invest_model(model_id, args, wait_seconds=0)` | Arranca un run; devuelve `job_id`. |
| `get_invest_job(job_id)` | Estado; al terminar, resumen de artefactos y (si falla) cola del log. |
| `get_invest_job_logs(job_id, tail_lines=200)` | stdout/stderr capturado. |
| `list_invest_jobs(limit=20)` | Runs recientes. |
| `cancel_invest_job(job_id)` | Mata un run en cola o en marcha. |
| `list_invest_job_artifacts(job_id)` | Catálogo de todos los ficheros de salida. |
| `validate_calibration_config(config)` | Chequea una config de calibración (modelo/params/objetivo, columnas de Obs_Data, flags `Status_Cal_*`, caps de factores, sandbox). |
| `run_calibration(model, parameters, objective, optimizer, observed_data_path, model_inputs, ...)` | Job de calibración (spotpy DDS/LHS/SCE-UA sobre InVEST). Devuelve `job_id`. Modelos: **AWY, SWY, SDR, NDR_N, NDR_P**. |
| `get_calibration_job(job_id)` | Estado + iteraciones; al terminar: best params, objetivo, obs-vs-sim, diagnostics. |
| `cancel_calibration_job(job_id)` | Mata un job de calibración. |

Convenciones:
- `args` es el dict de args de InVEST tal cual; **rutas absolutas**.
- El cliente NO debe pasar `workspace_dir` (lo gestiona el servidor).
- `required` en el spec puede ser `True`/`False` o **un string** (condicional, p.ej.
  `"do_valuation"`); el schema solo mete en `required` los `True` y documenta los
  condicionales en la `description`.

---

## 5. Estado actual

### Funciona / verificado
- Autodetección de `invest.exe` y del env `invest-geo`; `invest_env`, `list_invest_models`.
- `describe_invest_model('carbon')` → schema correcto.
- `validate_invest_args` → required + sandbox + `invest validate` + preflight geo.
- **Preflight geoespacial** (`preflight_geo`) probado end-to-end (.venv → subproceso
  → env invest-geo) con datos sintéticos: detecta `crs_not_projected`,
  `crs_units_not_meters`, `crs_mismatch` (error si `different_projections_ok=False`),
  `no_spatial_overlap`, `nodata_undefined`, `pixel_size_mismatch`. Escenario limpio → `ok: True`.
- **Ciclo de job completo**: submit → subproceso → estado terminal → log → `provenance.json`.
  Probado con un run que falla a propósito.
- **Calibración (SDR)**: probado end-to-end (.venv → subproceso → env invest-cal →
  `invest_calibration_assistant.core.calibrate`) contra `Dummy_InVEST`: 10 iter LHS de
  InVEST SDR + run best + diagnostics, ~18s. El motor es el **núcleo compartido**
  extraído del plugin del Workbench del usuario (repo `Invest_Plugin_Calibration`,
  rama `refactor/shared-core`, patch en scratchpad `shared-core.patch`). Solo SDR
  cableado; AWY/SWY/NDR_N/NDR_P siguen en la ruta legacy del plugin hasta portarlos.
- `wait_seconds` bloquea hasta que el job está *finalizado* (provenance escrita),
  no solo hasta que cambia el estado (se arregló una race con `threading.Event`).
- 13 tests en verde.
- Registrado y "Connected" en Claude Code.

### Pendiente
- **Run exitoso de punta a punta**: no hay sample data de InVEST en la máquina.
  Falta probar con un LULC + tabla de carbon pools reales.
- Resources y prompts MCP (v0.1 solo tiene tools).

---

## 6. Roadmap (capas siguientes, en orden sugerido)

1. ~~Env de conda con GDAL~~ **HECHO** — env `invest-geo`.
2. ~~Preflight geoespacial~~ **HECHO** — `geo/preflight.py` + tool `preflight_geo`.
   Pendiente de afinar: umbrales (`_PIXEL_RATIO_WARN`), y probarlo con datos reales
   de un caso de estudio (hasta ahora solo sintéticos).
3. **`summarize_results`**: zonal stats de los rásters de salida sobre un AOI +
   resumen en lenguaje natural + PNG de preview + sidecar JSON de estadísticas
   (para que la IA "vea" el resultado).
4. **`compare_scenarios`**: correr baseline vs alternativa, diferencia de salidas.
   Es el propósito de InVEST (Tradeoffs).
5. **Rutinas de datos deterministas** (tools): `project.scaffold`, `geo.fetch_dem`,
   `geo.fetch_landcover`, `geo.reproject`, `geo.clip_to_aoi`, `geo.align_stack`,
   `tables.from_template`. Idempotentes, con hash de contenido, log. Van en el env
   `invest-geo` (reproyección/recorte con rasterio/pygeoprocessing), invocadas por
   subproceso igual que `preflight` — el patrón ya está montado en `geo/client.py`.
6. **Playbooks** (prompts MCP): "preparar+correr NDR", "preparar+correr Carbon",
   "comparar dos escenarios de uso de suelo".
7. **Base de conocimiento** (resources): catálogo de fuentes de datos por variable,
   convención de carpetas, unidades, cheat-sheets por modelo.
8. **Cache content-addressed** por hash de inputs; **snapshots de JSON Schema** en
   el repo, diff en CI para detectar cambios breaking de spec al subir versión de InVEST.

### Convención de "proyecto InVEST" (a definir antes de las rutinas de datos)
```
{project}/
  project.json            AOI, CRS objetivo, metadatos, índice de datasets
  data/raw/               lo descargado, inmutable
  data/processed/         reproyectado/recortado/alineado
  tables/                 CSVs biofísicas, lookup
  datastacks/             {model}.invest.json
  jobs/{job_id}/workspace/  salidas de InVEST + provenance.json
  logs/
```
`raw/` inmutable + `processed/` derivado → trazabilidad y cache.

---

## 7. Quirks del `invest.exe` bundled (importante)

- `invest --help` y `invest <sub> --help` **crashean** con `UnicodeEncodeError`
  (consola cp1252). Hay que ejecutar con `PYTHONUTF8=1` y `PYTHONIOENCODING=utf-8`
  y capturar salida como utf-8. Lo hace `invest_cli._env()`.
- `invest list` — sin `--json`; se parsea texto: `  <id>  (<alias,alias>)  <Título>`.
- `invest getspec <model> --json` — JSON con `args`, `outputs`, `input_field_order`,
  `validate_spatial_overlap`, `different_projections_ok`, `aliases`, `about`,
  `model_title`, `userguide`.
- `invest validate --json <datastack>` → `{"validation_results": [[[keys...], msg], ...]}`;
  **exit code ≠ 0** cuando hay problemas de validación (no es fallo del CLI, hay que
  fiarse del JSON).
- Datastack: `{"model_id": "<id>", "args": {...}}`. La clave es `model_id`; con
  `model_name` intenta `import <id>` y falla.
- `invest run <model> -d <datastack> -w <workspace> --no-report` corre headless
  directamente (no hace falta `--headless` pese a lo que diga el help).

---

## 8. Comandos

```powershell
# instalar / actualizar el servidor
.\.venv\Scripts\pip install -e ".[dev]"

# tests
.\.venv\Scripts\pytest -q

# CLI: subcomandos serve / doctor / setup / mcp-config
.\.venv\Scripts\python -m invest_mcp doctor
.\.venv\Scripts\python -m invest_mcp mcp-config --client claude-code

# envs conda (crea invest-geo + invest-cal desde environment-*.yml + pip installs)
.\.venv\Scripts\python -m invest_mcp setup           # ambos
.\.venv\Scripts\python -m invest_mcp setup --geo     # solo uno
# manual (equivalente): conda env create -f environment-geo.yml ; luego pip install -e . --no-deps en ese env
# probar el worker geo:  echo '{"spatial_inputs":[]}' | <invest-geo>\python.exe -m invest_mcp.geo.preflight

# registrar en Claude Code (ya hecho, scope local)
claude mcp add invest --scope local -- "Y:\Server-UserFolder\Escritorio\MCP_InVEST\.venv\Scripts\python.exe" -m invest_mcp
```

Distribución: repo git `github.com/nogales02/invest-mcp` (rama `main`), sin PyPI.
`pip install git+…` o `git clone` + `pip install -e .`. Detalle en `INSTALL.md`.
El servidor es cliente-agnóstico (stdio + `--transport streamable-http`); sirve
para Claude, Cline, LibreChat, mcphost (Ollama), OpenAI Agents SDK, etc.

Tras tocar código en `geo/` o `calibration/worker.py`, reinstalar en el env
correspondiente (`.venv` + `invest-geo` / `invest-cal`).

Tras cambiar tools, **reiniciar Claude Code** para que recargue el servidor MCP.

---

## 9. Config (env `INVEST_MCP_*`, todo opcional — ver `.env.example`)

| Variable | Default | |
|---|---|---|
| `INVEST_MCP_INVEST_EXE` | autodetect Workbench | ruta a invest.exe |
| `INVEST_MCP_GEO_PYTHON` | autodetect `~/.conda/envs/invest-geo` | python.exe del env geo |
| `INVEST_MCP_DATA_ROOT` | `%USERPROFILE%\invest-mcp-data` | jobs/logs |
| `INVEST_MCP_ALLOWED_INPUT_DIRS` | — | carpetas extra de inputs (`;`-sep) |
| `INVEST_MCP_MAX_CONCURRENT_JOBS` | 2 | runs en paralelo |
| `INVEST_MCP_INVEST_TIMEOUT_SECONDS` | 21600 | timeout por run |
| `INVEST_MCP_LOCALE` | — | locale InVEST (es/en/zh) |

Siempre permitidas para leer inputs: `DATA_ROOT` y el cwd del servidor.

---

## 10. Estilo de código

- Type hints en todo; `from __future__ import annotations`.
- Docstrings de módulo que expliquen el "por qué", no solo el "qué".
- Sin dependencias nuevas sin discutirlo (el servidor debe seguir siendo ligero:
  `mcp` + `pydantic`). Lo geoespacial irá en su propio env/extra.
- Las tools devuelven dicts JSON-ables; errores como `{"ok": False, "error": "..."}`,
  no excepciones que crucen el límite MCP.
