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
    spec_translate.py  MODEL_SPEC -> JSON Schema del `args` + briefing markdown + table_arg_specs (columnas de cada CSV + index_col)
  execution/
    jobs.py          Job (dataclass) + JobStore persistente (job.json por job)
    runner.py        lanza `invest run`, semáforo, cancel (taskkill /T), wait (Event)
  workspace/
    sandbox.py       allow-list de rutas de entrada (resolve_input_path) + de salida (resolve_output_path) + allow_dir de sesión
    project.py       convención de "proyecto InVEST": scaffold del árbol + project.json (stdlib, corre en el server)
    readiness.py     escanea data/ + tables/, adivina rol por nombre, casa contra los inputs required de cada modelo (stdlib + spec_translate; puro)
    biotable.py      tablas biofísicas (stdlib; puro): build_template (esqueleto: expande [MONTH]/[SOIL_GROUP], una fila por lucode, celdas en blanco) + parse_legend + check_table (valida una tabla RELLENA: cobertura vs clases del ráster, columnas required/inesperadas, celdas vacías/no-numéricas, invariantes duras + rangos típicos citados del KB; severity error|warning|ok). Ambas aceptan `conditions` ({cond: bool}) → columna condicional con cond True = required dura, False = descartada, ausente = advisory; `_requirement_label` es el helper compartido. check_table devuelve `enforced_conditions`
    datastack.py     lee/escribe el parameter set `.invest.json` del Workbench ({args, model_id, invest_version}); tolera `model_name` legacy; relativize/absolutize de rutas (stdlib; puro) — punto de integración con el Workbench
    artifacts.py     catálogo de ficheros de salida (path, kind, size)
    report.py        render(payload) → memo markdown de métodos + resultados (overview, params, provenance con sha256, outputs, results desde summary.json, figuras, sección de comparación) — stdlib puro, solo colaciona y formatea. Lo usa la tool build_report
  geo/
    client.py        (server-side) build_payload/run_preflight + plan_rasters/run_summary + plan_comparison/run_comparison + _run_geo_worker (genérico) → _run_prep/run_reproject/run_clip/run_align_stack/run_raster_classes + run_delineate_watersheds + run_aggregate_to_units + run_fetch_dem/run_fetch_landcover/run_fetch_climate/run_fetch_soil/run_fetch_hydrography; subproceso al env invest-geo
    preflight.py     (CORRE EN invest-geo) lee JSON de stdin, chequea CRS/overlap/pixel
    summarize.py     (CORRE EN invest-geo) stats por ráster + zonal sobre AOI + escribe summary.json
    compare.py       (CORRE EN invest-geo) alinea escenario->baseline, ráster diferencia + delta stats + zonal + escribe compare.json
    aggregate.py     (CORRE EN invest-geo) aggregate_to_units: roll-up zonal de 1+ rásters de servicio (o `diff_*.tif` de compare_scenarios) a polígonos de unidades (municipios/predios/intervención); por unidad×ráster stats sum|mean|count|min|max|std|median + `val_<label>` = value_per_unit·sum (sum area-weighted por ha si area_weighted, se ignora en CRS geográfico); reescribe el vector de unidades con una columna por (ráster, stat) + CSV tidy + `<dst>_aggregate.json`. Reusa `summarize._jsonable` + `prep._describe_vector`
    prep.py          (CORRE EN invest-geo) rutinas deterministas: op=reproject|clip|align_stack|raster_classes (raster+vector); lee JSON de stdin, escribe salidas + describe cada capa
    hydro.py         (CORRE EN invest-geo) delineación de cuencas: pygeoprocessing fill_pits→flow_dir_d8→flow_accum→extract_streams_d8→snap outlets→delineate_watersheds_d8; escribe el vector de cuencas + intermedios en `<dst_stem>_hydro/`
    fetch.py         (CORRE EN invest-geo, TOCA RED) op=dem|landcover: descarga de buckets AWS públicos vía GDAL `/vsicurl/` (sin auth) — dem=Copernicus GLO-30 (`copernicus-dem-30m`, tiles 1°), landcover=ESA WorldCover 10m 2020/2021 (`esa-worldcover`, tiles 3°, +class_legend). `_download_layer` mosaica; `reproject_clip_describe` = cola común (reproyecta/recorta vía `prep`, describe) — la reusa `climate.py`
    climate.py       (CORRE EN invest-geo, TOCA RED) precip/ETo de **WorldClim v2.1** (climatología mensual, sin auth, `/vsizip//vsicurl/`): variable=precipitation (`prec`, mm) | eto (Hargreaves-Samani desde `tmin/tmax/tavg` + Ra por latitud/DOY). period=monthly (12 ficheros, `{month}` en dst) | annual (suma). resolution 10m..30s
    soil.py          (CORRE EN invest-geo, TOCA RED) suelo de **SoilGrids 2.0** (ISRIC, 250 m, sin auth, VRT global vía `/vsicurl/` + `WarpedVRT` IGH→EPSG:4326, ventana al bbox): variable=texture (sand/silt/clay %, `{fraction}` en dst) | hydrologic_soil_group (HSG 1..4 derivado del triángulo textural USDA — aprox. solo-textura) | usle_k (K por Williams/EPIC 1995 desde sand/silt/clay/SOC → SI ×0.1317) | depth_to_bedrock (**SoilGrids 2017** BDTICM, GeoTIFF global ya en EPSG:4326, cm→mm para AWY `depth_to_root_rest_layer`). depth 0-5cm..100-200cm; stat mean/Q0.05/Q0.5/Q0.95. Reusa `fetch.reproject_clip_describe`
    hydrography.py   (CORRE EN invest-geo, TOCA RED) hidrografía de **HydroSHEDS v1** (sin auth): product=rivers (HydroRIVERS v1.0, líneas) | basins (HydroBASINS v1c standard, polígonos, level 1..12). Lee el shapefile del zip remoto vía GDAL `/vsizip//vsicurl/` + filtro bbox (índice `.sbn` → transferencia local; `CPL_VSIL_CURL_USE_HEAD=NO` porque el server bloquea HEAD). region auto-detectada del centroide del AOI contra 9 envolventes continentales (af ar as au eu gr na sa si); centroide ambiguo → pide region=. clip_to_aoi por defecto False (features enteras que intersectan). driver por extensión del dst
    _preview_worker.py  (SUBPROCESO AISLADO) rinde el PNG de preview (matplotlib), best-effort; `--diverging` = RdBu_r centrado en 0 para un ráster diferencia
  calibration/
    client.py        (server-side) CalibrationRunner: job + subproceso al env invest-cal
    worker.py        (CORRE EN invest-cal) lee JSON, llama invest_calibration_assistant.core
  provenance.py      provenance.json por run: versiones + sha256 de cada input
  tools.py           las 36 tools MCP + register(server); helpers `_choose_table_arg` + `_table_conditions` (tables_from_template / check_table_vs_raster: `_table_conditions` resuelve `{cond: bool}` de las columnas condicionales contra el dict `args` que se pasa, solo para las condiciones nombradas como clave en `args` — H3: NDR `calc_n`/`calc_p` → exige/descarta `load_type_n`/`load_type_p` etc.), `_clone_args` (clone_job: aplica drop_args + overrides sobre los args del datastack, devuelve args + diff), `_aggregate_raster_specs` (aggregate_to_units: normaliza el arg `rasters` = path | dict | lista → specs con label slug+dedup + value_per_unit por ráster con default global), `_load_json`
  resources.py       7 resources MCP (catálogo de modelos, cheat-sheet por modelo, convención de carpetas, catálogo de fuentes de datos, guía de modelos = qué modelo responde a qué pregunta, base de coeficientes citados + sus ficheros) + register(server)
  prompts.py         4 prompts/playbooks MCP (prepare_and_run_model, compare_land_use_scenarios, fill_biophysical_table, recommend_model) + register(server)
  knowledge/
    __init__.py      paquete de DATOS de referencia (nunca lógica): base de coeficientes citados
    coefficients.py  loader stdlib puro: entry(name)/index()/PARAMETERS/PROFILES; lee los JSON de coefficients/ y los sirve como texto. + parameter_for_column(col)/column_ranges() → mapea columna InVEST (usle_c, cn_a, kc_6, c_above…) a su parámetro del KB + su typical_range citado (lo usa check_table_vs_raster)
    coefficients/    base de coeficientes citados para las tablas biofísicas/lookup de InVEST (v1, mayormente del FS28-Moorabool_MasterFile; root_depth + carbon_pools de refs estándar):
      README.md        cómo elegir un valor (5 pasos), por qué NO se casa con una leyenda, forma de cada registro
      sources.json     bibliografía: por fuente citation/doi/url/type/scope/provides/context/verified (web|excel|standard)
      usle_c.json      ~29 registros — Yang 2003 (medias globales por clase), Bakker 2008 (parcelas Europa), Teng 2016 (Australia RUSLE ~1km), Benavidez 2018 (cultivos, by_source Panagos/David/Morgan), Xiong 2023 (por bioma), Wischmeier 1978 (laboreo)
      usle_p.json      6 registros — P=1 por defecto, contorno por pendiente (Wischmeier 1978), fajas (Renard 1997), terrazas (Renard 1997 + Chen 2017), medias globales (Xiong 2019)
      ndr_nutrient.json  ~19 registros — Raji 2020 (Sokoto-Rima, semi-árido tropical: set NDR completo con crit_len + proportion_subsurface), Redhead 2018 (GB nacional, LCM2007), Ludemann 2022 (fertilizante Australia por cultivo), fila BMP % reducción
      curve_number.json  ~15 registros — Asante 2008/GeoSFM (mayoría de clases), Jaafar 2019/GCN250 (suelo desnudo, plantaciones, pastizal nativo buen estado); values = {CN_A..CN_D}; InVEST SWY espera ARC II
      kc.json          2 registros — Kc anual medio por clase DEECA vía LAI (FAO-56 desde Copernicus LAI 300m, k=0.5, cap 1.2) + Kc de referencia FAO-56 por forma de cultivo; Kc mensuales viven en el profile
      root_depth.json  ~11 registros (NUEVO) — por bioma/forma: max_root_depth_mm (Canadell 1996) + d95_mm (Schenk & Jackson 2002); cultivos usan effective_root_depth_mm (FAO-56); nota de convención max vs D95 + min(root_depth, soil_depth)
      carbon_pools.json  ~11 registros (NUEVO) — por bioma: values = {c_above,c_below,c_soil,c_dead} en Mg C/ha (NO biomasa, NO CO2e) + ranges; método de ensamblaje IPCC (AGB×0.47, ratio R:S, SOC ref × factores); IPCC 2006/2019 Vol4 + Ruesch & Gibbs 2008; todo confidence "low" (Tier-1)
      profiles/
        moorabool_fs28.json  ejemplo trabajado (NO defaults): parametrización FS28 completa para la cuenca del Moorabool, Victoria — study_context, raster_inputs, factores de cambio climático SSP2-4.5, legend_crosswalk, biophysical_table (18 filas), kc_monthly, nbs_maturation, known_inconsistency, sources_used
environment-geo.yml  env invest-geo   |  environment-cal.yml  env invest-cal
environment-server.yml  env conda "invest-mcp" (python+pip) para el servidor sin Python del sistema
scripts/             bootstrap.ps1 (Windows) / bootstrap.sh (POSIX): elige server env (.venv o conda invest-mcp) + pip + setup + doctor + mcp-config
docs/                INSTALACION-PASO-A-PASO.md  guía "para dummies" (ES): de cero a Claude conectado (+ §5.c: pilotar el MCP con Ollama / Qwen vía mcphost)
                     MANUAL-HERRAMIENTAS.md      referencia tipo "help" (ES) de las 36 tools + 7 resources + 4 prompts: ficha por tool (firma, params, devuelve, ejemplo, trampas) + conceptos base + flujo + cookbook + errores frecuentes + glosario
INSTALL.md           referencia terse: prereqs + snippets de config por cliente + seguridad
tests/               test_spec_translate, test_sandbox, test_geo_payload,
                     test_calibration_tools, test_summarize, test_compare,
                     test_prep, test_readiness, test_resources_prompts,
                     test_hydro, test_biotable, test_fetch,
                     test_climate, test_soil, test_hydrography,
                     test_datastack, test_knowledge_coefficients,
                     test_check_table, test_clone_job, test_report,
                     test_aggregate
                     (220 tests: 214 pass + 6 skip sin numpy)
```

`geo/preflight.py` (y `geo/summarize.py`, `geo/compare.py`, `geo/aggregate.py`,
`geo/prep.py`, `geo/hydro.py`, `geo/fetch.py`, `geo/climate.py`, `geo/soil.py`,
`geo/hydrography.py`) **solo importan
stdlib al cargar**; rasterio/pyproj/shapely/pyogrio/geopandas/pygeoprocessing/numpy se
importan dentro de las funciones (para que el env `.venv` pueda importar el
módulo sin GDAL, aunque nunca lo ejecuta).

**Motor de calibración** = repo aparte `github.com/N4W-Facility/Invest_Plugin_Calibration`,
rama `refactor/shared-core` (pusheada), paquete `invest_calibration_assistant.core`
(`config`/`engine`/`models/{awy,swy,sdr,ndr}`/`biotable`/`metrics`/`zonal`/`selection`/
`plots`+`_plot_worker`). Compartido con el plugin del Workbench del usuario. Clon local
en `Y:\Server-UserFolder\Escritorio\Invest_Plugin_Calibration`.

---

## 4. Tool surface (36 tools) + 7 resources + 4 prompts

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
| `clone_job(job_id, overrides=None, drop_args=None, wait_seconds=0)` | Re-lanza un job previo con args cambiados. Saca `model_id`+`args` del `datastack.json` del job, aplica `drop_args` (quita) y `overrides` (pone/añade), y hace `submit` por el mismo camino que `run_invest_model` (checks de sandbox + rutas incluidos). Siempre quita `workspace_dir`. Devuelve el job nuevo + `cloned_from`, `source_status`, `diff` (`{arg: {from, to}}`; `to: null` si se quitó), `arg_count`/`unchanged_arg_count`. El job origen puede estar en cualquier estado; no se toca. Uso típico: cambiar el LULC por uno de escenario y pasar baseline + clon a `compare_scenarios`. Error si el clon quedaría idéntico o si el datastack no está en disco. |
| `list_invest_job_artifacts(job_id)` | Catálogo de todos los ficheros de salida. |
| `summarize_results(job_id, aoi_path="", rasters=None, include_intermediate=False, make_preview=True)` | Resumen de un run terminado: stats por ráster de salida (válidos/nodata, min/max/media/std/suma, histograma 10-bins), zonal por feature sobre un AOI vectorial (reproyectado al CRS del ráster), la `raster_values_summary.csv` de InVEST si existe, un digest en lenguaje natural y un PNG de preview del ráster principal (best-effort, subproceso aislado). Escribe `<jobdir>/summary/summary.json`. Necesita el env `invest-geo`. |
| `compare_scenarios(baseline_job_id, scenario_job_id, aoi_path="", rasters=None, include_intermediate=False, make_preview=True)` | Baseline vs escenario alternativo del **mismo modelo** (el propósito de InVEST — tradeoffs). Por cada ráster de salida presente en ambos runs: alinea el escenario a la malla del baseline (reproyecta si difieren), escribe `diff_<nombre>.tif` = `escenario - baseline`, y reporta total antes/después, Δ y % de cambio, px que suben/bajan/igual, histograma de Δ, zonal de Δ por feature sobre un AOI, digest NL y un PNG de preview con colormap divergente. Escribe `<scen_jobdir>/compare_vs_<baseline_job_id>/compare.json`. Necesita el env `invest-geo`. |
| `aggregate_to_units(rasters, units_path, dst_path, id_columns=None, stats=None, value_per_unit=None, area_weighted=False, all_touched=False, value_currency="USD", max_units=5000)` | **Entregable de reparto.** Roll-up zonal de 1+ rásters de servicio (una salida de InVEST, o un `diff_*.tif` de `compare_scenarios` — repartir el **Δ** es el caso típico: "¿qué le deja este cambio de uso a cada municipio/predio?") a los polígonos de `units_path`. `rasters` = un path, o una lista de paths / dicts `{path, label?, units?, value_per_unit?}`. Por unidad×ráster calcula `stats` (`sum` `mean` `count` `min` `max` `std` `median`; def sum/mean/count) sobre los píxeles cuyo centro cae en el polígono (`all_touched` = cualquier toque), y `val_<label>` = `value_per_unit`·sum (valoración $ simple donde InVEST no la trae; `value_per_unit` global con override por ráster). `area_weighted` multiplica cada píxel por su área en ha antes de sumar (para un ráster de densidad por-ha; se ignora en CRS geográfico). Reescribe `units_path` como `dst_path` (`.gpkg`/`.shp`/`.geojson` — driver por extensión; `.shp` trunca nombres largos) con una columna por (ráster, stat) + `val_*`, un CSV tidy al lado y `<dst>_aggregate.json`. Devuelve totales por ráster (incl. `value_total`), filas por unidad y digest NL. Necesita el env `invest-geo`. |
| `build_report(job_ids, dst_path, title="", include_args=True, include_provenance=True, include_artifacts=True, include_comparisons=True)` | Memo de métodos + resultados en **Markdown** para uno o varios jobs, ensamblado de lo que ya hay en disco — **colaciona y formatea, no calcula ni interpreta**. Por job: metadatos del run, `args` del datastack, `provenance.json` (versiones InVEST/tool + sha256 de cada input), catálogo de artefactos, el `summary.json` de `summarize_results` (stats por ráster + zonal AOI) + la `raster_values_summary.csv` de InVEST, y el PNG de preview. Si dos jobs dados tienen un `compare_scenarios` entre ellos, añade la sección de diferencia. Un job no-`succeeded` recibe la cola de su log. `job_ids` = uno o una lista (conserva el orden). `dst_path` termina en `.md`, bajo carpeta permitida. Rutas de imagen relativas a `dst_path` si comparten unidad. **No necesita `invest-geo`.** Convierte el `.md` con `pandoc report.md -o report.pdf`. |
| `fetch_dem(dst_path, aoi_path="", bbox=None, target_crs="", target_resolution=None, clip_to_aoi=True, buffer_deg=0.05, resampling="bilinear", source="cop30", keep_intermediate=False)` | **TOCA RED.** Descarga un DEM para el AOI y lo deja como GeoTIFF. `source="cop30"` (único cableado): **Copernicus GLO-30** (~30 m) del bucket AWS público `copernicus-dem-30m` — sin credenciales, contacta solo `copernicus-dem-30m.s3.amazonaws.com`. Área por `aoi_path` (vector; sus bounds mandan y con `clip_to_aoi` enmascara al polígono) y/o `bbox` `[minx,miny,maxx,maxy]` en **lon/lat (EPSG:4326)**. `buffer_deg` pad; `target_crs`/`target_resolution` reproyectan (reusa `prep._op_reproject`/`_op_clip`). Tiles oceánicos/fuera de cobertura → `tiles_missing`. Necesita `invest-geo`. |
| `fetch_landcover(dst_path, aoi_path="", bbox=None, year=2021, target_crs="", target_resolution=None, clip_to_aoi=True, buffer_deg=0.05, resampling="nearest", source="worldcover", keep_intermediate=False)` | **TOCA RED.** Igual que `fetch_dem` pero **ESA WorldCover 10 m** (`year` 2020/2021, 11 clases) del bucket AWS público `esa-worldcover` (sin auth, contacta `esa-worldcover.s3.eu-central-1.amazonaws.com`). `resampling="nearest"` por defecto (categórico). La respuesta trae `class_legend` (valor→etiqueta) listo para `tables_from_template`. Necesita `invest-geo`. |
| `fetch_climate(dst_path, variable, aoi_path="", bbox=None, source="worldclim", period="monthly", months=None, resolution="10m", target_crs="", target_resolution=None, clip_to_aoi=True, buffer_deg=0.05, resampling="bilinear", keep_intermediate=False)` | **TOCA RED.** Climatología de **WorldClim v2.1** (1970–2000, sin auth, contacta `geodata.ucdavis.edu`). `variable`: `precipitation` (`prec`, mm) o `eto` (ETo **Hargreaves-Samani** desde `tmin/tmax/tavg` + Ra por latitud/DOY — *modelada*, no medida). `period="monthly"` → 12 rásters (forma de SWY; `dst_path` **debe** llevar `{month}`, p.ej. `precip_{month}.tif`); `period="annual"` → 1 ráster = suma (forma de AWY). `months=[6,7,8]` subconjunto. `resolution` `10m`(def, ~18 km)/`5m`/`2.5m`/`30s`(~1 km). Necesita `invest-geo`. |
| `fetch_soil(dst_path, variable, aoi_path="", bbox=None, source="soilgrids", depth="0-5cm", stat="mean", target_crs="", target_resolution=None, clip_to_aoi=True, buffer_deg=0.05, resampling="", keep_intermediate=False)` | **TOCA RED.** Suelo de **SoilGrids 2.0** (ISRIC, 250 m, sin auth, contacta `files.isric.org`). VRT global en Homolosine → `WarpedVRT` a lon/lat + ventana al bbox antes de reproyectar. `variable`: `texture` (sand/silt/clay % en peso — 3 rásters, `dst_path` **debe** llevar `{fraction}`) · `hydrologic_soil_group` (HSG `1..4`=A..D para SWY/Urban Flood, derivado **solo del triángulo textural USDA** — aprox.; uint8 nodata 0; `resampling` def `nearest`) · `usle_k` (erodibilidad K para SDR en SI `t·ha·h·ha⁻¹·MJ⁻¹·mm⁻¹` vía Williams/EPIC 1995 desde sand/silt/clay/SOC) · `depth_to_bedrock` (profundidad absoluta a lecho rocoso para AWY `depth_to_root_rest_layer`, de **SoilGrids 2017** `BDTICM` — GeoTIFF global ya en EPSG:4326, cm→mm; `depth`/`stat` se ignoran; el GeoTIFF de 8.5 GB abre lento ~2 min). `depth` `0-5cm`(def)/`5-15cm`/`15-30cm`/`30-60cm`/`60-100cm`/`100-200cm`; `stat` `mean`(def)/`Q0.05`/`Q0.5`/`Q0.95`. Necesita `invest-geo`. |
| `fetch_hydrography(dst_path, product, aoi_path="", bbox=None, source="hydrosheds", region="", level=8, target_crs="", clip_to_aoi=False, buffer_deg=0.05, keep_intermediate=False)` | **TOCA RED.** Hidrografía de **HydroSHEDS v1** (WWF/McGill, sin auth, contacta `data.hydrosheds.org`). `product`: `rivers` (HydroRIVERS v1.0 — líneas con `DIS_AV_CMS`, `UPLAND_SKM`, orden Strahler, topología `NEXT_DOWN`) · `basins` (HydroBASINS v1c standard — polígonos, `level` Pfafstetter `1..12`, def 8). Lee el shapefile del zip remoto vía `/vsizip//vsicurl/` + filtro bbox (índice `.sbn`). `region` (`af ar as au eu gr na sa si`) en blanco = auto-detección por el centroide del AOI; centroide ambiguo (p.ej. Oriente Medio) → error pidiendo `region=`. `clip_to_aoi=False` (def) = features enteras que intersectan el bbox; `True` = `gpd.clip` geométrico. Driver por extensión de `dst_path` (`.gpkg`/`.shp`/`.geojson`). Necesita `invest-geo`. |
| `scaffold_project(root, name="", target_crs="", aoi_path="", overwrite=False)` | Crea el árbol de "proyecto InVEST" (`data/raw`, `data/processed`, `tables`, `datastacks`, `jobs`, `logs`) + `project.json` (nombre, CRS objetivo, AOI, `datasets: []`). Añade `root` a la allow-list de la sesión (lecturas y **escrituras**). Idempotente; `overwrite` solo reescribe el `project.json`. No necesita `invest-geo`. |
| `project_readiness(root, models=None)` | Escanea `data/` + `tables/` del proyecto, adivina el rol de cada fichero por su nombre (`dem`, `lulc`, `watersheds`, `biophysical_table`…) y lo casa contra los inputs **required** de cada modelo. Devuelve `inventory`, `assessments` (por modelo: `matched` / `ambiguous` / `missing` / `needs_values`), `ready_to_attempt`, `gaps_by_model` y un digest NL. **Apoya** la decisión de qué correr, no la toma. Los inputs numéricos/opción van en `needs_values`, no bloquean. No necesita `invest-geo`. |
| `reproject_layer(src_path, dst_path, target_crs, resampling="nearest", resolution=None)` | Reproyecta un ráster o vector a `target_crs` (EPSG/WKT/proj). `resampling` (solo ráster): `nearest` para categóricos (land cover), `bilinear`/`cubic`/`average` para continuos. `resolution` `[x,y]` opcional = tamaño de píxel objetivo. Necesita `invest-geo`. |
| `clip_to_aoi(src_path, dst_path, aoi_path, all_touched=False)` | Recorta un ráster (crop al bbox del AOI + máscara) o vector (`gpd.clip`) al polígono de `aoi_path`. El AOI se reproyecta al CRS de la capa. Necesita `invest-geo`. |
| `align_raster_stack(rasters, reference_path="", target_crs="", resolution=None, extent=None, resampling="nearest")` | Pone varios rásters en **una malla idéntica** (mismo CRS + tamaño de píxel + extent + alineación) para que InVEST los apile. `rasters` = lista de `{"src","dst"}`. Malla **o** desde `reference_path` (un ráster) **o** desde `target_crs`+`resolution`+`extent` juntos. `resampling` se aplica a todos (correr dos veces si mezcla categóricos y continuos). Necesita `invest-geo`. |
| `delineate_watersheds(dem_path, outlets_path, dst_path, threshold_flow_accumulation=1000, snap_distance_px=10, fill_pits=True, keep_intermediate=False)` | Corta polígonos de cuenca aguas arriba de puntos de salida con la cadena D8 de **pygeoprocessing** (mismo motor que InVEST → las cuencas cuadran con el routing de SDR/NDR/SWY): fill_pits → flow_dir_d8 → flow_accum → streams (umbral en px) → snap de cada outlet a la red → `delineate_watersheds_d8`. Reproyecta los outlets al CRS del DEM. Escribe `.gpkg`/`.shp`/`.geojson`; intermedios en `<dst_stem>_hydro/` (se borran salvo `keep_intermediate`). `snap_distance_px=0` desactiva el snap. Devuelve descripción del vector + `snap_report` por punto. Necesita `invest-geo`. |
| `tables_from_template(model_id, lulc_path, dst_path, table_arg="", legend_path="", include_optional=True, args=None, max_classes=1000)` | Esqueleto de tabla biofísica/lookup de un modelo: una fila por lucode único del LULC + las columnas que pide su MODEL_SPEC (celdas de coeficiente en blanco). `table_arg` = qué CSV templetar (auto-detecta el que va por `lucode`; si hay varios, el error los lista). `legend_path` (CSV `code,label`) → añade columna `description`. `args` (el dict `args` del run) resuelve las columnas **condicionales**: `{"calc_n": true, "calc_p": false}` → emite `load_type_n`/`load_n`/… como `required` y descarta las de fósforo; sin `args` todas salen `required if: <cond>`. Expande `[MONTH]`→`_1..12` y `[SOIL_GROUP]`→`_a..d`; otros `[TOKEN]` quedan literales con nota. Devuelve `headers`, `column_help` (about/units/requirement por columna), `classes` (valor+px), `resolved_conditions`, `narrative`. Necesita `invest-geo` (lee las clases del ráster). |
| `check_table_vs_raster(model_id, table_path, lulc_path="", table_arg="", include_optional=True, args=None, max_classes=1000)` | Valida una tabla biofísica/lookup **rellena** antes de correr. `checks`: `coverage` (con `lulc_path`: clases del ráster sin fila = `missing_rows`; filas para códigos ausentes = `orphan_rows`; claves duplicadas), `columns` (`missing` required, `unexpected` extras — solo se validan celdas de columnas que el modelo consume), `cells` (`empty_required`, `non_numeric`), `ranges` (`invariant_violations` duras: fracciones ∈ [0,1], curve numbers ordenados A≤B≤C≤D ∈ (0,100], loads/depths ≥ 0, root_depth entero · `out_of_typical` blandas: fuera de la banda citada del KB, con el `resource` fuente). `args` (el dict `args` del run) resuelve las columnas condicionales: con `{"calc_n": true}` `load_type_n`/`load_n`/`eff_n`/`crit_len_n`/`proportion_subsurface_n` pasan a required duras (faltan → `missing`, en blanco → `empty_required`); con `{"calc_p": false}` las de fósforo se ignoran; condiciones no nombradas quedan en `conditional_columns`. `enforced_conditions` lista las activadas. `severity` = `error` (bloqueante) \| `warning` (revisar) \| `ok`; `pass` = `severity != error`. `lulc_path` opcional (sin él: solo estructura + valores, no necesita `invest-geo`). Cierra `tables_from_template` → rellenar desde `invest://coefficients` → `check_table_vs_raster` → `validate_invest_args`. |
| `import_datastack(src_path)` | Lee un **datastack** `.invest.json` (parameter set, p.ej. uno que guardó el Workbench) y reporta qué trae: modelo, `args`, `required_missing`, por cada ruta si existe en disco y si cae dentro del sandbox, `ready`. Resuelve rutas relativas contra la carpeta del datastack. Enchufa directo con `validate_invest_args` → `run_invest_model`. stdlib puro, no necesita `invest-geo`. |
| `export_datastack(dst_path, model_id="", args=None, job_id="", relative=False)` | Escribe un **datastack** `.invest.json` (`{args, model_id, invest_version}`) que el Workbench abre directamente — el punto de hand-off con el Workbench. Da `model_id`+`args`, o `job_id` para sacar modelo+args de un run terminado. `relative=True` reescribe las rutas de fichero relativas a `dst_path` (stack portable). Chequea las rutas contra el sandbox y reporta su existencia sin bloquear. stdlib puro. |
| `validate_calibration_config(config)` | Chequea una config de calibración (modelo/params/objetivo, columnas de Obs_Data, flags `Status_Cal_*`, caps de factores, sandbox). |
| `run_calibration(model, parameters, objective, optimizer, observed_data_path, model_inputs, ...)` | Job de calibración (spotpy DDS/LHS/SCE-UA sobre InVEST). Devuelve `job_id`. Modelos: **AWY, SWY, SDR, NDR_N, NDR_P**. |
| `get_calibration_job(job_id)` | Estado + iteraciones; al terminar: best params, objetivo, obs-vs-sim, `diagnostics` (sensibilidad Spearman por parámetro), `dotty_data` (rutas a JSON ploteables). |
| `cancel_calibration_job(job_id)` | Mata un job de calibración. |

**Resources MCP** (`resources.py`) — referencia que el cliente lee sin gastar una
tool; solo datos, nunca decisiones:
- `invest://models` — catálogo de modelos instalados (JSON).
- `invest://model/{model_id}/cheatsheet` — briefing por modelo (`human_briefing`).
- `invest://conventions` — el layout de proyecto + campos de `project.json`.
- `invest://data-sources` — catálogo curado de fuentes (DEM, land cover, clima,
  suelo, hidrografía) con URLs y notas.
- `invest://model-guide` — guía markdown: qué modelo InVEST responde a qué
  pregunta del mundo real, con las entradas/salidas cabecera de cada modelo y
  sus emparejamientos habituales, agrupado por dominio (agua terrestre, carbono
  y hábitat, urbano, costero/marino, agricultura, recreación, herramientas de
  terreno) + qué queda fuera del alcance de InVEST. Mapa de partida para
  `recommend_model`; confirmar contra el cheat-sheet.
- `invest://coefficients` — índice de la base de coeficientes citados: por
  parámetro (resource, aka, models, invest_column, units, definition,
  record_count, source_keys), profiles, cómo elegir, bibliografía.
- `invest://coefficients/{name}` — un fichero de la base: un parámetro
  (`usle_c`, `usle_p`, `ndr_nutrient`, `curve_number`, `kc`, `root_depth`,
  `carbon_pools`), `sources` (bibliografía), `readme` (cómo elegir un valor) o
  un profile trabajado (`moorabool_fs28`). Registros indexados por atributos
  semánticos de cobertura (forma, densidad de dosel, condición, manejo, bioma,
  región, escala), NO por una leyenda concreta; el `crosswalk` es solo
  orientativo. Cada valor lleva `source_key` + `confidence` + `verified`.

**Prompts/playbooks MCP** (`prompts.py`) — recetas que el LLM sigue y adapta:
- `prepare_and_run_model(model_id, project_root)` — de scaffold a summarize.
- `compare_land_use_scenarios(model_id, project_root)` — baseline vs escenario
  con `compare_scenarios`.
- `fill_biophysical_table(model_id, project_root)` — del esqueleto de
  `tables_from_template` a una tabla con valores citados: casar cada clase de
  cobertura con un registro de `invest://coefficients` por semántica (no por
  código), anotar la procedencia en `logs/`, y `check_table_vs_raster` hasta
  `ok` o cada warning justificado.
- `recommend_model(question, project_root)` — de la pregunta del usuario a
  modelo(s) InVEST + los datos que necesita cada uno: afinar la pregunta →
  `invest://model-guide` + `list_invest_models` → shortlist confirmada contra
  los cheat-sheets → `project_readiness` para ver qué datos ya están → recomendar
  (primario + complementos, entradas disponible/preparar/falta, salidas, si hace
  falta baseline-vs-escenario) → hand-off a `prepare_and_run_model` /
  `compare_land_use_scenarios`. Dice claramente cuándo la pregunta está fuera de
  InVEST.

Convenciones:
- `args` es el dict de args de InVEST tal cual; **rutas absolutas**.
- El cliente NO debe pasar `workspace_dir` (lo gestiona el servidor).
- `required` en el spec puede ser `True`/`False` o **un string** (condicional, p.ej.
  `"do_valuation"`); el schema solo mete en `required` los `True` y documenta los
  condicionales en la `description`.

---

## 5. Estado actual

### ⟳ Para retomar (2026-08-30) — dónde estamos

Sesión larga añadiendo la **capa de preparación de datos** (roadmap §6 puntos
8–10) + el hand-off con el Workbench. Todo verificado end-to-end contra
`Dummy_InVEST` o un bbox de los Alpes.
**36 tools + 7 resources + 4 prompts. 220 tests en verde** (`pytest -q`, 214
pass + 6 skip sin numpy).

**Última sesión (2026-08-31, cont.) — H3:** `tables_from_template` y
`check_table_vs_raster` toman un `args` opcional (el dict `args` del run) y
resuelven las columnas **condicionales** del MODEL_SPEC contra él vía el helper
`_table_conditions`. Motivación: la suite test vio que una tabla biofísica de
NDR sin `load_type_n`/`load_type_p` pasaba `check_table_vs_raster` como `ok` y
luego `validate_invest_args`/el run fallaban. Ahora con `args={"calc_n": true,
"calc_p": false}` el template emite `load_type_n`/`load_n`/… como `required` y
descarta las de fósforo; el checker las exige (faltan → `missing`, en blanco →
`empty_required`) y descarta las de la rama off (ni required ni `unexpected`).
Sin `args` el comportamiento es el de antes (todas advisory). Núcleo puro en
`biotable._requirement_label` + `conditions` en `build_template`/`check_table`
(devuelve `enforced_conditions`). Verificado contra el MODEL_SPEC real de NDR.
+7 tests (`test_biotable` +2, `test_check_table` +5). Limpieza del árbol:
`.claude/settings.json` y `_suite_test/` a `.gitignore`, PROJECT-STATE rev 11
commiteado (`15494ec`).

**Sesión previa (2026-08-31, cont.):** `aggregate_to_units` `[tool]` +
`geo/aggregate.py` (CORRE EN invest-geo) — roll-up zonal de rásters de servicio
(o `diff_*.tif` de `compare_scenarios`) a polígonos de unidades
(municipios/predios/intervención), con valoración $ simple (`val_<label>` =
`value_per_unit`·sum). Núcleo puro `_aggregate_raster_specs` (normaliza el arg
`rasters` = path | dict | lista; label slug+dedup; `value_per_unit` global con
override por ráster) + `_aggregate_narrative`. Escribe el vector de unidades con
una columna por (ráster, stat) + `val_*`, CSV tidy y `<dst>_aggregate.json`.
Verificado end-to-end (.venv → subproceso → invest-geo) sobre `c_storage_bas.tif`
del job de carbon real + `SubBasin.shp`: `count` 76 650 px y `mean` 50.62
**clavan** con el zonal ya verificado de `summarize_results`; `sum` 3 880 246.53
(< total InVEST 4 061 555.98 porque la subcuenca no cubre todo el ráster);
`val_carbon_baseline` = sum·50 = 194 012 326.60. `Basin.shp` + `all_touched=True`
→ 77 672 px, 3 932 663.59. Drivers `.gpkg`/`.geojson`/`.shp` OK. +15 tests
(`test_aggregate.py`). Sin commitear.

**Sesión previa (2026-08-31, cont.):** `build_report` `[tool]` +
`workspace/report.py` — memo markdown de métodos + resultados para 1+ jobs,
ensamblado de lo que ya hay en disco (metadatos, `args`, `provenance.json` con
sha256, catálogo de artefactos, `summary.json` de `summarize_results` + la
`raster_values_summary.csv` de InVEST, figuras de preview, y la sección de
diferencia si hay un `compare.json` entre dos de los jobs). Solo colaciona y
formatea; stdlib puro; no necesita `invest-geo`. Verificado end-to-end sobre los
jobs reales de carbon (baseline + escenario de deforestación): las cifras del
memo cuadran con la `raster_values_summary.csv` (4 061 555.98) y con
`compare_scenarios` (Δ −130 285.96, −3.2%, 1 717 px ↓). +12 tests. También:
**`docs/INSTALACION-PASO-A-PASO.md` §5.c nuevo** — cómo pilotar el MCP con un
modelo local vía **Ollama** (`mcphost` + Qwen: modelo con `tools`, `num_ctx`
alto, config `mcpServers` idéntica; alternativa `ollmcp`; caveats).

**Sesión previa (2026-08-31, cont.):** `clone_job` `[tool]` — re-lanza un job
previo con args cambiados (lee el `datastack.json` del job, aplica
`drop_args`+`overrides`, `submit` por el camino de `run_invest_model`). Devuelve
`diff` + `cloned_from` + `source_status`; `hint` propone el `compare_scenarios`.
Núcleo puro `_clone_args` (10 tests) + guard rails con `_RUNNER.submit` stubeado.
Verificado end-to-end: clon de un job de carbon real (`carbon-…174b40`,
succeeded) con `overrides={carbon_pools_path: <copia>}` → job nuevo → **succeeded
en <60 s** con artefactos; `diff`/`arg_count` correctos.

**Sesión previa (2026-08-31, cont.):** `recommend_model` `[prompt]` +
`invest://model-guide` `[resource]` — de la pregunta del usuario a modelo(s)
InVEST. La guía cubre los 26 modelos instalados (answers/needs/gives/pair/
not-for, por dominio) + qué queda fuera de InVEST; el prompt orquesta
afinar-pregunta → guía + `list_invest_models` → confirmar cheat-sheets →
`project_readiness` → recomendar → hand-off. +2 tests.

**Sesión previa (2026-08-31, cont.):** `check_table_vs_raster` `[tool]` +
`fill_biophysical_table` `[prompt]` — cierran el lazo de la tabla biofísica.
`check_table_vs_raster` valida una tabla rellena (cobertura vs clases del
ráster, columnas, celdas, invariantes duras + rangos típicos citados del KB);
núcleo puro `biotable.check_table` + `coefficients.column_ranges()`; verificado
end-to-end contra `Dummy_InVEST` (carbon limpio → warning por 3 orphan rows;
SDR real → detecta 46 columnas ajenas al spec + `usle_c=0` bajo el suelo
citado; tabla rota → los 4 defectos plantados). `fill_biophysical_table` guía
esqueleto → `invest://coefficients` → check. +17 tests (`test_check_table.py`).

**Sesión previa (2026-08-31):** base de **coeficientes citados** (`[resource]`,
roadmap §6 punto 10) — paquete `src/invest_mcp/knowledge/coefficients/` con 7
parámetros (`usle_c`, `usle_p`, `ndr_nutrient`, `curve_number`, `kc`,
`root_depth`, `carbon_pools`) + `sources.json` (bibliografía verificada) +
`README.md` + el profile trabajado `moorabool_fs28`. Poblada mayormente del
`FS28-Moorabool_MasterFile.xlsx` (26 hojas, revisado hoja a hoja); `root_depth`
y `carbon_pools` añadidos de refs estándar (Canadell 1996, Schenk & Jackson
2002, FAO-56, IPCC 2006/2019 Vol4, Ruesch & Gibbs 2008). Registros
legend-agnósticos (atributos semánticos de cobertura, no una leyenda), cada
valor con cita + `confidence` + `verified` (`web`/`excel`/`standard`). Loader
stdlib puro `knowledge/coefficients.py`; 2 resources nuevos; 11 tests nuevos
(`test_knowledge_coefficients.py`). Sin commitear.

**La pila de 11 ramas ESTÁ FUSIONADA en `main`** (2026-08-30). `main` =
`8a56650` (`origin/main`), 11 merge-commits bottom-up (PRs #2, #12, #4, #5, #6,
#7, #8, #1, #9, #10, #11 — #3 se auto-cerró al borrar su rama base y se recreó
como #12). Todas las ramas de feature borradas (local y `origin`). Solo queda
`main`. Los merges se hicieron por la API de GitHub con el token del credential
manager (`git credential fill`); las bajas de rama con `git push origin
--delete`. `gh` CLI **no** está instalado.

Orden fusionado (cada rama era la anterior + más commits, pila lineal → cero
conflictos): `data-prep-routines` (scaffold + reproject/clip/align) →
`readiness-resources-prompts` (project_readiness + 4 resources + 2 prompts) →
`delineate-watersheds` (geo/hydro.py D8) → `tables-from-template`
(workspace/biotable.py) → `fetch-dem` (COP30) → `fetch-landcover` (WorldCover) →
`fetch-climate` (WorldClim + Hargreaves) → `fetch-soil` (SoilGrids 2.0) →
`fetch-hydrography` (HydroSHEDS) → `datastack-io` (import/export_datastack) →
`fetch-soil-bedrock` (SoilGrids 2017 BDTICM).

**Cadena de preparación ya montada** (todas las tools existen y están
verificadas): `scaffold_project` →
`fetch_dem`/`fetch_landcover`/`fetch_climate`/`fetch_soil`/`fetch_hydrography` →
`reproject_layer`/`clip_to_aoi`/`align_raster_stack` → `delineate_watersheds`
→ `tables_from_template` → `project_readiness` →
`import_datastack`/`export_datastack` (round-trip Workbench) →
`validate_invest_args` → `run_invest_model` →
`summarize_results`/`compare_scenarios`.

**Gotcha de esta sesión:** el acceso a S3 (`*.s3.amazonaws.com`) desde esta
máquina fue **intermitente** — algunas aperturas `/vsicurl/` tardaron >180 s o
colgaron (p.ej. el tile COP30 de Angola), otras fueron rápidas (Alpes). No es
bug del código; reintentar si un `fetch_*` cuelga. **`files.isric.org`
(SoilGrids) va parecido**: leer el VRT global vía `WarpedVRT` tarda ~2–3 min
para un AOI pequeño (el timeout del worker de fetch es 1800 s, sobra). El
GeoTIFF global BDTICM (SoilGrids 2017, ~8.5 GB, no-COG, strip) es aún más
frágil: una lectura de ventana puede cortarse a media franja
(`TIFFReadEncodedStrip() failed`) — `_read_bdticm` reintenta 4 veces. HydroSHEDS
(`data.hydrosheds.org`) fue **rápido y fiable** (rivers ~8 s, basins ~3 s) y
**bloquea HEAD** → `CPL_VSIL_CURL_USE_HEAD=NO`.

**Siguientes candidatos** (roadmap §6, "Capacidades pendientes por etapa"):
`fetch_hydrography` `source="hydrorivers_global"`/HydroBASINS lakes · `fetch_soil`
PAWC (SoilGrids 2017 `AWCh1..3`/`WWP`) y refinar HSG con Ksat+profundidad · `fetch_climate`
`source="terraclimate"`/`"chirps"` · `compare_scenarios_multi` · `export_map` ·
`aggregate_to_units` refinamientos (repartir a subunidades anidadas, valoración
por tabla en vez de escalar constante) · ampliar el KB de coeficientes (más
regiones/biomas, glosario, unidades por output).

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
- **`project_readiness` + primeros Resources y Prompts MCP** (2026-08-30):
  - `project_readiness` (`workspace/readiness.py`, puro): escanea `data/`+`tables/`,
    adivina rol por keywords en el nombre, casa contra los inputs required de cada
    modelo. Verificado contra `Dummy_InVEST` (SDR/SWY → `ready_to_attempt`; Carbon
    → falta `carbon_pools_path`; AWY → falta `precipitation_path`). El matching es
    name-based y best-effort **a propósito**: expone cada acierto/duda/hueco.
  - 4 resources (`invest://models`, `invest://model/{id}/cheatsheet`,
    `invest://conventions`, `invest://data-sources`) + 2 prompts
    (`prepare_and_run_model`, `compare_land_use_scenarios`) registrados en
    `build_server()` vía `resources.register` / `prompts.register`. `list_resources`
    / `list_resource_templates` / `list_prompts` del servidor los devuelven.
- **`delineate_watersheds`** end-to-end (2026-08-30, .venv → subproceso →
  invest-geo) sobre `Dummy_InVEST/DEM.tif` (EPSG:32733) con 2 puntos de salida:
  el píxel de máxima flow-accum (90 289 px) y otro a 8 px en diagonal. Cadena
  pygeoprocessing completa (`hydro.py`). Snap: punto 0 → 0.0 m (ya en cauce, buen
  sanity check); punto 1 → 497 m ≈ 5.4 px al cauce más cercano. 2 cuencas
  MultiPolygon en EPSG:32733; áreas **769.5 km²** (= 90 289 × 92.318² / 1e6
  **exacto**) y 708 km²; atributos (`name`) preservados. Intermedios (DEM relleno,
  flow dir/accum, streams, outlets snapped) en `<dst_stem>_hydro/`. `_run_prep`
  refactorizado a `_run_geo_worker` genérico (mismo stub en tests).
- **`tables_from_template`** end-to-end (2026-08-30, .venv → subproceso →
  invest-geo → `op=raster_classes`) sobre `Dummy_InVEST/INPUTS/LULC/LULC.tif`
  (8 clases: 10,20,…,90). `carbon` → `lucode,c_above,c_below,c_soil,c_dead` (=
  cabecera real de `Carbon_Pools.csv`); `ndr` → 9 columnas todas marcadas
  `required if: calc_n`/`calc_p`; `annual_water_yield` sin `table_arg` → error que
  lista `biophysical_table_path` + `demand_table_path` (ambos van por `lucode`);
  `seasonal_water_yield` → `cn_a..cn_d` + `kc_1..kc_12` expandidos + columna
  `description` de la leyenda. `spec_translate.table_arg_specs` extrae columnas +
  `index_col` del spec; `workspace/biotable.py` ensambla el CSV (puro).
- **`fetch_dem`** (`geo/fetch.py`, **primera tool que toca red**) end-to-end
  (2026-08-30, .venv → subproceso → invest-geo → GDAL `/vsicurl/`): bbox de los
  Alpes (~8×9 km, tile `N45/E006`) → COP30 mosaicado, reproyectado 4326→EPSG:32632
  a 30 m → 267×303 px, nodata -9999; elevaciones 1077–4088 m (media 2390), coheren-
  tes con el macizo. `tiles_used`/`tiles_missing`/`provider_host` en la respuesta.
  Sin auth (bucket AWS público). El tile de Angola (`S13/E016`, área de
  `Dummy_InVEST`) tardó >180 s en abrir — lentitud puntual de ese tile/red, no del
  código; el mecanismo (`/vsicurl/` + `rio_merge(bounds=)` + reproject/clip via
  `prep`) queda validado.
- **`fetch_landcover`** end-to-end (2026-08-30) sobre el mismo bbox de los Alpes:
  ESA WorldCover 2021 tile `N45E006` → mosaicado, reproyectado 4326→EPSG:32632 a
  10 m nearest → uint8, nodata 0; clases {10,20,30,40,50,60,70,80,100} presentes
  (nieve/roca desnuda/bosque — coherente con alta montaña). `class_legend` de 11
  clases en la respuesta. Comparte `_download_layer` con `fetch_dem`; solo cambia
  la enumeración de tiles (3° vs 1°) y el default de resampling (nearest vs
  bilinear).
- **`fetch_climate`** (`geo/climate.py`, TOCA RED) end-to-end (2026-08-30) sobre
  un bbox de los Alpes (~45.9°N 6.9°E): WorldClim 10m vía `/vsizip//vsicurl/`.
  `precipitation` anual → **1547 mm/yr** (reproyectado a UTM 32N @ 1 km),
  realista para Chamonix. `eto` Hargreaves meses 1/6/12 → **10.8 / 86.1 / 10.0
  mm/mes** (pico estival fuerte, correcto). `_ra_mm_per_day`: 45°N jun/dic =
  17.1/4.3, ecuador jun = 13.6 mm/d — físicamente sano. `reproject_clip_describe`
  extraído de `fetch._download_layer` y reusado.
- **`fetch_soil`** (`geo/soil.py`, TOCA RED) end-to-end (2026-08-30) sobre el
  bbox de los Alpes (~45.9°N 6.9°E): SoilGrids 2.0 `sand/silt/clay/soc` leídos
  del VRT global vía `/vsicurl/` + `WarpedVRT` IGH→EPSG:4326, ventana al bbox,
  reproyectado a UTM 32N @ 250 m. `hydrologic_soil_group` → HSG mayormente **2**
  (grupo B, till glacial en el valle) + nodata en la alta montaña sin datos de
  suelo. `usle_k` (Williams/EPIC → SI) → **K 0.030–0.035** t·ha·h·ha⁻¹·MJ⁻¹·mm⁻¹,
  media 0.032 (rango global típico 0.01–0.07). Triángulo textural USDA verificado
  contra las 12 clases; K más alto en limos, más bajo en arenas. La lectura del
  VRT de ISRIC tardó ~2–3 min (misma clase de lentitud de red que S3).
  `depth_to_bedrock` (SoilGrids 2017 `BDTICM`, cm→mm) añadido después: GeoTIFF
  global no-COG → lectura de ventana propensa a `TIFFReadEncodedStrip failed`,
  `_read_bdticm` reintenta 4×.
- **`fetch_hydrography`** (`geo/hydrography.py`, TOCA RED) end-to-end
  (2026-08-30, .venv → invest-geo) sobre el bbox de los Alpes: HydroSHEDS v1
  vía `/vsizip//vsicurl/` + filtro bbox. `rivers` → **100 líneas** HydroRIVERS
  reproyectadas a EPSG:32632 con atributos reales (`DIS_AV_CMS`, `ORD_STRA`,
  `NEXT_DOWN`, `HYBAS_L12`); `basins` `level=9` → **10 polígonos** HydroBASINS
  (`HYBAS_ID`, `PFAF_ID`, `NEXT_DOWN`). Auto-detección de `region` desde el
  centroide → `"eu"`. Salida GPKG y GeoJSON. Rápido y fiable (~3–8 s).
- **`export_datastack` / `import_datastack`** (`workspace/datastack.py`, stdlib
  puro) end-to-end (2026-08-30) con el registry real: `export_datastack(job_id=
  carbon-…)` sacó modelo+args del `datastack.json` del job → `.invest.json`
  válido (`model_id` + `invest_version` 3.20.1), marcó la ruta de un input
  fuera del sandbox sin bloquear; `import_datastack` lo reparseó → modelo
  resuelto, `required_missing []`, `files_missing []`, `ready`. `relative=True`
  reescribe rutas a `../data/…`.
- **Base de coeficientes citados** (`knowledge/coefficients/`, 2026-08-31):
  `kb.entry(name)` sirve cada JSON/MD como texto; `kb.index()` arma el catálogo.
  Verificado por los 11 tests: todo fichero parsea y trae las claves core; todo
  `source_key` usado (incl. `sources_used` del profile) existe en `sources.json`;
  toda entrada de bibliografía lleva `context`+`verified`+`scope`; cordura física
  (C ∈ [0,1], CN ordenado A≤B≤C≤D y ∈ (0,100], eff/proportion_subsurface ∈ [0,1]);
  `resources.register` cablea `invest://coefficients` + `.../{name}`;
  `coefficients_entry("bogus")` → JSON de error con la lista `known` (no lanza).
- **`check_table_vs_raster`** (`biotable.check_table` + `coefficients.column_ranges()`)
  end-to-end (2026-08-31, .venv → invest-geo para las clases del ráster) contra
  `Dummy_InVEST`: `Carbon_Pools.csv` limpio → `warning`/`pass` por 3 `orphan_rows`
  (95/100/200 no están en el LULC); `01-Biophysical_Table_Execution_SDR.csv`
  (tabla combinada) → 46 columnas ajenas al MODEL_SPEC de SDR listadas en
  `unexpected` (y NO se validan sus celdas), `usle_c=0` marcado `out_of_typical`
  (suelo citado 0.0001); tabla rota a propósito → `error` con `missing_rows`,
  `empty_required`, invariante `usle_c must be a fraction in [0,1]` y
  `out_of_typical`. +17 tests (`test_check_table.py`).
- **`recommend_model` `[prompt]` + `invest://model-guide` `[resource]`**
  (2026-08-31): la guía se generó contra la lista real de modelos instalados
  (`list_models()` → 26: awy, swy, sdr, ndr, carbon, fc, hq, hra, pollination,
  crop_production_*, recreation, scenic_quality, coastal_*, wave/wind_energy,
  urban_* (cooling/ufrm/stormwater/una/umh), delineateit, routedem,
  scenario_generator_proximity). +2 tests: la guía es markdown y nombra los
  `model_id` instalados + secciones answers/needs/gives + "Outside InVEST's
  scope"; el prompt sustituye `question`/`project_root`, apunta a
  `invest://model-guide` + `list_invest_models` + `project_readiness`, y su
  forma sin args dice "no project folder".
- **`clone_job`** end-to-end (2026-08-31, .venv → invest.exe): clon del job real
  `carbon-20260830T103940-174b40` (succeeded) con
  `overrides={carbon_pools_path: <copia de Carbon_Pools.csv>}` → job nuevo
  `carbon-20260831T015323-17fa42` → **succeeded en <60 s** (`wait_seconds=60`) con
  artefactos; `diff` = solo `carbon_pools_path` {from,to}, `arg_count` 2,
  `unchanged_arg_count` 1, `source_status` succeeded. +10 tests
  (`test_clone_job.py`): `_clone_args` (override pone/añade, drop quita,
  no-op no cuenta, `workspace_dir` siempre fuera) + guard rails del tool
  (job inexistente, datastack ausente, clon idéntico → error; submit stubeado
  recibe los args fusionados sin `workspace_dir`).
- **`build_report`** (`workspace/report.py::render` + tool) end-to-end
  (2026-08-31, .venv, sin `invest-geo`) sobre los jobs reales
  `carbon-20260830T103940-174b40` (baseline, con `summary.json`) +
  `carbon-20260830T115238-319176` (escenario de deforestación, con
  `compare_vs_...`): memo markdown con Overview, por job Parameters /
  Provenance (sha256 reales) / Outputs / Results (stats por ráster = 4 061 555.98,
  **cuadra** con la `raster_values_summary.csv`) / Figures, y la sección de
  comparación con la tabla Δ (−130 285.96, −3.2%, 1 717 px ↓) + zonal AOI.
  Rutas de figura absolutas al caer report en `Y:` y los PNG en `C:` (fallback
  cross-drive correcto). +12 tests (`test_report.py`): renderer puro (header/
  overview/footer, secciones opcionales, tabla de params + digest truncado,
  tabla de stats + CSV de InVEST, sección de comparación, log-tail en job
  fallido, `_num`/`_bytes`, escape de `|`) + tool (escribe el `.md`, `include_*`
  apagan secciones, guard rails).
- **`aggregate_to_units`** (`geo/aggregate.py` + tool + `_aggregate_raster_specs`)
  end-to-end (2026-08-31, .venv → subproceso → invest-geo) sobre
  `c_storage_bas.tif` del job `carbon-20260830T103940-174b40` + `SubBasin.shp`
  (EPSG:32733): 1 unidad, `count` 76 650 px y `mean` 50.62 **clavan** con el
  zonal ya verificado de `summarize_results`; `sum` 3 880 246.53 (< total InVEST
  4 061 555.98, la subcuenca no cubre todo el ráster); `val_carbon_baseline` =
  sum·50 = 194 012 326.60. `Basin.shp` + `all_touched=True` → 77 672 px /
  3 932 663.59; `area_weighted` con píxel de 1 ha no cambia la suma. Drivers
  `.gpkg`/`.geojson`/`.shp` + CSV tidy + `<dst>_aggregate.json`. +15 tests
  (`test_aggregate.py`): `_aggregate_raster_specs` (str/lista/dict, slug+dedup de
  labels, `value_per_unit` global vs override, errores) + payload building
  (`run_aggregate_to_units` stubeado) + guard rails del tool (rasters vacío,
  stat desconocido, sandbox in/out, `env_missing`, happy path con narrative).
- 220 tests en verde (214 pass — incl. +4 de PR #21 results_suffix y +7 de H3
  [`test_check_table` 22, `test_biotable` +2 → 12]; `test_aggregate` 15,
  `test_report` 12, `test_clone_job` 10, `test_knowledge_coefficients` 11,
  `test_hydrography` 16, `test_datastack` 15, +2 en `test_soil`; 6 skips: helpers numpy —
  `_ra_mm_per_day`, triángulo textural, EPIC K — con numpy ausente del `.venv`;
  se verifican en `invest-geo`). Registrado y "Connected" en Claude Code.

### Pendiente
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
9. **Playbooks** (prompts MCP) — **PARCIAL** (2026-08-31): `prepare_and_run_model`
   + `compare_land_use_scenarios` + `fill_biophysical_table` + `recommend_model`
   en `prompts.py`. Pendiente: playbook de calibración, playbook multi-servicio.
10. **Base de conocimiento** (resources) — **PARCIAL** (2026-08-31): `resources.py`
    con catálogo de modelos, cheat-sheet por modelo, convención de carpetas,
    catálogo de fuentes de datos y **`invest://model-guide`** (qué modelo para
    qué pregunta — HECHO). **+ base de coeficientes citados HECHA**
    (`knowledge/coefficients/`, resources `invest://coefficients` +
    `.../{name}`): 7 parámetros (`usle_c`, `usle_p`, `ndr_nutrient`,
    `curve_number`, `kc`, `root_depth`, `carbon_pools`) + `sources.json` +
    `README.md` + profile `moorabool_fs28`. Registros legend-agnósticos, cada
    valor con cita verificada. **+ `check_table_vs_raster` `[tool]` HECHO**
    (cruza una tabla rellena contra el spec, el ráster y los rangos citados del
    KB) **+ `fill_biophysical_table` `[prompt]`**. Pendiente: más parámetros
    (`eff`/`crit_len` de NDR fuera de zonas semi-áridas, C/P por más regiones),
    unidades por output, glosario.
11. **Cache content-addressed** por hash de inputs; **snapshots de JSON Schema** +
    diff en CI para detectar cambios breaking al subir versión de InVEST.
12. **conda-lock** (win-64 / linux-64 / osx-arm64) + imagen Docker (Linux, sin
    dependencia del Workbench, `invest` de conda-forge).

### Capacidades pendientes por etapa del flujo

Cada una es una capacidad **acotada y determinista** (o dato/receta), no "hacer el
servidor más listo" — el juicio y la orquestación viven en el cliente (ver §2 de
la discusión: MCP = superficie del dominio; agente = LLM que la usa). Tag:
`[tool]` función determinista · `[resource]` dato que el LLM lee · `[prompt]`
receta que el LLM sigue y adapta.

**Entrada de datos**
- ~~`[tool]` `fetch_dem`~~ **PARCIAL** (2026-08-30) — `geo/fetch.py` `op=dem`,
  `source="cop30"` (Copernicus GLO-30 del bucket AWS público, sin auth, `/vsicurl/`
  + `rio_merge`). Verificado end-to-end (ver §5). Pendiente: `source="srtm"`/
  `"nasadem"` (auth Earthdata, env var), `"opentopo"` (API key).
- ~~`[tool]` `fetch_landcover`~~ **PARCIAL** (2026-08-30) — `geo/fetch.py`
  `op=landcover`, `source="worldcover"` (ESA WorldCover 10 m 2020/2021 del bucket
  AWS público, sin auth). Comparte `_download_layer` con `fetch_dem`. Verificado
  end-to-end (ver §5). Pendiente: ESRI LC / Dynamic World.
- ~~`[tool]` `fetch_climate`~~ **PARCIAL** (2026-08-30) — `geo/climate.py`,
  `source="worldclim"` (WorldClim v2.1, sin auth, `/vsizip//vsicurl/`):
  `precipitation` + `eto` (Hargreaves), monthly/annual. Verificado end-to-end
  (ver §5). Pendiente: `source="terraclimate"` (años reales, netCDF — necesita
  sintaxis `NETCDF:` + subdataset), `source="chirps"` (trópicos).
- ~~`[tool]` `fetch_soil`~~ **PARCIAL** (2026-08-30) — `geo/soil.py`,
  `source="soilgrids"` (SoilGrids 2.0, sin auth, VRT global vía `/vsicurl/` +
  `WarpedVRT` IGH→EPSG:4326). `variable`: `texture` (sand/silt/clay % crudo) ·
  `hydrologic_soil_group` (HSG 1..4 del triángulo textural USDA — aprox.
  solo-textura) · `usle_k` (Williams/EPIC 1995 → SI) · `depth_to_bedrock`
  (SoilGrids 2017 `BDTICM`, GeoTIFF global en EPSG:4326, cm→mm para AWY
  `depth_to_root_rest_layer`; `_read_bdticm` reintenta la lectura de ventana).
  Verificado end-to-end (ver §5). Pendiente: PAWC (SoilGrids 2017 `AWCh1..3` /
  `WWP`), `stat` distinto de `mean` no probado, refinar HSG con Ksat +
  profundidad (HYSOGs250m / HiHydroSoil).
- ~~`[tool]` `fetch_hydrography`~~ **PARCIAL** (2026-08-30) — `geo/hydrography.py`,
  `source="hydrosheds"`: `product=rivers` (HydroRIVERS v1.0) / `basins`
  (HydroBASINS v1c standard, level 1..12). Shapefile del zip remoto vía
  `/vsizip//vsicurl/` + filtro bbox; `region` auto-detectada del centroide.
  Verificado end-to-end (ver §5). Pendiente: HydroBASINS "lake" / customized
  Pfafstetter, subcuencas anidadas, poblar `datasets` del manifest.
- Afinar `fetch_*`: chunking para AOIs grandes (mosaica en memoria); poblar
  `datasets` del `project.json` desde estas rutinas.
- ~~`[tool]` `delineate_watersheds`~~ **HECHO** (2026-08-30) — `geo/hydro.py`,
  cadena D8 de pygeoprocessing (fill_pits→flow_dir_d8→flow_accum→extract_streams_d8
  →snap outlets→delineate_watersheds_d8). Verificado end-to-end (ver §5).
  Pendiente de afinar: subcuencas anidadas (`calculate_subwatershed_boundary`),
  MFD opcional, poblar `datasets` del manifest.
- `[tool]` `build_aoi` — AOI desde punto+buffer / límite administrativo (GADM/GAUL)
  / bbox / snap a cuenca.
- `[tool]` `sanitize_layer` — arreglar nodata/dtype de rásters, geometrías
  inválidas, multipart→singlepart, encoding y nombres de columna de CSVs.

**Tablas biofísicas / lookup**
- ~~`[tool]` `tables_from_template`~~ **HECHO** (2026-08-30) — `workspace/biotable.py`
  + `op=raster_classes` en `prep.py` + `spec_translate.table_arg_specs`. Una fila
  por `lucode`, columnas del MODEL_SPEC, expande `[MONTH]`/`[SOIL_GROUP]`, leyenda
  opcional. Verificado end-to-end (ver §5).
- ~~`[resource]` base de coeficientes citados por clase de cobertura y región.~~
  **HECHO** (2026-08-31) — `knowledge/coefficients/` (`usle_c`, `usle_p`,
  `ndr_nutrient`, `curve_number`, `kc`, `root_depth`, `carbon_pools`) +
  `sources.json` (bibliografía verificada) + `README.md` + profile
  `moorabool_fs28`. Resources `invest://coefficients` + `.../{name}`. Registros
  indexados por atributos semánticos de cobertura, no por una leyenda; crosswalk
  solo orientativo; cada valor con `source_key`+`confidence`+`verified`.
  Verificado (11 tests). Pendiente: cobertura de más regiones/biomas, glosario.
- ~~`[tool]` `check_table_vs_raster`~~ **HECHO** (2026-08-31) —
  `workspace/biotable.py::check_table` (puro) + `coefficients.column_ranges()` +
  tool en `tools.py`. Cobertura (clases sin fila / filas huérfanas / duplicadas)
  vs el LULC, columnas required/inesperadas, celdas vacías/no-numéricas,
  invariantes duras + rangos típicos citados del KB (blandos). `severity`
  error/warning/ok. `+ [prompt] fill_biophysical_table` guía esqueleto →
  `invest://coefficients` → check. Verificado end-to-end (ver §5). Pendiente:
  cruzar `crit_len` contra la resolución del DEM, avisar de clases sin
  `native_veg` en modelos que lo piden.

**Elegir modelo / integración Workbench**
- ~~`[prompt]` `recommend_model`~~ **HECHO** (2026-08-31) — `prompts.py` +
  `invest://model-guide` `[resource]` (qué modelo responde a qué pregunta, los
  26 modelos instalados por dominio + fuera-de-alcance). El prompt: afinar la
  pregunta → guía + `list_invest_models` → confirmar cheat-sheets →
  `project_readiness` → recomendar (primario + complementos, entradas
  disponible/preparar/falta, salidas, baseline-vs-escenario) → hand-off.
- ~~`[tool]` `import_datastack` / `export_datastack`~~ **HECHO** (2026-08-30) —
  `workspace/datastack.py` (stdlib puro) + tools. Round-trip del parameter set
  `.invest.json` (`{args, model_id, invest_version}`) con el Workbench; tolera
  `model_name` legacy; `export` puede sacar modelo+args de un `job_id`;
  `relative=True` para stacks portables. Verificado end-to-end (ver §5).
  Pendiente: datastack "archive" (`.invest.tar.gz` con los datos empaquetados).
- ~~`[tool]` `clone_job`~~ **HECHO** (2026-08-31) — `tools.py` + helper puro
  `_clone_args`. Lee `model_id`+`args` del `datastack.json` del job, aplica
  `drop_args`+`overrides`, `submit` por el camino de `run_invest_model`. Devuelve
  `diff` + `cloned_from` + `source_status`; el `hint` propone el
  `compare_scenarios(baseline, clon)`. Verificado end-to-end (ver §5).
- `[tool]` `explain_provenance` — linaje completo de un resultado desde
  `provenance.json` + los pasos de `prep` que produjeron cada input.

**Salida / entregables**
- `[tool]` `compare_scenarios_multi` — N escenarios y/o N servicios en una tabla de
  trade-offs; ranking.
- ~~`[tool]` `aggregate_to_units`~~ **HECHO** (2026-08-31) — `geo/aggregate.py`
  (CORRE EN invest-geo) + `run_aggregate_to_units` + tool + `_aggregate_raster_specs`
  (puro). Roll-up zonal de 1+ rásters de servicio (o `diff_*.tif` de
  `compare_scenarios`) a polígonos de unidades; `stats` sum/mean/count/min/max/
  std/median por unidad×ráster; `val_<label>` = `value_per_unit`·sum (valoración
  $ simple, global con override por ráster); `area_weighted` ×área ha; reescribe
  el vector de unidades (columna por (ráster, stat) + `val_*`) + CSV tidy +
  `<dst>_aggregate.json`. Verificado end-to-end (ver §5). Pendiente: repartir a
  subunidades anidadas, valoración por tabla de precios en vez de constante,
  poblar `datasets` del manifest.
- ~~`[tool]` `build_report`~~ **HECHO** (2026-08-31) — `workspace/report.py`
  (`render(payload)`, stdlib puro) + tool. Memo **Markdown** para 1+ jobs:
  overview, y por job params + `provenance.json` (sha256) + catálogo de outputs
  + resultados desde el `summary.json` de `summarize_results` + la
  `raster_values_summary.csv` de InVEST + figuras de preview; sección de
  diferencia si hay `compare.json` entre dos de los jobs; log-tail para un job
  fallido. Solo colaciona. Convertir con `pandoc`. Verificado end-to-end (ver
  §5). Pendiente: HTML/PDF nativo (ahora vía pandoc), incrustar los PNG.
- `[tool]` `export_map` — GeoTIFF→PNG/GeoPDF con leyenda/basemap/AOI (mejor que el
  preview de debug); opcional proyecto QGIS.
- `[tool]` `run_uncertainty` — Monte Carlo perturbando coeficientes de tabla →
  bandas de confianza sobre las cifras titulares.

**Calibración (extensión)**
- `[tool]` split de validación / validación cruzada espacial, multi-aforo,
  multi-objetivo (Pareto), incertidumbre (GLUE/DREAM), artefacto de informe,
  warm-start desde una calibración previa, regionalización a cuencas no aforadas.

**Infraestructura transversal**
- `[tool]` `estimate_run_cost` — tiempo/RAM/disco desde extent+resolución antes de
  lanzar; aviso de runs gigantes.
- `[tool]` `manage_workspaces` — historial consultable, uso de disco, limpieza de
  runs viejos.
- Cache content-addressed (item 11), snapshots de schema + diff en CI (item 11),
  conda-lock + Docker (item 12).

### Convención de "proyecto InVEST" (IMPLEMENTADA en `workspace/project.py` + `scaffold_project`)
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
