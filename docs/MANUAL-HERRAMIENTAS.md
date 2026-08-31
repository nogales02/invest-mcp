# Manual de herramientas — invest-mcp

Referencia tipo *help* de **todo** lo que el servidor MCP pone sobre la mesa:
**36 tools**, **7 resources** y **4 prompts**. Pensado para consultarlo mientras
trabajas (tú o el asistente).

- ¿Instalar y conectar un cliente (Claude Desktop/Code, Ollama…)? → [`INSTALACION-PASO-A-PASO.md`](INSTALACION-PASO-A-PASO.md)
- ¿Referencia terse de config por cliente? → [`../INSTALL.md`](../INSTALL.md)
- ¿Diseño interno, decisiones de arquitectura, roadmap? → [`../CLAUDE.md`](../CLAUDE.md)

---

## Cómo leer este manual

Cada herramienta tiene una ficha con la misma forma:

> **Firma** · iconos · **Qué hace** · **Parámetros** (tabla) · **Devuelve** ·
> **Ejemplo** · **Notas / trampas**

Iconos:

| Icono | Significado |
|---|---|
| 🧩 | **stdlib puro** — corre en el servidor (`.venv`), no necesita nada más. |
| 🗺️ | Necesita el env conda **`invest-geo`** (GDAL/rasterio/pyproj…). Si falta, la tool devuelve `{"ok": false, "env_missing": true, ...}`. |
| 📊 | Necesita el env conda **`invest-cal`** (natcap.invest + spotpy) — solo calibración. |
| 🌐 | **Toca la red.** Descarga de buckets/servidores públicos, sin credenciales. Puede tardar o colgar si la fuente va lenta; reintentar. |
| ⚙️ | Lanza `invest.exe` por subproceso. |

Todas las tools devuelven un **dict JSON-able**. El éxito lleva `"ok": true`; el
error es `{"ok": false, "error": "..."}` (nunca una excepción que cruce el
límite MCP).

---

## 1. Conceptos que se repiten

**`args`** — El diccionario de argumentos **de InVEST tal cual**, con las claves
que espera cada modelo (`lulc_cur_path`, `dem_path`, `biophysical_table_path`…).
Las **rutas van absolutas**. Lo obtienes de `describe_invest_model(model_id)` →
`args_schema`.

**`workspace_dir`** — **Nunca lo pases.** El servidor crea y gestiona el
workspace de cada run. Si lo incluyes en `args`, se ignora.

**Sandbox / allow-list** — El servidor solo lee y escribe dentro de carpetas
"de confianza":

- siempre: `INVEST_MCP_DATA_ROOT` (`%USERPROFILE%\invest-mcp-data`) y el
  directorio de trabajo del servidor;
- las que añadas con `allow_input_dir(path)` (dura lo que dure la sesión);
- la `root` de un `scaffold_project(...)` (lectura **y** escritura).

Una ruta fuera de todo eso → `{"ok": false, "error": "... rejected ..."}` o
`sandbox_issues`. Solución: `allow_input_dir(carpeta_padre)`.

**Job** — Una ejecución (de modelo o de calibración). `run_invest_model` /
`clone_job` / `run_calibration` devuelven un **`job_id`**; luego consultas su
estado. Estados:

| Estado | Significado |
|---|---|
| `queued` | En cola (hay un `BoundedSemaphore` que limita la concurrencia, def. 2). |
| `running` | En marcha (un proceso del SO, supervisado por un hilo daemon). |
| `succeeded` | Terminó bien. **Terminal.** |
| `failed` | Terminó con error. **Terminal.** Mira `log_tail` / `get_invest_job_logs`. |
| `cancelled` | Lo mataste con `cancel_*`. **Terminal.** |

Los jobs persisten en `%USERPROFILE%\invest-mcp-data\jobs\<job_id>\`
(`job.json`, `datastack.json`, log, `provenance.json`, `workspace/`).

**`provenance.json`** — Se escribe al terminar cada run: versión de InVEST y del
MCP, plataforma, y el **sha256 de cada fichero de entrada**. Trazabilidad.

**Datastack (`.invest.json`)** — El "parameter set" del InVEST Workbench:
`{"model_id": ..., "args": {...}, "invest_version": ...}`. Es el **punto de
integración con el Workbench**: `import_datastack` lo lee, `export_datastack` lo
escribe.

**AOI** — *Area of Interest*, el vector de polígono(s) que delimita el caso de
estudio. Muchas tools lo reproyectan solas al CRS de la capa con la que operan.

**CRS proyectado** — En metros (UTM, p.ej. `EPSG:32618`), no geográfico
(`EPSG:4326`, grados). InVEST hidrológico lo exige. El preflight lo comprueba.

**Envs** — El servidor (`.venv`, Python 3.14) es ligero (`mcp` + `pydantic`).
Lo pesado vive en envs conda sidecar que se invocan por subproceso:
`invest-geo` (geoprocesado) e `invest-cal` (calibración). Comprueba cuáles hay
con `invest_env()`.

---

## 2. El flujo típico (mapa)

```
                        invest_env()  ─ ¿InVEST alcanzable? ¿qué envs hay?
                             │
   ┌─────────────────────────┼───────────────────────────────────┐
   │  ELEGIR MODELO          │  PREPARAR DATOS                    │
   │  list_invest_models     │  scaffold_project                 │
   │  describe_invest_model  │  fetch_dem / _landcover / _climate │
   │  (prompt recommend_model)│ / _soil / _hydrography  (🌐)      │
   │  (resource model-guide) │  reproject_layer / clip_to_aoi /  │
   │                         │  align_raster_stack               │
   │                         │  delineate_watersheds             │
   │                         │  project_readiness  ─ ¿qué falta?  │
   │                         │  tables_from_template →            │
   │                         │  (rellenar con invest://coefficients) │
   │                         │  check_table_vs_raster            │
   └─────────────────────────┴───────────────────────────────────┘
                             │
                   validate_invest_args   (schema + sandbox + invest validate + preflight)
                   preflight_geo          (chequeo geo profundo aparte)
                             │
                   run_invest_model  ──►  job_id
                             │
                   get_invest_job / get_invest_job_logs / list_invest_jobs
                             │
   ┌─────────────────────────┼───────────────────────────────────┐
   │  INTERPRETAR            │  ESCENARIOS                        │
   │  list_invest_job_artifacts │ clone_job (cambia el LULC)      │
   │  summarize_results      │  → run → compare_scenarios         │
   │  aggregate_to_units     │                                   │
   │  build_report           │  export_datastack → Workbench      │
   └─────────────────────────┴───────────────────────────────────┘
```

---

## 3. Tools

### 3.1 Administración

#### `invest_env()`
🧩 ⚙️

**Qué hace.** Confirma que el servidor alcanza `invest.exe` y reporta rutas,
versión y qué envs conda están disponibles. **Úsala primero.**

**Parámetros.** Ninguno.

**Devuelve.** `invest_exe`, `invest_version`, `geo_python` +
`geo_preflight_available` (bool), `cal_python` + `calibration_available` (bool),
`data_root`, `jobs_dir`, `allowed_input_roots` (lista), `max_concurrent_jobs`.

**Ejemplo.** `invest_env()`

**Notas.** Si `geo_preflight_available` es `false`, todo lo marcado 🗺️ fallará
con `env_missing`; instala con `invest-mcp setup --geo`.

---

#### `allow_input_dir(path)`
🧩

**Qué hace.** Añade una carpeta a la allow-list del sandbox **para el resto de
la sesión** (lecturas de entrada).

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `path` | ✔ | — | Carpeta a confiar. Absoluta. |

**Devuelve.** `added`, `allowed_input_roots` (lista actualizada).

**Ejemplo.** `allow_input_dir("D:/casos/rio_bogota")`

**Notas.** No es persistente: al reiniciar el servidor se pierde. Para algo fijo,
usa la env var `INVEST_MCP_ALLOWED_INPUT_DIRS` (`;` en Windows) o
`scaffold_project`.

---

### 3.2 Inventario de modelos

#### `list_invest_models()`
🧩 ⚙️

**Qué hace.** Lista todos los modelos InVEST instalados en este Workbench.

**Parámetros.** Ninguno.

**Devuelve.** `{"models": [{"model_id", "aliases", "title"}, ...]}`.

**Ejemplo.** `list_invest_models()`

**Notas.** Los `aliases` (p.ej. `awy` → `annual_water_yield`) sirven en cualquier
tool que pida `model_id`.

---

#### `describe_invest_model(model_id)`
🧩 ⚙️

**Qué hace.** Briefing completo de un modelo: para qué sirve, **JSON Schema de
sus `args`**, ficheros de entrada y salidas que produce.

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `model_id` | ✔ | — | id o alias. |

**Devuelve.** `title`, `about`, `userguide`, `validate_spatial_overlap`,
`different_projections_ok`, `briefing_markdown` (texto legible),
`args_schema` (JSON Schema: propiedades, tipos, `required`, descripciones),
`input_path_args` (qué args son rutas y de qué tipo), `outputs`.

**Ejemplo.** `describe_invest_model("sdr")`

**Notas.** En `args_schema.required` solo van los obligatorios "duros". Los
**condicionales** (p.ej. `required` = `"do_valuation"`) se documentan en la
`description`, no en `required`.

---

### 3.3 Validación

#### `validate_invest_args(model_id, args)`
🧩 ⚙️ 🗺️(opcional)

**Qué hace.** Chequeo en seco antes de gastar cómputo. Cuatro capas:
(1) campos `required` del schema, (2) que cada ruta de entrada cae en el
sandbox, (3) el propio `invest validate` (columnas, proyecciones, solapamiento),
(4) el preflight geoespacial (si `invest-geo` está).

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `model_id` | ✔ | — | id o alias. |
| `args` | ✔ | — | dict de args de InVEST (rutas absolutas). Sin `workspace_dir`. |

**Devuelve.** `ok` (bool global), `missing_required` (lista), `sandbox_issues`
(lista), `invest_warnings` (lista de `{args, message}`), `geo_preflight`
(`{ok, checks, note}` o `{skipped: "..."}`).

**Ejemplo.**
```json
validate_invest_args("carbon", {
  "lulc_cur_path": "C:/data/Dummy_InVEST/INPUTS/LULC/LULC.tif",
  "carbon_pools_path": "C:/data/Dummy_InVEST/INPUTS/Carbon_Pools.csv"
})
```

**Notas.** `invest validate` devuelve exit code ≠ 0 cuando hay avisos — no es
fallo del CLI, el JSON manda. Si `sandbox_issues` no está vacío, el preflight se
salta (no tiene sentido abrir ficheros ya rechazados).

---

#### `preflight_geo(model_id, args)`
🗺️

**Qué hace.** Solo el chequeo geoespacial **profundo**, aislado: CRS definido y
proyectado donde toca, unidades en metros, solapamiento real entre capas,
cordura del tamaño de píxel, nodata definido.

**Parámetros.** Igual que `validate_invest_args` (`model_id`, `args`).

**Devuelve.** `ok`, `checks` (lista de `{check, severity, detail, ...}` —
`crs_not_projected`, `crs_units_not_meters`, `crs_mismatch`,
`no_spatial_overlap`, `nodata_undefined`, `pixel_size_mismatch`…), `note`.
Si falta el env: `{"ok": false, "env_missing": true}`.

**Ejemplo.** `preflight_geo("sdr", args)`

**Notas.** `validate_invest_args` ya lo llama por dentro; usa `preflight_geo`
suelto cuando quieras iterar solo sobre lo geoespacial.

---

### 3.4 Ejecución de modelos

#### `run_invest_model(model_id, args, wait_seconds=0)`
⚙️

**Qué hace.** Arranca un run de InVEST. Devuelve el `job_id` de inmediato.

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `model_id` | ✔ | — | id o alias. |
| `args` | ✔ | — | dict de args (rutas absolutas). Sin `workspace_dir`. |
| `wait_seconds` | ✖ | `0` | Si > 0, bloquea hasta que el job **termine** (provenance escrita), máx. 3600. Útil para modelos rápidos (carbon ~4 s). |

**Devuelve.** El job (`job_id`, `status`, `workspace`, `datastack_path`,
`log_path`, `provenance_path`, `created_at`, …) + `hint`.

**Ejemplo.** `run_invest_model("carbon", args, wait_seconds=60)`

**Notas.** Las rutas se revalidan contra el sandbox aquí también. `wait_seconds`
usa un `threading.Event`: no hace *polling*, despierta cuando de verdad acaba.

---

#### `get_invest_job(job_id)`
🧩

**Qué hace.** Estado de un run. Al terminar añade un resumen de artefactos; si
falló, la cola del log.

**Parámetros.** `job_id` (✔).

**Devuelve.** El job + (si terminal) `artifacts_summary`, `invest_logs`;
(si `failed`) `log_tail` (60 líneas).

**Ejemplo.** `get_invest_job("carbon-20260830T103940-174b40")`

---

#### `get_invest_job_logs(job_id, tail_lines=200)`
🧩

**Qué hace.** stdout/stderr capturado del run.

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `job_id` | ✔ | — | |
| `tail_lines` | ✖ | `200` | Últimas N líneas; `0` = todas. |

**Devuelve.** `log_path`, `total_lines`, `lines` (lista).

---

#### `list_invest_jobs(limit=20)`
🧩

**Qué hace.** Runs recientes primero.

**Parámetros.** `limit` (✖, def. `20`).

**Devuelve.** `{"jobs": [ ... ]}` (mismos campos que `get_invest_job`, sin los
extras terminales).

---

#### `cancel_invest_job(job_id)`
⚙️

**Qué hace.** Mata un run en cola o en marcha (`taskkill /T`, kill real del
árbol de procesos).

**Parámetros.** `job_id` (✔).

**Devuelve.** El job con `status: "cancelled"`.

---

#### `clone_job(job_id, overrides=None, drop_args=None, wait_seconds=0)`
⚙️

**Qué hace.** Re-lanza un job previo **con algunos args cambiados**. Saca
`model_id` + `args` del `datastack.json` del job, aplica `drop_args` (quita) y
luego `overrides` (pone/añade), y hace `submit` por el mismo camino que
`run_invest_model` (checks de sandbox incluidos). Siempre quita `workspace_dir`.

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `job_id` | ✔ | — | Job origen. Puede estar en cualquier estado; no se toca. |
| `overrides` | ✖ | `None` | dict fusionado sobre los args (sustituye o añade). |
| `drop_args` | ✖ | `None` | Lista de nombres de arg a eliminar. |
| `wait_seconds` | ✖ | `0` | Como en `run_invest_model`. |

**Devuelve.** El job nuevo + `cloned_from`, `source_status`, `diff`
(`{arg: {from, to}}`; `to: null` si se quitó), `arg_count`,
`unchanged_arg_count`, `hint` (propone el `compare_scenarios`).

**Ejemplo.** Escenario de deforestación reusando todo lo demás:
```json
clone_job("carbon-20260830T103940-174b40",
          overrides={"lulc_cur_path": "C:/proj/data/processed/lulc_defor.tif"},
          wait_seconds=60)
```

**Notas.** Error si el clon quedaría **idéntico** (no pasaste nada que cambie) o
si el datastack no está en disco. Uso canónico: baseline + clon →
`compare_scenarios(baseline, clon)`.

---

### 3.5 Resultados y entregables

#### `list_invest_job_artifacts(job_id)`
🧩

**Qué hace.** Catálogo de **todos** los ficheros que produjo un run terminado.

**Parámetros.** `job_id` (✔).

**Devuelve.** `summary` (conteos por tipo), y listas de ficheros con `path`,
`kind` (`raster` / `vector` / `table` / `log` / `json` / …), `size`.

---

#### `summarize_results(job_id, aoi_path="", rasters=None, include_intermediate=False, make_preview=True)`
🗺️

**Qué hace.** Resumen de un run terminado: stats por ráster de salida, zonal por
feature sobre un AOI, la `raster_values_summary.csv` de InVEST si existe, un
digest en lenguaje natural y un PNG de preview del ráster principal. Escribe
`<jobdir>/summary/summary.json`.

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `job_id` | ✔ | — | Debe estar `succeeded`. |
| `aoi_path` | ✖ | `""` | Vector de polígonos (en el sandbox). Se reproyecta solo al CRS de cada ráster. |
| `rasters` | ✖ | `None` | Limita a estos ficheros de salida (rutas relativas o nombres). Def.: todos los de nivel superior. |
| `include_intermediate` | ✖ | `false` | También `intermediate_outputs/`. |
| `make_preview` | ✖ | `true` | Renderiza el PNG (subproceso aislado; si crashea solo se pierde el PNG). |

**Devuelve.** `rasters` (por cada uno: `valid_count`/`nodata`, `min`/`max`/
`mean`/`std`/`sum`, histograma 10-bins), `aoi` (zonal por feature),
`invest_raster_values_summary`, `preview`, `sidecar_json`, `narrative`.

**Ejemplo.**
```json
summarize_results("carbon-20260830T103940-174b40",
                  aoi_path="C:/data/Dummy_InVEST/SubBasin.shp")
```

---

#### `compare_scenarios(baseline_job_id, scenario_job_id, aoi_path="", rasters=None, include_intermediate=False, make_preview=True)`
🗺️

**Qué hace.** Baseline vs escenario alternativo del **mismo modelo** — el
propósito de InVEST (tradeoffs). Por cada ráster de salida presente en ambos:
alinea el escenario a la malla del baseline, escribe `diff_<nombre>.tif` =
`escenario − baseline`, y reporta total antes/después, Δ y % de cambio, px que
suben/bajan/igual, histograma de Δ, zonal de Δ por feature. Escribe
`<scen_jobdir>/compare_vs_<baseline_job_id>/compare.json`.

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `baseline_job_id` | ✔ | — | Job `succeeded`. |
| `scenario_job_id` | ✔ | — | Job `succeeded` **del mismo modelo**. |
| `aoi_path` | ✖ | `""` | Vector de polígonos para el zonal de Δ. |
| `rasters` | ✖ | `None` | Limita a estos; def.: los que comparten ambos runs. |
| `include_intermediate` | ✖ | `false` | También diferencia `intermediate_outputs/`. |
| `make_preview` | ✖ | `true` | Preview con colormap divergente (RdBu_r centrado en 0). |

**Devuelve.** `pairs` (por ráster: `delta_sum`, `pct_change`, `increased_px` /
`decreased_px` / `unchanged_px`, `delta_min`/`delta_max`/`delta_mean`, si hubo
resample), `only_in_baseline` / `only_in_scenario`, `aoi`, `preview`,
`sidecar_json`, `narrative`.

**Ejemplo.** `compare_scenarios(base_id, scen_id, aoi_path=".../SubBasin.shp")`

**Notas.** Falla si los jobs son de modelos distintos, si alguno no está
`succeeded`, o si son el mismo job. Los `diff_*.tif` son la entrada típica de
`aggregate_to_units` ("¿qué le deja este cambio a cada municipio?").

---

#### `aggregate_to_units(rasters, units_path, dst_path, id_columns=None, stats=None, value_per_unit=None, area_weighted=False, all_touched=False, value_currency="USD", max_units=5000)`
🗺️

**Qué hace.** **Entregable de reparto.** Roll-up zonal de 1+ rásters de servicio
(una salida de InVEST, o un `diff_*.tif` de `compare_scenarios`) a los polígonos
de `units_path` (municipios, predios, huella de intervención). Por unidad×ráster
calcula `stats` y, opcionalmente, un valor monetario simple. Reescribe el vector
de unidades con una columna por (ráster, stat) + `val_*`, un CSV *tidy* al lado
y `<dst>_aggregate.json`.

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `rasters` | ✔ | — | Un path, o lista de paths / dicts `{path, label?, units?, value_per_unit?}`. |
| `units_path` | ✔ | — | Vector de polígonos de las unidades (en el sandbox). Se reproyecta solo. |
| `dst_path` | ✔ | — | Vector de salida: `.gpkg` (recomendado) / `.shp` (trunca nombres largos) / `.geojson`. |
| `id_columns` | ✖ | `None` | Columnas de id a arrastrar. Def.: los 4 primeros atributos. |
| `stats` | ✖ | `["sum","mean","count"]` | Cualquiera de `sum` `mean` `count` `min` `max` `std` `median`. |
| `value_per_unit` | ✖ | `None` | Valor por unidad-de-valor del ráster (p.ej. USD por tonelada). `val_<label> = value_per_unit · sum`. Global; un `value_per_unit` dentro de un dict de `rasters` lo pisa. |
| `area_weighted` | ✖ | `false` | Multiplica cada píxel por su área en ha antes de sumar (para rásters de densidad por-ha). Se ignora en CRS geográfico. |
| `all_touched` | ✖ | `false` | Cuenta todo píxel que toque el borde del polígono, no solo los de centro dentro. |
| `value_currency` | ✖ | `"USD"` | Etiqueta de moneda en la salida. |
| `max_units` | ✖ | `5000` | Corta (con aviso) por encima de N unidades. |

**Devuelve.** `rasters` (totales por ráster, incl. `value_total`), `features`
(filas por unidad), `csv_path`, `output_vector`, `narrative`.

**Ejemplo.** Repartir el Δ de carbono de un escenario a municipios, valorado a
50 USD/t:
```json
aggregate_to_units(
  rasters="C:/proj/jobs/scen/compare_vs_base/diff_c_storage_bas.tif",
  units_path="C:/proj/data/municipios.gpkg",
  dst_path="C:/proj/out/reparto_carbono.gpkg",
  id_columns=["MPIO_CDPMP", "MPIO_CNMBR"],
  value_per_unit=50, value_currency="USD")
```

**Notas.** `count` y `mean` cuadran exacto con el zonal de `summarize_results`.

---

#### `build_report(job_ids, dst_path, title="", include_args=True, include_provenance=True, include_artifacts=True, include_comparisons=True)`
🧩

**Qué hace.** Memo de **métodos + resultados en Markdown** para uno o varios
jobs, ensamblado de lo que **ya hay en disco**. Colaciona y formatea — **no
calcula ni interpreta**. Por job: metadatos del run, `args` del datastack,
`provenance.json` (versiones + sha256), catálogo de artefactos, el
`summary.json` de `summarize_results` + la `raster_values_summary.csv` de
InVEST, y el PNG de preview. Si dos jobs dados tienen un `compare_scenarios`
entre ellos, añade la sección de diferencia. Un job no-`succeeded` recibe la
cola de su log.

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `job_ids` | ✔ | — | Uno o una lista (conserva el orden). |
| `dst_path` | ✔ | — | Termina en `.md`, bajo carpeta permitida. |
| `title` | ✖ | auto | Título del memo. |
| `include_args` | ✖ | `true` | Sección de parámetros. |
| `include_provenance` | ✖ | `true` | Sección de procedencia (sha256). |
| `include_artifacts` | ✖ | `true` | Catálogo de salidas. |
| `include_comparisons` | ✖ | `true` | Sección de diferencia si hay `compare.json`. |

**Devuelve.** `path`, `bytes`, `jobs`, `comparisons`, `figures`, `notes`,
`hint` (comando `pandoc` para PDF/HTML).

**Ejemplo.**
```json
build_report(["carbon-...base", "carbon-...scen"],
             dst_path="C:/proj/logs/informe_carbono.md",
             title="Carbono — baseline vs deforestación")
```

**Notas.** Para que la sección de resultados salga rellena, corre antes
`summarize_results` (y `compare_scenarios` para la de diferencia). Convertir a
PDF: `pandoc informe_carbono.md -o informe.pdf`.

---

### 3.6 Preparación de datos — estructura de proyecto

#### `scaffold_project(root, name="", target_crs="", aoi_path="", overwrite=False)`
🧩

**Qué hace.** Crea el árbol de "proyecto InVEST" y un `project.json`, y **añade
`root` a la allow-list de la sesión (lecturas y escrituras)**.

Árbol: `data/raw/` (descargas, inmutable), `data/processed/` (derivado:
reproyectado/recortado/alineado), `tables/`, `datastacks/`, `jobs/`, `logs/`.

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `root` | ✔ | — | Ruta absoluta del proyecto (se crea si no existe). |
| `name` | ✖ | nombre de carpeta | Etiqueta del manifiesto. |
| `target_crs` | ✖ | `""` | CRS en el que se trabajará el caso (p.ej. `"EPSG:32618"`). Se anota, **no se impone aquí**. |
| `aoi_path` | ✖ | `""` | AOI a registrar. Debe caer ya en una carpeta permitida. |
| `overwrite` | ✖ | `false` | Solo reescribe el `project.json` (las carpetas se dejan). |

**Devuelve.** `root`, `allowed_input_roots`, `subdirs`, ruta del manifiesto.

**Ejemplo.**
```json
scaffold_project("D:/casos/rio_frio", name="Río Frío",
                 target_crs="EPSG:32618",
                 aoi_path="D:/casos/rio_frio/data/raw/aoi.gpkg")
```

**Notas.** Idempotente. Lee `invest://conventions` para el significado de cada
carpeta.

---

#### `project_readiness(root, models=None)`
🧩

**Qué hace.** Escanea `data/` + `tables/` del proyecto, **adivina el rol** de
cada fichero por su nombre (`dem`, `lulc`, `watersheds`, `biophysical_table`,
`soil_group`, `precipitation`…) y lo casa contra los inputs **required** de cada
modelo. **Apoya** la decisión de qué correr, no la toma; cada acierto/duda/hueco
queda expuesto.

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `root` | ✔ | — | Proyecto ya scaffoldeado (necesita `project.json`). |
| `models` | ✖ | `None` | Limita a estos ids/alias. Def.: shortlist de instalados (`carbon`, `annual_water_yield`, `seasonal_water_yield`, `sdr`, `ndr`, `pollination`, `urban_flood_risk_mitigation`). |

**Devuelve.** `inventory` (ficheros + rol adivinado), `assessments` (por modelo:
`matched` / `ambiguous` / `missing` / `needs_values`), `ready_to_attempt`,
`gaps_by_model`, `narrative`.

**Ejemplo.** `project_readiness("D:/casos/rio_frio", models=["sdr", "ndr"])`

**Notas.** El matching es por *keywords* en el nombre del fichero, a propósito
best-effort. Los inputs numéricos/opción van en `needs_values` y **no bloquean**.

---

### 3.7 Preparación de datos — descarga (tocan la red)

Todas: 🌐 🗺️. El área se da con `aoi_path` (un vector; sus *bounds* mandan) y/o
`bbox = [minx, miny, maxx, maxy]` **en lon/lat, EPSG:4326**. `buffer_deg` (def.
`0.05`) pad. `target_crs` / `target_resolution` reproyectan el resultado.
`keep_intermediate` (def. `false`) conserva los ficheros de paso.

#### `fetch_dem(dst_path, aoi_path="", bbox=None, target_crs="", target_resolution=None, clip_to_aoi=True, buffer_deg=0.05, resampling="bilinear", source="cop30", keep_intermediate=False)`

**Qué hace.** Descarga un DEM y lo deja como GeoTIFF.

**Fuente.** `source="cop30"` (única cableada): **Copernicus DEM GLO-30** (~30 m),
bucket AWS público `copernicus-dem-30m` (sin credenciales; solo contacta
`copernicus-dem-30m.s3.amazonaws.com`).

**Parámetros propios.** `resampling` def. `bilinear` (continuo). `clip_to_aoi`
def. `true` → enmascara al polígono del AOI.

**Devuelve.** `dst`, `tiles_used`, `tiles_missing` (tiles oceánicos / fuera de
cobertura), `provider_host`, descripción del ráster.

**Ejemplo.**
```json
fetch_dem("D:/casos/rio_frio/data/raw/dem.tif",
          aoi_path="D:/casos/rio_frio/data/raw/aoi.gpkg",
          target_crs="EPSG:32618", target_resolution=[30, 30])
```

---

#### `fetch_landcover(dst_path, aoi_path="", bbox=None, year=2021, target_crs="", target_resolution=None, clip_to_aoi=True, buffer_deg=0.05, resampling="nearest", source="worldcover", keep_intermediate=False)`

**Qué hace.** Descarga un ráster de cobertura y lo deja como GeoTIFF.

**Fuente.** `source="worldcover"` (única): **ESA WorldCover 10 m**
(`year` = `2020` o `2021`, 11 clases), bucket AWS público `esa-worldcover`.

**Parámetros propios.** `year` (def. `2021`). `resampling` def. `nearest`
(categórico — **mantenlo así** al reproyectar).

**Devuelve.** `dst`, `class_legend` (valor → etiqueta; listo para
`tables_from_template(legend_path=...)`), descripción del ráster.

---

#### `fetch_climate(dst_path, variable, aoi_path="", bbox=None, source="worldclim", period="monthly", months=None, resolution="10m", target_crs="", target_resolution=None, clip_to_aoi=True, buffer_deg=0.05, resampling="bilinear", keep_intermediate=False)`

**Qué hace.** Descarga la climatología que necesitan los modelos de agua.

**Fuente.** `source="worldclim"` (única): **WorldClim v2.1**, climatología
mensual 1970–2000 (contacta `geodata.ucdavis.edu`).

**Parámetros propios.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `variable` | ✔ | — | `"precipitation"` (WorldClim `prec`, mm) o `"eto"` (ETo por **Hargreaves-Samani** desde `tmin`/`tmax`/`tavg` — *modelada*, no medida). |
| `period` | ✖ | `"monthly"` | `"monthly"` → 12 rásters (forma de SWY); `dst_path` **debe** llevar `{month}` (p.ej. `precip_{month}.tif`). `"annual"` → 1 ráster = suma de los 12 (forma de AWY). |
| `months` | ✖ | `None` | Subconjunto, p.ej. `[6,7,8]`. |
| `resolution` | ✖ | `"10m"` | `10m` (~18 km, rápido) / `5m` / `2.5m` / `30s` (~1 km). |

**Ejemplo.**
```json
fetch_climate("D:/casos/rio_frio/data/raw/precip_{month}.tif",
              variable="precipitation", aoi_path=".../aoi.gpkg",
              period="monthly", resolution="30s", target_crs="EPSG:32618")
```

**Notas.** Si tienes una malla local de ETo, úsala en vez de la de Hargreaves.

---

#### `fetch_soil(dst_path, variable, aoi_path="", bbox=None, source="soilgrids", depth="0-5cm", stat="mean", target_crs="", target_resolution=None, clip_to_aoi=True, buffer_deg=0.05, resampling="", keep_intermediate=False)`

**Qué hace.** Descarga rásters de suelo.

**Fuente.** `source="soilgrids"` (única): **SoilGrids 2.0** (ISRIC, 250 m,
contacta `files.isric.org`); `depth_to_bedrock` viene de **SoilGrids 2017**
(`BDTICM`).

**Parámetros propios.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `variable` | ✔ | — | `"texture"` (sand/silt/clay %, 3 rásters — `dst_path` **debe** llevar `{fraction}`) · `"hydrologic_soil_group"` (HSG 1..4 = A..D, del triángulo textural USDA — aproximación solo-textura; uint8) · `"usle_k"` (erodibilidad K para SDR, SI, Williams/EPIC 1995) · `"depth_to_bedrock"` (profundidad a lecho, cm→mm, para AWY `depth_to_root_rest_layer`; `depth`/`stat` se ignoran). |
| `depth` | ✖ | `"0-5cm"` | `0-5cm` / `5-15cm` / `15-30cm` / `30-60cm` / `60-100cm` / `100-200cm`. |
| `stat` | ✖ | `"mean"` | `mean` / `Q0.05` / `Q0.5` / `Q0.95`. |
| `resampling` | ✖ | auto | Vacío = según variable (`nearest` para HSG). |

**Ejemplo.**
```json
fetch_soil("D:/casos/rio_frio/data/raw/soil_{fraction}.tif",
           variable="texture", aoi_path=".../aoi.gpkg",
           depth="0-5cm", target_crs="EPSG:32618")
```

**Notas.** Leer el VRT global de ISRIC tarda ~2–3 min para un AOI pequeño (no es
cuelgue). El GeoTIFF de `depth_to_bedrock` (~8.5 GB) abre lento (~2 min) y la
lectura reintenta 4×.

---

#### `fetch_hydrography(dst_path, product, aoi_path="", bbox=None, source="hydrosheds", region="", level=8, target_crs="", clip_to_aoi=False, buffer_deg=0.05, keep_intermediate=False)`

**Qué hace.** Descarga red de ríos o polígonos de cuenca como vector.

**Fuente.** `source="hydrosheds"` (única): **HydroSHEDS v1** (WWF/McGill,
contacta `data.hydrosheds.org`; rápido y fiable).

**Parámetros propios.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `product` | ✔ | — | `"rivers"` (HydroRIVERS v1.0 — líneas con `DIS_AV_CMS`, `UPLAND_SKM`, orden Strahler, `NEXT_DOWN`) o `"basins"` (HydroBASINS v1c standard — polígonos). |
| `region` | ✖ | `""` (auto) | Código continental HydroSHEDS: `af ar as au eu gr na sa si`. Vacío = auto-detección por el centroide del AOI; si es ambiguo (p.ej. Oriente Medio), el error te pide pasarlo. |
| `level` | ✖ | `8` | Solo `basins`: nivel Pfafstetter `1..12` (1 = escala continental, 12 = subcuencas mínimas). |
| `clip_to_aoi` | ✖ | `false` | `false` = features enteras que intersectan el bbox; `true` = recorte geométrico al AOI (los ríos se truncan). |

**Ejemplo.** `fetch_hydrography(".../rios.gpkg", product="rivers", aoi_path=".../aoi.gpkg", target_crs="EPSG:32618")`

---

### 3.8 Preparación de datos — transformaciones geo

Todas 🗺️. Entradas y salidas deben caer en el sandbox.

#### `reproject_layer(src_path, dst_path, target_crs, resampling="nearest", resolution=None)`

**Qué hace.** Reproyecta un ráster **o** vector a `target_crs`.

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `src_path` | ✔ | — | Ráster o vector. |
| `dst_path` | ✔ | — | Salida. |
| `target_crs` | ✔ | — | EPSG (`"EPSG:32618"`), WKT o proj string. |
| `resampling` | ✖ | `"nearest"` | Solo ráster: `nearest` para categóricos (land cover), `bilinear`/`cubic`/`average` para continuos. |
| `resolution` | ✖ | `None` | Solo ráster: `[x, y]` tamaño de píxel objetivo en unidades del CRS destino. |

---

#### `clip_to_aoi(src_path, dst_path, aoi_path, all_touched=False)`

**Qué hace.** Recorta un ráster (crop al bbox del AOI + máscara fuera del
polígono) o vector (`gpd.clip`) al polígono de `aoi_path`. El AOI se reproyecta
solo al CRS de la capa.

**Parámetros.** `src_path`, `dst_path`, `aoi_path` (✔); `all_touched` (✖, def.
`false`) = conserva todo píxel que toque el borde.

---

#### `align_raster_stack(rasters, reference_path="", target_crs="", resolution=None, extent=None, resampling="nearest")`

**Qué hace.** Pone varios rásters en **una malla idéntica** (mismo CRS + píxel +
extent + alineación) para que InVEST los apile.

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `rasters` | ✔ | — | Lista de `{"src": ..., "dst": ...}`. |
| `reference_path` | ✖* | `""` | Un ráster cuya malla se copia exacta. |
| `target_crs` + `resolution` + `extent` | ✖* | — | Alternativa a `reference_path`: hay que dar **los tres** (`extent` = `[minx, miny, maxx, maxy]`). |
| `resampling` | ✖ | `"nearest"` | Se aplica a **todos**. Corre la tool dos veces si mezclas categóricos y continuos. |

\* Se necesita **o** `reference_path` **o** el trío `target_crs`+`resolution`+`extent`.

**Ejemplo.**
```json
align_raster_stack(
  [{"src": ".../dem.tif", "dst": ".../processed/dem.tif"},
   {"src": ".../lulc.tif", "dst": ".../processed/lulc.tif"}],
  reference_path=".../dem.tif")
```

---

#### `delineate_watersheds(dem_path, outlets_path, dst_path, threshold_flow_accumulation=1000, snap_distance_px=10, fill_pits=True, keep_intermediate=False)`

**Qué hace.** Corta polígonos de cuenca aguas arriba de puntos de salida con la
cadena **D8 de pygeoprocessing** (mismo motor que InVEST → las cuencas cuadran
con el routing de SDR/NDR/SWY): fill_pits → flow_dir_d8 → flow_accum → streams →
snap de cada outlet a la red → `delineate_watersheds_d8`.

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `dem_path` | ✔ | — | DEM **proyectado** (metros). |
| `outlets_path` | ✔ | — | Vector de puntos. Se reproyecta solo al CRS del DEM. |
| `dst_path` | ✔ | — | `.gpkg` / `.shp` / `.geojson`. |
| `threshold_flow_accumulation` | ✖ | `1000` | Píxeles acumulados para considerar "cauce". |
| `snap_distance_px` | ✖ | `10` | Radio (en px) para pegar cada outlet al cauce más cercano. `0` desactiva el snap. |
| `fill_pits` | ✖ | `true` | Rellenar depresiones del DEM antes. |

**Devuelve.** Descripción del vector de cuencas + `snap_report` por punto
(distancia movida). Intermedios en `<dst_stem>_hydro/` (se borran salvo
`keep_intermediate`).

---

### 3.9 Tablas biofísicas / lookup

#### `tables_from_template(model_id, lulc_path, dst_path, table_arg="", legend_path="", include_optional=True, args=None, max_classes=1000)`
🗺️

**Qué hace.** Escribe el **esqueleto** de una tabla biofísica/lookup: una fila
por lucode único del LULC, con las columnas que pide el `MODEL_SPEC` del modelo.
Las celdas de coeficiente van **en blanco** para que las rellenes.

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `model_id` | ✔ | — | id o alias. |
| `lulc_path` | ✔ | — | Ráster de cobertura (para leer sus clases). |
| `dst_path` | ✔ | — | CSV de salida. |
| `table_arg` | ✖ | auto | Qué CSV templetar (p.ej. `biophysical_table_path`). Auto-detecta el que va por `lucode`; si hay varios, el error los lista. |
| `legend_path` | ✖ | `""` | CSV con `code,label` en las 2 primeras columnas → añade columna `description`. |
| `include_optional` | ✖ | `true` | Emitir también columnas solo-opcionales. Las **condicionales** (p.ej. `load_n` de NDR con `calc_n`) se emiten siempre y se marcan en `column_help`. |
| `args` | ✖ | `null` | El dict `args` con el que vas a correr. Si lo pasas, las columnas condicionales se resuelven contra él: `{"calc_n": true, "calc_p": false}` emite `load_type_n`/`load_n`/… como `required` y **descarta** las columnas de fósforo. Sin `args`, todas se emiten como `required if: <cond>`. |
| `max_classes` | ✖ | `1000` | Corta la lista de clases. |

**Devuelve.** `headers`, `column_help` (about / units / requirement por
columna), `classes` (valor + nº de píxeles), `key_column`, `resolved_conditions`,
`notes`, `narrative`. Expande `[MONTH]` → `_1..12` y `[SOIL_GROUP]` → `_a..d`.

**Ejemplo.**
```json
tables_from_template("carbon",
  lulc_path="C:/data/Dummy_InVEST/INPUTS/LULC/LULC.tif",
  dst_path="C:/proj/tables/carbon_pools.csv")
```

---

#### `check_table_vs_raster(model_id, table_path, lulc_path="", table_arg="", include_optional=True, args=None, max_classes=1000)`
🗺️ (solo si pasas `lulc_path`)

**Qué hace.** Valida una tabla biofísica/lookup **rellena** antes de correr.

**Chequeos** (en `checks`):

- `coverage` (necesita `lulc_path`): clases del ráster sin fila (`missing_rows`),
  filas para códigos ausentes (`orphan_rows`), claves duplicadas.
- `columns`: `missing` (required que faltan), `unexpected` (extras — solo se
  validan celdas de columnas que el modelo consume).
- `cells`: `empty_required`, `non_numeric`.
- `ranges`: `invariant_violations` (duras: fracciones ∈ [0,1], curve numbers
  ordenados A≤B≤C≤D ∈ (0,100], loads/depths ≥ 0, root_depth entero) ·
  `out_of_typical` (blandas: fuera de la banda citada del KB, con el `resource`
  fuente).

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `model_id` | ✔ | — | |
| `table_path` | ✔ | — | La tabla rellena. |
| `lulc_path` | ✖ | `""` | Sin él: solo estructura + valores (no necesita `invest-geo`). Con él: + cobertura. |
| `table_arg` | ✖ | auto | Igual regla que `tables_from_template`. |
| `include_optional` | ✖ | `true` | |
| `args` | ✖ | `null` | El dict `args` con el que vas a correr. Resuelve las columnas condicionales del modelo: con `{"calc_n": true}` las columnas de NDR `load_type_n`/`load_n`/`eff_n`/`crit_len_n`/`proportion_subsurface_n` pasan a **required duras** (faltan → `missing`, en blanco → `empty_required`); con `{"calc_p": false}` las de fósforo se ignoran. Las condiciones no nombradas en `args` quedan como aviso (`conditional_columns`). |

**Devuelve.** `severity` (`error` bloqueante / `warning` revisar / `ok`), `pass`
(= `severity != error`), `checks` (detalle), `conditional_columns`,
`enforced_conditions` (las que activó `args`), `narrative`.

**Notas.** Cierra el lazo: `tables_from_template` → rellenar desde
`invest://coefficients` → `check_table_vs_raster` → `validate_invest_args`. El
prompt `fill_biophysical_table` lo guía.

---

### 3.10 Datastack (round-trip con el Workbench)

#### `import_datastack(src_path)`
🧩

**Qué hace.** Lee un datastack `.invest.json` (p.ej. uno que guardó el
Workbench) y reporta qué trae: modelo, `args`, `required_missing`, y por cada
ruta si existe en disco y si cae en el sandbox.

**Parámetros.** `src_path` (✔).

**Devuelve.** `model_id`, `model_title`, `args` (con rutas absolutizadas contra
la carpeta del datastack), `required_missing`, `path_args` (por arg: `exists`,
`in_sandbox`), `files_missing`, `paths_outside_sandbox`, `ready`, `next`.

**Notas.** Tolera `model_name` legacy. Enchufa directo con
`validate_invest_args(model_id, args)` → `run_invest_model`.

---

#### `export_datastack(dst_path, model_id="", args=None, job_id="", relative=False)`
🧩 ⚙️

**Qué hace.** Escribe un datastack `.invest.json` que el Workbench abre
directamente — el **hand-off** con el Workbench.

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `dst_path` | ✔ | — | Termina en `.json` (convención: `.invest.json`). |
| `model_id` + `args` | ✖* | — | Modelo + dict de args de InVEST. |
| `job_id` | ✖* | `""` | Alternativa: saca modelo + args del datastack de un run. |
| `relative` | ✖ | `false` | `true` reescribe las rutas de fichero relativas a `dst_path` (stack portable para zippear). |

\* Da **o** `model_id` (+ `args`) **o** un `job_id`.

**Devuelve.** `path`, `model_id`, `invest_version`, `arg_count`, `made_relative`,
`path_args` (existencia reportada, no bloquea).

**Ejemplo.** `export_datastack("C:/proj/datastacks/sdr.invest.json", job_id="sdr-...")`

---

### 3.11 Calibración (AWY / SWY / SDR / NDR)

Motor compartido con el plugin de calibración del Workbench del usuario (env
`invest-cal`). Modelos: **AWY, SWY, SDR, NDR_N, NDR_P**.

#### `validate_calibration_config(config)`
📊

**Qué hace.** Chequea una config de calibración sin correrla: modelo / params /
objetivo, columnas de `Obs_Data`, flags `Status_Cal_*` de la tabla biofísica,
caps de factores, y que todo path esté en el sandbox.

**Parámetros.** `config` (✔) — mismo shape que `run_calibration` (ver abajo).

**Devuelve.** `ok`, `issues` (lista, con `level`), `sandbox_issues`.

---

#### `run_calibration(model, parameters, objective, optimizer, observed_data_path, model_inputs, results_suffix="", make_plots=True, wait_seconds=0)`
📊

**Qué hace.** Job de calibración (spotpy sobre InVEST). Devuelve un `job_id`.

**Parámetros.**

| nombre | req | por defecto | qué es |
|---|---|---|---|
| `model` | ✔ | — | `AWY` / `SWY` / `SDR` / `NDR_N` / `NDR_P`. |
| `parameters` | ✔ | — | `{nombre: {"min": .., "max": .., "value": ..}}`. Claves por modelo: **AWY** `Z`, `Factor-Kc` · **SWY** `Alpha`, `Beta`, `Gamma`, `Factor-Kc_m` · **SDR** `sdr_max`, `Borselli-K_SDR`, `IC0`, `L_max`, `Factor-C`, `Factor-P` · **NDR_N** `SubCri_Len_N`, `Sub_Eff_N`, `Borselli-K_NDR`, `Factor_Load_N`, `Factor_Eff_N` · **NDR_P** análogo con `_P`. |
| `objective` | ✔ | — | `MSE` / `MAE` / `RMSE` / `RRMSE`. |
| `optimizer` | ✔ | — | `{"method": "DDS"\|"LHS"\|"SCE-UA", "n_simulations": >=10, "seed": ..}`. |
| `observed_data_path` | ✔ | — | CSV con `ws_id` + una columna con nombre tipo el modelo. |
| `model_inputs` | ✔ | — | Los inputs de InVEST (rutas absolutas) + `threshold_flow_accumulation`. |
| `results_suffix` | ✖ | `""` | Sufijo para los workspaces. |
| `make_plots` | ✖ | `true` | Intenta los *dotty plots* (JPG en subproceso aislado; siempre escribe el JSON). |
| `wait_seconds` | ✖ | `0` | Bloquea hasta terminar, máx. 3600. |

**Devuelve.** El job + `hint`.

---

#### `get_calibration_job(job_id)`
🧩

**Qué hace.** Estado de un job de calibración. Mientras corre: últimas
iteraciones (`progress`). Al terminar: `result` con `best_parameters`,
`best_objective`, `obs_vs_sim`, `diagnostics` (sensibilidad Spearman por
parámetro), `n_iterations`, `warnings`, `artifacts`, y `dotty_data` (rutas a
JSON ploteables).

**Parámetros.** `job_id` (✔).

---

#### `cancel_calibration_job(job_id)`
📊

**Qué hace.** Mata un job de calibración en marcha.

**Parámetros.** `job_id` (✔).

---

## 4. Resources

Material de **referencia** que el cliente lee sin gastar una tool. Solo datos,
nunca decisiones.

| URI | Tipo | Qué trae |
|---|---|---|
| `invest://models` | JSON | Catálogo de modelos instalados: `model_id`, `aliases`, `title`. |
| `invest://model/{model_id}/cheatsheet` | Markdown | Briefing por modelo: propósito, entradas agrupadas (con unidades) y salidas clave. Es el `human_briefing` del spec. |
| `invest://conventions` | Markdown | El layout de "proyecto InVEST" + los campos de `project.json`. |
| `invest://data-sources` | Markdown | Catálogo curado de fuentes abiertas y globales (DEM, land cover, clima, ETo, suelo, hidrografía, erosividad R / erodibilidad K, límites administrativos) con URLs y notas; marca cuáles ya están cableadas en un `fetch_*`. |
| `invest://model-guide` | Markdown | Qué modelo InVEST responde a qué pregunta del mundo real, con entradas/salidas cabecera de cada uno y sus emparejamientos habituales, por dominio (agua terrestre, carbono y hábitat, urbano, costero/marino, agricultura, recreación, herramientas de terreno) + **qué queda fuera del alcance de InVEST**. Mapa de partida para `recommend_model`. |
| `invest://coefficients` | JSON | Índice de la **base de coeficientes citados**: por parámetro (`resource`, `aka`, `models`, `invest_column`, `units`, `definition`, `record_count`, `source_keys`), `profiles`, cómo elegir, bibliografía. |
| `invest://coefficients/{name}` | JSON | Un fichero de la base. `{name}` ∈ `usle_c`, `usle_p`, `ndr_nutrient`, `curve_number`, `kc`, `root_depth`, `carbon_pools` (parámetros); `sources` (bibliografía verificada); `readme` (cómo elegir un valor en 5 pasos); `moorabool_fs28` (profile trabajado — **ejemplo, no defaults**). Registros indexados por atributos semánticos de cobertura (forma, densidad de dosel, condición, manejo, bioma, región, escala), **no** por una leyenda concreta; el `crosswalk` es solo orientativo. Cada valor lleva `source_key` + `confidence` + `verified`. |

---

## 5. Prompts / playbooks

Recetas que el asistente **sigue y adapta** (no las ejecuta el servidor). Todas
aceptan argumentos opcionales para particularizar.

| Prompt | Args | Para qué |
|---|---|---|
| `prepare_and_run_model` | `model_id="carbon"`, `project_root=""` | De cero a resultados para **un** modelo: orientar → `scaffold_project` → `project_readiness` → rellenar huecos (`fetch_*`, `reproject_layer`/`clip_to_aoi`/`align_raster_stack`) → tabla biofísica → `validate_invest_args` → `run_invest_model` → `summarize_results` → anotar en `logs/`. |
| `compare_land_use_scenarios` | `model_id="sdr"`, `project_root=""` | Baseline vs escenario alternativo: correr el mismo modelo con dos LULC (mismo grid, misma tabla) y medir la diferencia con `compare_scenarios`. Fijar todo lo no-LULC. |
| `fill_biophysical_table` | `model_id="sdr"`, `project_root=""` | Del esqueleto de `tables_from_template` a una tabla con valores **citados**: casar cada clase con un registro de `invest://coefficients` **por semántica** (no por código), elegir el valor por cercanía de bioma/región/escala, anotar la procedencia en `logs/`, y `check_table_vs_raster` hasta `ok` o cada warning justificado. |
| `recommend_model` | `question=""`, `project_root=""` | De la pregunta del usuario a modelo(s) InVEST + los datos que necesita cada uno: afinar la pregunta → `invest://model-guide` + `list_invest_models` → shortlist confirmada contra los cheat-sheets → `project_readiness` → recomendar (primario + complementos, entradas disponible/preparar/falta, salidas, si hace falta baseline-vs-escenario) → hand-off. Dice claramente cuándo la pregunta está **fuera** de InVEST. |

---

## 6. Recetas rápidas (cookbook)

**A. Correr un modelo con datos que ya tengo en disco**
```
allow_input_dir("D:/mis_datos")            # si están fuera del sandbox
describe_invest_model("carbon")            # ver args_schema
validate_invest_args("carbon", args)       # arreglar lo que marque
run_invest_model("carbon", args, wait_seconds=60)
get_invest_job(job_id)                      # o list_invest_job_artifacts
summarize_results(job_id, aoi_path="D:/mis_datos/aoi.gpkg")
```

**B. Preparar un caso de estudio desde cero (sin datos)**
```
scaffold_project("D:/casos/x", target_crs="EPSG:32618", aoi_path=".../aoi.gpkg")
fetch_dem(".../data/raw/dem.tif", aoi_path=".../aoi.gpkg", target_crs="EPSG:32618")
fetch_landcover(".../data/raw/lulc.tif", aoi_path=".../aoi.gpkg", target_crs="EPSG:32618")
fetch_soil(".../data/raw/hsg.tif", variable="hydrologic_soil_group", aoi_path=".../aoi.gpkg", target_crs="EPSG:32618")
fetch_climate(".../data/raw/precip_{month}.tif", variable="precipitation", aoi_path=".../aoi.gpkg", period="monthly")
align_raster_stack([...], reference_path=".../data/raw/dem.tif")   # a data/processed/
delineate_watersheds(".../processed/dem.tif", ".../raw/outlets.gpkg", ".../processed/watersheds.gpkg")
project_readiness("D:/casos/x", models=["seasonal_water_yield"])
tables_from_template("seasonal_water_yield", lulc_path=".../processed/lulc.tif", dst_path=".../tables/bio.csv")
#   ... rellenar bio.csv con invest://coefficients ...
check_table_vs_raster("seasonal_water_yield", ".../tables/bio.csv", lulc_path=".../processed/lulc.tif")
```

**C. Trade-off de cambio de uso (baseline vs escenario)**
```
run_invest_model("sdr", args_baseline, wait_seconds=120)     # -> base_id
clone_job(base_id, overrides={"lulc_path": ".../processed/lulc_scenario.tif"}, wait_seconds=120)  # -> scen_id
compare_scenarios(base_id, scen_id, aoi_path=".../aoi.gpkg")
aggregate_to_units(rasters=".../jobs/<scen>/compare_vs_<base>/diff_sed_export.tif",
                   units_path=".../municipios.gpkg", dst_path=".../out/reparto.gpkg",
                   value_per_unit=<USD/t>)
build_report([base_id, scen_id], dst_path=".../logs/informe.md")
```

**D. Round-trip con el InVEST Workbench**
```
import_datastack("C:/Users/yo/Desktop/mi_stack.invest.json")   # -> model_id, args
validate_invest_args(model_id, args)
run_invest_model(model_id, args)
export_datastack(".../datastacks/salida.invest.json", job_id=job_id)   # de vuelta al Workbench
```

---

## 7. Errores frecuentes

| Mensaje / síntoma | Causa | Qué hacer |
|---|---|---|
| `... path rejected` / `sandbox_issues` no vacío | Ruta fuera de las carpetas de confianza. | `allow_input_dir(carpeta_padre)` o mover el dato bajo el `root` del proyecto. |
| `{"ok": false, "env_missing": true}` | Falta el env `invest-geo` (o `invest-cal`). | `invest-mcp setup --geo` (o `--cal`); comprueba con `invest_env()`. |
| `missing_required: [...]` | Faltan args obligatorios. | Míralos en `describe_invest_model(...).args_schema.required`. |
| `invest_warnings` con "spatial overlap" / "projection" | Las capas no solapan o tienen CRS incompatibles. | `preflight_geo` para el detalle; `reproject_layer` + `clip_to_aoi` + `align_raster_stack`. |
| `crs_not_projected` / `crs_units_not_meters` en el preflight | Capa en grados (EPSG:4326). | Reproyecta a un CRS proyectado en metros (UTM). |
| `clone would be identical to the source` | No pasaste `overrides`/`drop_args` que cambien algo. | Pasa un override real. |
| `jobs are different models` (en `compare_scenarios`) | Los dos jobs no son del mismo modelo. | Compara solo runs del mismo `model_id`. |
| `monthly period needs '{month}' in dst_path` | `fetch_climate period="monthly"` sin el token. | Pon `{month}` en `dst_path` (`precip_{month}.tif`). |
| `variable='texture' needs '{fraction}' in dst_path` | `fetch_soil variable="texture"` sin el token. | Pon `{fraction}` (`soil_{fraction}.tif`). |
| Un `fetch_*` cuelga o tarda >3 min | Lentitud puntual de la fuente (S3 / ISRIC). | Reintentar. HydroSHEDS suele ir rápido; SoilGrids es lento a propósito. |
| `dst_path must end in .md` / `.json` | Extensión equivocada en `build_report` / `export_datastack`. | Corrige la extensión. |
| El job queda `failed` enseguida | Error de InVEST (columnas, rutas, datos). | `get_invest_job_logs(job_id)` — la cola del log de InVEST dice qué. |

---

## 8. Glosario

- **AOI** — *Area of Interest*. El vector de polígono(s) del caso de estudio.
- **CRS** — Sistema de referencia de coordenadas. "Proyectado" = en metros
  (UTM); "geográfico" = en grados (EPSG:4326).
- **Datastack / parameter set** — Fichero `.invest.json` del Workbench con
  `{model_id, args, invest_version}`.
- **DEM** — Modelo digital de elevación.
- **HSG** — *Hydrologic Soil Group* (A–D / 1–4), para SWY / Urban Flood /
  Stormwater.
- **Job** — Una ejecución (de modelo o calibración) con su `job_id` y carpeta.
- **LULC** — *Land Use / Land Cover*, el ráster de cobertura (categórico).
- **lucode** — El código entero de cada clase del LULC; la clave de la tabla
  biofísica.
- **Preflight** — Chequeo geoespacial previo al run (CRS, solapamiento, píxel).
- **Provenance** — `provenance.json`: versiones + sha256 de cada entrada.
- **Sandbox / allow-list** — Las carpetas donde el servidor puede leer/escribir.
- **Tabla biofísica / lookup** — El CSV que traduce cada `lucode` a coeficientes
  del modelo (`usle_c`, `Kc`, `cn_a`, `c_above`…).
- **Zonal** — Estadística de un ráster agregada dentro de cada polígono.
- **`invest-geo` / `invest-cal`** — Los envs conda sidecar (geoprocesado /
  calibración) que el servidor invoca por subproceso.

---

*Generado a mano a partir de `src/invest_mcp/{tools,resources,prompts}.py`.
Si añades o cambias una tool, actualiza también esta ficha y la tabla de
`CLAUDE.md` §4.*
