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

**Publicado (2026-08-30):** repo git `github.com/nogales02/invest-mcp` (rama `main`),
sin PyPI. Es **cliente-agnóstico**: cualquier cliente MCP sirve (Claude Desktop/Code,
Cline, Continue, LibreChat, `mcphost` para Ollama, OpenAI Agents SDK…). No hay nada
que "activar" dentro de InVEST. Ver `INSTALL.md`.

---

## 2. Decisión de arquitectura central (v0.1)

El servidor es un **wrapper de subproceso puro** sobre el `invest.exe` que trae
instalado el **InVEST Workbench**. **No importa `natcap.invest`.**

Motivos:
- El Python del sistema es **3.14** y no hay wheels de GDAL para 3.14/Windows, así
  que `pip install natcap.invest` en el `.venv` es **imposible** (pin `gdal==3.10.*`
  sin wheel).
- El aislamiento por subproceso contiene segfaults de GDAL y hace que cancelar un
  run sea un kill real.
- Lo pesado (GDAL, `natcap.invest`, `spotpy`) vive en envs conda **sidecar**
  (`invest-geo`, `invest-cal`), invocadas por subproceso — el `.venv` sigue ligero.

Un proceso del SO por run, supervisado por un hilo daemon + un `BoundedSemaphore`
para limitar concurrencia. Los jobs persisten en
`%USERPROFILE%\invest-mcp-data\jobs\<job_id>\`.

### Hechos del entorno (verificados 2026-08-30)
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
  gdal 3.12, geopandas, rasterstats, matplotlib-base, pandas, **numpy pin `<2.3`** +
  `spotpy` (pip) + `invest_mcp` y `invest-calibration-assistant` (pip -e, apunta a
  `Y:\Server-UserFolder\Escritorio\Invest_Plugin_Calibration`). No se pudo
  añadir natcap.invest a `invest-geo` (choca el pin `gdal==3.10.*`), de ahí el
  env aparte. Ver `environment-cal.yml`.
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
- **`invest-mcp setup` / conda discovery** (aprendido al verificar desde un clon
  limpio): `_find_conda()` devuelve solo **ejecutables reales** — los wrappers
  `condabin\*.BAT`/`.CMD` **crashean** en `env create` (`0xC0000409`). Prefiere el
  `micromamba.exe` que trae el Workbench (`resources/micromamba.exe`), así que con
  solo el Workbench basta. micromamba usa `create` (no `env create`) y deja los
  envs en `%APPDATA%\mamba\envs\` — la detección (`_conda_env_roots`) ya mira ahí
  + `MAMBA_ROOT_PREFIX` + `%LOCALAPPDATA%\{mamba,micromamba}`. `setup` tiene
  fallback `<conda> run -n <name> python` para localizar el env e imprime la ruta.

---

## 3. Estructura del repo

```
src/invest_mcp/
  cli.py             `invest-mcp` : subcomandos serve / doctor / setup / mcp-config
  server.py          build_server() -> MCPServer ; run() honra transport/host/port
  config.py          Settings (env INVEST_MCP_*) + autodetección invest.exe / envs (POSIX-safe)
  invest_cli.py      subproceso: version / list / getspec / validate  (fuerza UTF-8)
  models/
    registry.py      lista modelos, cachea specs (lru_cache), resuelve alias
    spec_translate.py  MODEL_SPEC -> JSON Schema del `args` + briefing markdown
  execution/
    jobs.py          Job (dataclass) + JobStore persistente (job.json por job)
    runner.py        lanza `invest run`, semáforo, cancel (taskkill /T), wait (Event)
  workspace/
    sandbox.py       allow-list de rutas de entrada (resolve_input_path) + de salida (resolve_output_path) + allow_dir de sesión
    project.py       convención de "proyecto InVEST": scaffold del árbol + project.json (stdlib, corre en el server)
    artifacts.py     catálogo de ficheros de salida (path, kind, size)
  geo/
    client.py        (server-side) build_payload/run_preflight + plan_rasters/run_summary + plan_comparison/run_comparison + _run_prep/run_reproject/run_clip/run_align_stack; subproceso al env invest-geo
    preflight.py     (CORRE EN invest-geo) lee JSON de stdin, chequea CRS/overlap/pixel
    summarize.py     (CORRE EN invest-geo) stats por ráster + zonal sobre AOI + escribe summary.json
    compare.py       (CORRE EN invest-geo) alinea escenario->baseline, ráster diferencia + delta stats + zonal + escribe compare.json
    prep.py          (CORRE EN invest-geo) rutinas deterministas: op=reproject|clip|align_stack (raster+vector); lee JSON de stdin, escribe salidas + describe cada capa
    _preview_worker.py  (SUBPROCESO AISLADO) rinde el PNG de preview (matplotlib), best-effort; `--diverging` = RdBu_r centrado en 0 para un ráster diferencia
  calibration/
    client.py        (server-side) CalibrationRunner: job + subproceso al env invest-cal
    worker.py        (CORRE EN invest-cal) lee JSON, llama invest_calibration_assistant.core
  provenance.py      provenance.json por run: versiones + sha256 de cada input
  tools.py           las 22 tools MCP + register(server)
environment-geo.yml  env invest-geo   |  environment-cal.yml  env invest-cal
environment-server.yml  env conda "invest-mcp" (python+pip) para el servidor sin Python del sistema
scripts/             bootstrap.ps1 (Windows) / bootstrap.sh (POSIX): elige server env (.venv o conda invest-mcp) + pip + setup + doctor + mcp-config
docs/                INSTALACION-PASO-A-PASO.md  guía "para dummies" (ES): de cero a Claude conectado
INSTALL.md           referencia terse: prereqs + snippets de config por cliente + seguridad
tests/               test_spec_translate, test_sandbox, test_geo_payload,
                     test_calibration_tools, test_summarize, test_compare,
                     test_prep  (40 tests)
```

`geo/preflight.py` (y `geo/summarize.py`, `geo/compare.py`, `geo/prep.py`)
**solo importan stdlib al cargar**; rasterio/pyproj/shapely/pyogrio/geopandas se
importan dentro de las funciones (para que el env `.venv` pueda importar el
módulo sin GDAL, aunque nunca lo ejecuta).

**Motor de calibración** = repo aparte `github.com/N4W-Facility/Invest_Plugin_Calibration`,
rama `refactor/shared-core` (pusheada), paquete `invest_calibration_assistant.core`
(`config`/`engine`/`models/{awy,swy,sdr,ndr}`/`biotable`/`metrics`/`zonal`/`selection`/
`plots`+`_plot_worker`). Compartido con el plugin del Workbench del usuario. Clon local
en `Y:\Server-UserFolder\Escritorio\Invest_Plugin_Calibration`.

---

## 4. Tool surface (22 tools)

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
| `summarize_results(job_id, aoi_path="", rasters=None, include_intermediate=False, make_preview=True)` | Resumen de un run terminado: stats por ráster de salida (válidos/nodata, min/max/media/std/suma, histograma 10-bins), zonal por feature sobre un AOI vectorial (reproyectado al CRS del ráster), la `raster_values_summary.csv` de InVEST si existe, un digest en lenguaje natural y un PNG de preview del ráster principal (best-effort, subproceso aislado). Escribe `<jobdir>/summary/summary.json`. Necesita el env `invest-geo`. |
| `compare_scenarios(baseline_job_id, scenario_job_id, aoi_path="", rasters=None, include_intermediate=False, make_preview=True)` | Baseline vs escenario alternativo del **mismo modelo** (el propósito de InVEST — tradeoffs). Por cada ráster de salida presente en ambos runs: alinea el escenario a la malla del baseline (reproyecta si difieren), escribe `diff_<nombre>.tif` = `escenario - baseline`, y reporta total antes/después, Δ y % de cambio, px que suben/bajan/igual, histograma de Δ, zonal de Δ por feature sobre un AOI, digest NL y un PNG de preview con colormap divergente. Escribe `<scen_jobdir>/compare_vs_<baseline_job_id>/compare.json`. Necesita el env `invest-geo`. |
| `scaffold_project(root, name="", target_crs="", aoi_path="", overwrite=False)` | Crea el árbol de "proyecto InVEST" (`data/raw`, `data/processed`, `tables`, `datastacks`, `jobs`, `logs`) + `project.json` (nombre, CRS objetivo, AOI, `datasets: []`). Añade `root` a la allow-list de la sesión (lecturas y **escrituras**). Idempotente; `overwrite` solo reescribe el `project.json`. No necesita `invest-geo`. |
| `reproject_layer(src_path, dst_path, target_crs, resampling="nearest", resolution=None)` | Reproyecta un ráster o vector a `target_crs` (EPSG/WKT/proj). `resampling` (solo ráster): `nearest` para categóricos (land cover), `bilinear`/`cubic`/`average` para continuos. `resolution` `[x,y]` opcional = tamaño de píxel objetivo. Necesita `invest-geo`. |
| `clip_to_aoi(src_path, dst_path, aoi_path, all_touched=False)` | Recorta un ráster (crop al bbox del AOI + máscara) o vector (`gpd.clip`) al polígono de `aoi_path`. El AOI se reproyecta al CRS de la capa. Necesita `invest-geo`. |
| `align_raster_stack(rasters, reference_path="", target_crs="", resolution=None, extent=None, resampling="nearest")` | Pone varios rásters en **una malla idéntica** (mismo CRS + tamaño de píxel + extent + alineación) para que InVEST los apile. `rasters` = lista de `{"src","dst"}`. Malla **o** desde `reference_path` (un ráster) **o** desde `target_crs`+`resolution`+`extent` juntos. `resampling` se aplica a todos (correr dos veces si mezcla categóricos y continuos). Necesita `invest-geo`. |
| `validate_calibration_config(config)` | Chequea una config de calibración (modelo/params/objetivo, columnas de Obs_Data, flags `Status_Cal_*`, caps de factores, sandbox). |
| `run_calibration(model, parameters, objective, optimizer, observed_data_path, model_inputs, ...)` | Job de calibración (spotpy DDS/LHS/SCE-UA sobre InVEST). Devuelve `job_id`. Modelos: **AWY, SWY, SDR, NDR_N, NDR_P**. |
| `get_calibration_job(job_id)` | Estado + iteraciones; al terminar: best params, objetivo, obs-vs-sim, `diagnostics` (sensibilidad Spearman por parámetro), `dotty_data` (rutas a JSON ploteables). |
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
- Autodetección de `invest.exe` + envs `invest-geo` / `invest-cal`; `invest_env`,
  `list_invest_models`, `describe_invest_model('carbon')` → schema correcto.
- `validate_invest_args` → required + sandbox + `invest validate` + preflight geo.
- **Preflight geoespacial** (`preflight_geo`) end-to-end (.venv → subproceso → env
  invest-geo) con datos sintéticos: `crs_not_projected`, `crs_units_not_meters`,
  `crs_mismatch`, `no_spatial_overlap`, `nodata_undefined`, `pixel_size_mismatch`;
  escenario limpio → `ok: True`.
- **Ciclo de job completo**: submit → subproceso → estado terminal → log →
  `provenance.json` (probado con un run que falla a propósito). `wait_seconds` usa
  `threading.Event`: bloquea hasta *finalizado* (provenance escrita), no solo hasta
  que cambia el estado.
- **Calibración — los 5 modelos** (AWY/SWY/SDR/NDR_N/NDR_P) end-to-end (.venv →
  subproceso → env invest-cal → `invest_calibration_assistant.core.calibrate`)
  contra el dataset real `Dummy_InVEST`, 10–40 iter LHS + run best:
  SDR 40 iter → 5.6% err · AWY 30 → 2% · SWY → ~3% · NDR_P cerca · NDR_N flojo
  (búsqueda corta). Devuelve `best_parameters`, `best_objective`, `obs_vs_sim`,
  `diagnostics` (Spearman por param), `warnings` (caps de factores),
  `dotty_data_<MODELO>.json`. JPG de dotty plots se rinde en subproceso aislado
  (matplotlib roto en esta máquina → solo se pierde el JPG). Motor = **núcleo
  compartido** extraído del plugin del Workbench del usuario.
- **CLI** `invest-mcp {serve,doctor,setup,mcp-config}`. **Verificado desde un clon
  limpio** (2026-08-30): `git clone` → venv → `pip install -e .` → `doctor` (marca
  envs ausentes como `[warn]`) → `setup --geo` (construye `invest-geo` con la
  micromamba del Workbench + `pip install -e`) → `doctor` verde → worker de
  preflight corre. Se arreglaron 3 bugs de discovery de conda en el proceso
  (ver §2).
- **Distribución lista**: `github.com/nogales02/invest-mcp` (main) pusheado;
  `N4W-Facility/Invest_Plugin_Calibration` rama `refactor/shared-core` pusheada.
  Transporte dual (stdio + streamable-http). `INSTALL.md` con snippets por cliente
  + tabla de troubleshooting.
- **Run exitoso de Carbon** de punta a punta (2026-08-30): job
  `carbon-20260830T103940-174b40` → `succeeded` en ~4 s con inputs de
  `Dummy_InVEST` (`INPUTS/LULC/LULC.tif` + `INPUTS/Carbon_Pools.csv`, generado
  extrayendo `lucode,c_above,c_below,c_soil,c_dead` de `01-Biophysical_Table.csv`).
  `validate_invest_args` limpio → `run_invest_model` → `get_invest_job` →
  `list_invest_job_artifacts`: 5 rásters + `raster_values_summary.csv`
  (Baseline Carbon Storage 4 061 556 t) + `file_registry.json` + log InVEST +
  `provenance.json` con sha256 de los 2 inputs. Cierra el único hueco de
  verificación de modelos "normales".
- **`summarize_results`** end-to-end (2026-08-30, .venv → subproceso → invest-geo)
  sobre el job de Carbon: stats por ráster (`c_storage_bas.tif` suma
  4 061 555.98 — **cuadra exacto** con la `raster_values_summary.csv` de InVEST;
  los 4 pools suman el total), histograma, zonal sobre `SubBasin.shp`
  (reproyectado al CRS del ráster; media 50.62 sobre 76 650 px) y **PNG de
  preview sí renderizado** (subproceso aislado `_preview_worker`, matplotlib
  funcionó aquí; si crashea solo se pierde el JPG). Sidecar
  `<jobdir>/summary/summary.json`.
- **`compare_scenarios`** end-to-end (2026-08-30, .venv → subproceso → invest-geo)
  sobre Carbon: baseline (`carbon-20260830T103940-174b40`, LULC original) vs
  escenario de deforestación (clase 10 → 30, 1 717 px reclasados con numpy).
  Δ = **-130 285.96 t** (-3.21%); `delta_min` = **-75.88 t/ha exacto** = suma de
  pools de clase 10 (117.93) − clase 30 (42.05); `decreased_px` = 1 717 clavado;
  resto `unchanged` a 0. Mallas idénticas → sin resample, diferencia exacta.
  `diff_c_storage_bas.tif` (nodata NaN) + zonal de Δ sobre `SubBasin.shp` +
  **preview RdBu_r divergente centrado en 0 renderizado**. Sidecar
  `<scen_jobdir>/compare_vs_<baseline>/compare.json`.
- **Rutinas de datos deterministas (1ª tanda)** end-to-end (2026-08-30, .venv →
  subproceso → invest-geo) contra `Dummy_InVEST`:
  - `scaffold_project` → árbol + `project.json` (AOI registrada, `relpath` null si
    fuera del proyecto) + `root` añadido a la allow-list de la sesión (escrituras
    posteriores dentro de `data/processed/` funcionan).
  - `reproject_layer` DEM (EPSG:32733 → 4326, bilinear) 362×520→370×519 ✓;
    vector `SubBasin.shp` → 4326 ✓.
  - `clip_to_aoi` DEM al bbox de `Basin.shp` 362×520→358×516, CRS y píxel
    preservados ✓; vector `SubBasin` recortado a `Basin` ✓.
  - `align_raster_stack` [DEM, LULC] con `reference=DEM` → ambos a **malla
    idéntica** (362×520, mismos bounds y píxel, cada uno con su nodata/dtype) ✓.
  - Sandbox de escritura rechaza `dst` fuera de toda carpeta permitida; validación
    de `align_raster_stack` sin `reference`/`target_crs+resolution+extent`.
- 40 tests en verde (nuevo `test_prep`: scaffold del proyecto, `resolve_output_path`,
  construcción de payloads con `_run_prep` stubeado — todo puro). Registrado y
  "Connected" en Claude Code.

### Pendiente
- Resources y prompts MCP (por ahora solo tools).
- `conda-lock` para solves 100% reproducibles entre plataformas.
- Merge del PR del plugin a `main` → cambiar `INVEST_MCP_CAL_PLUGIN_SPEC` /
  `environment-cal.yml` de `@refactor/shared-core` a `@main`.

---

## 6. Roadmap (capas siguientes, en orden sugerido)

1. ~~Env de conda con GDAL~~ **HECHO** — envs `invest-geo` + `invest-cal`.
2. ~~Preflight geoespacial~~ **HECHO** — `geo/preflight.py` + tool `preflight_geo`.
   Pendiente de afinar: umbrales (`_PIXEL_RATIO_WARN`).
3. ~~Calibración de modelos hidrológicos~~ **HECHO** — 5 modelos, núcleo compartido,
   dotty plots (JSON + JPG aislado), tools `run_calibration` / `get_calibration_job`.
4. ~~Empaquetar para distribución git~~ **HECHO** — CLI, transporte dual, `INSTALL.md`,
   ambos repos pusheados.
5. ~~Run exitoso de carbon~~ **HECHO** (2026-08-30) — con inputs de `Dummy_InVEST`
   (LULC + `Carbon_Pools.csv` derivado de la tabla biofísica). Hueco cerrado.
6. ~~`summarize_results`~~ **HECHO** (2026-08-30) — `geo/summarize.py` +
   `geo/_preview_worker.py` + tool `summarize_results`. Stats por ráster + zonal
   sobre AOI + `raster_values_summary.csv` de InVEST + digest NL + PNG de preview
   + sidecar `summary.json`. Pendiente de afinar: `_DECIMATE_ABOVE_PX`,
   selección de columnas de id del AOI (ahora las 4 primeras).
7. ~~`compare_scenarios`~~ **HECHO** (2026-08-30) — `geo/compare.py` +
   `plan_comparison`/`run_comparison` en `geo/client.py` + tool `compare_scenarios`.
   Alinea escenario→baseline (reproject bilinear si difieren las mallas), escribe
   `diff_<name>.tif` = escenario−baseline, delta stats (total antes/después, Δ, %,
   px ↑/↓/=, histograma), zonal de Δ sobre AOI (reusa `summarize._zonal`), preview
   divergente (`_preview_worker --diverging`, RdBu_r centrado en 0). Verificado
   end-to-end con Carbon (deforestación clase 10→30). Pendiente de afinar:
   chunking para rásters gigantes (ahora lee la banda entera para escribir el diff).
8. **Rutinas de datos deterministas** (tools) — **PARCIAL** (2026-08-30):
   - **HECHO**: `scaffold_project` (`workspace/project.py`, stdlib) +
     `reproject_layer` / `clip_to_aoi` / `align_raster_stack` (`geo/prep.py` en
     invest-geo, `_run_prep`/`run_*` en `geo/client.py`). Sandbox de escritura
     `resolve_output_path`. Verificado end-to-end (ver §5).
   - **Pendiente**: `geo.fetch_dem`, `geo.fetch_landcover` (tocan red — decidir
     fuentes: SRTM/Copernicus DEM, ESA WorldCover/ESRI LC), `tables.from_template`
     (esqueleto de tabla biofísica por modelo). Afinar: chunking en `prep.py` para
     rásters gigantes (lee la banda entera); overwrite de shapefiles; poblar
     `datasets: []` del `project.json` desde estas rutinas.
9. **Playbooks** (prompts MCP): "preparar+correr NDR", "preparar+correr Carbon",
   "comparar dos escenarios de uso de suelo".
10. **Base de conocimiento** (resources): catálogo de fuentes de datos por variable,
    convención de carpetas, unidades, cheat-sheets por modelo.
11. **Cache content-addressed** por hash de inputs; **snapshots de JSON Schema** +
    diff en CI para detectar cambios breaking al subir versión de InVEST.
12. **conda-lock** (win-64 / linux-64 / osx-arm64) + imagen Docker (Linux, sin
    dependencia del Workbench, `invest` de conda-forge).

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
# instalación de cero (o re-hacerla): server env + pip + envs conda + doctor + mcp-config
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
#   server env: auto -> .venv si hay Python >=3.10 del sistema, si no un env conda
#   "invest-mcp" (environment-server.yml) con el micromamba del Workbench. Fuerza
#   con -CondaServer / -Venv.  flags: -SkipEnvs -GeoOnly -CalOnly -Conda <ruta> -Http
#   POSIX: scripts/bootstrap.sh (--conda-server / --venv / --skip-envs / ...)
#   guía "para dummies" (ES): docs\INSTALACION-PASO-A-PASO.md

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
#   usa: CONDA_EXE/MAMBA_EXE -> micromamba del PATH -> micromamba.exe del Workbench
#        -> conda.exe/mamba.exe real bajo una raiz (NUNCA un condabin\*.bat)
#   micromamba deja los envs en %APPDATA%\mamba\envs ; doctor los encuentra igual
# manual: conda env create -f environment-geo.yml  (o micromamba create -f ... -y)
#         luego  conda run -n invest-geo pip install -e . --no-deps
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
| `INVEST_MCP_INVEST_EXE` | autodetect Workbench (o `invest` en PATH) | ruta a invest.exe |
| `INVEST_MCP_GEO_PYTHON` | autodetect `~/.conda/envs/invest-geo` | python del env geo |
| `INVEST_MCP_CAL_PYTHON` | autodetect `~/.conda/envs/invest-cal` | python del env calibración |
| `INVEST_MCP_DATA_ROOT` | `%USERPROFILE%\invest-mcp-data` | jobs/logs |
| `INVEST_MCP_ALLOWED_INPUT_DIRS` | — | carpetas extra de inputs (`;` win / `:` posix) |
| `INVEST_MCP_MAX_CONCURRENT_JOBS` | 2 | runs en paralelo |
| `INVEST_MCP_INVEST_TIMEOUT_SECONDS` | 21600 | timeout por run |
| `INVEST_MCP_LOCALE` | — | locale InVEST (es/en/zh) |
| `INVEST_MCP_TRANSPORT` / `_HOST` / `_PORT` | `stdio` / `127.0.0.1` / `8000` | HTTP necesita extra `[http]` |
| `INVEST_MCP_CAL_PLUGIN_SPEC` | `... @ git+...@refactor/shared-core` | spec pip del núcleo de calibración (usado por `setup --cal`) |

Siempre permitidas para leer inputs: `DATA_ROOT` y el cwd del servidor.

---

## 10. Estilo de código

- Type hints en todo; `from __future__ import annotations`.
- Docstrings de módulo que expliquen el "por qué", no solo el "qué".
- Sin dependencias nuevas sin discutirlo (el servidor debe seguir siendo ligero:
  `mcp` + `pydantic`). Lo geoespacial/calibración vive en sus envs conda; extras
  opcionales en `pyproject` (`[http]` = uvicorn, `[dev]` = pytest).
- Las tools devuelven dicts JSON-ables; errores como `{"ok": False, "error": "..."}`,
  no excepciones que crucen el límite MCP.
