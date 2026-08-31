"""MCP prompts -- guided playbooks the client can invoke and adapt.

A prompt returns a recipe for the model to follow step by step. It is not a
hardcoded pipeline: the model stays free to skip, reorder or deviate as the
situation demands. The server never executes these -- it only hands them over.
"""

from __future__ import annotations


def prepare_and_run_model(model_id: str = "carbon", project_root: str = "") -> str:
    root = f"`{project_root}`" if project_root else "a project folder you pick with the user"
    return f"""\
# Playbook — prepare and run the InVEST **{model_id}** model, end to end

Work in {root}. Confirm each step with the user before spending compute; stop and
ask whenever a choice is judgement, not fact.

1. **Orient.** `invest_env()` to confirm InVEST is reachable. Read the resource
   `invest://model/{model_id}/cheatsheet` for this model's required inputs, units
   and outputs.
2. **Scaffold.** `scaffold_project(root, name=..., target_crs=..., aoi_path=...)`.
   Pick `target_crs` as a projected CRS in metres covering the AOI (ask the user
   if unsure). Read `invest://conventions` for the folder meaning.
3. **Inventory what's already there.** `project_readiness(root, models=["{model_id}"])`.
   It lists files found, what it matched to which arg, and what's missing.
4. **Fill the gaps.** For each missing spatial input:
   - If the user has the file, note its path. Otherwise consult
     `invest://data-sources` and have the user download into `data/raw/`.
   - Bring every raster onto the project grid: `reproject_layer` (use
     `resampling="nearest"` for categorical layers like land cover, `bilinear`/
     `average` for continuous), then `clip_to_aoi`, then `align_raster_stack`
     with one layer as `reference_path` so the whole stack coregisters. Write
     results into `data/processed/`.
5. **Biophysical / lookup table.** Build the CSV the model needs in `tables/`,
   one row per land-cover class present in the LULC raster. Use published
   coefficients for the region and record the source in `logs/`. Ask the user to
   review it.
6. **Validate.** `validate_invest_args("{model_id}", args)` — it runs the schema
   check, the sandbox check, `invest validate` and the geospatial preflight. Fix
   everything it flags. Do not pass `workspace_dir`.
7. **Run.** `run_invest_model("{model_id}", args)`; poll `get_invest_job(job_id)`
   until terminal. On failure, read `get_invest_job_logs(job_id)`.
8. **Interpret.** `summarize_results(job_id, aoi_path=...)` for per-raster stats,
   zonal stats over the AOI and a preview. Relate the numbers back to the user's
   question.
9. **Record.** Note the job id, inputs and headline results in `logs/`. The run's
   `provenance.json` already has input hashes and tool versions.
"""


def compare_land_use_scenarios(model_id: str = "sdr", project_root: str = "") -> str:
    root = f"`{project_root}`" if project_root else "a project folder you pick with the user"
    return f"""\
# Playbook — quantify a land-use trade-off with **{model_id}**

The point of InVEST: run the *same* model under two land-cover maps (e.g. current
vs. reforestation) and measure the difference. Work in {root}.

1. **Baseline first.** Follow the `prepare_and_run_model` playbook for
   **{model_id}** with the current land-cover map. Keep every non-LULC input
   (DEM, climate, tables, watersheds) fixed — only the LULC raster will change.
   Note the baseline `job_id`.
2. **Build the alternative LULC.** With the user, define the scenario (which
   areas change to which class). Produce `data/processed/lulc_<scenario>.tif` on
   the *same grid* as the baseline LULC (`align_raster_stack` with the baseline
   LULC as `reference_path`). If a class changes, make sure the biophysical
   table already has a row for it.
3. **Run the scenario.** `run_invest_model("{model_id}", args)` with identical
   args except `lulc*` pointing at the scenario raster. Poll to completion. Note
   the scenario `job_id`.
4. **Compare.** `compare_scenarios(baseline_job_id, scenario_job_id, aoi_path=...)`.
   It writes `scenario − baseline` difference rasters, totals before/after, %
   change, pixels up/down, per-feature deltas over the AOI and a diverging-colour
   preview.
5. **Report.** State the change per output that matters (e.g. sediment export
   −X t/yr, −Y%), where on the map it concentrates (the zonal deltas), and the
   caveats (input vintage, table coefficients, resolution). Save to `logs/`.
6. **Optional:** repeat step 2–4 for more scenarios and tabulate them side by
   side.
"""


def fill_biophysical_table(model_id: str = "sdr", project_root: str = "") -> str:
    root = f"`{project_root}`" if project_root else "a project folder you pick with the user"
    return f"""\
# Playbook — fill the **{model_id}** biophysical / lookup table from cited coefficients

Turn the blank skeleton into a table with real, *sourced* numbers. Work in
{root}. The coefficients are a judgement call — surface every source and every
uncertainty to the user; never invent a value.

1. **Know the columns.** Read `invest://model/{model_id}/cheatsheet` for what the
   table feeds. `describe_invest_model("{model_id}")` if you need the exact arg
   name and the conditional columns (e.g. NDR's `load_n` only when `calc_n`).
2. **Skeleton.** `tables_from_template("{model_id}", lulc_path=..., dst_path="tables/…csv",
   legend_path=...)`. Keep its `column_help` (about / units / requirement) and
   `classes` (each land-cover code + pixel count) in view.
3. **Open the knowledge base.** Read `invest://coefficients` for the catalog,
   then `invest://coefficients/<parameter>` for each column you must fill
   (`usle_c`, `usle_p`, `ndr_nutrient`, `curve_number`, `kc`, `root_depth`,
   `carbon_pools`). Read `invest://coefficients/readme` once for how records are
   organised.
4. **Match each land-cover class to a record — by meaning, not by code.** For
   every class, look at its real-world cover (with the user / the legend) and
   match it against each record's `cover` attributes: vegetation `form`,
   `canopy_density`, `condition`, `management`, and the record's `context`
   (`biome`, `region`, `spatial_scale`). The `crosswalk` field is advisory
   only — do not key off it.
5. **Pick the value.** Prefer a source whose biome / region / scale is closest
   to the study site; note the `confidence` and `caveats`. When records
   disagree, tell the user the spread and why, and pick with them. For monthly
   Kc (SWY) use `invest://coefficients/moorabool_fs28` → `kc_monthly` only as a
   *shape* reference (temperate seasonal); re-derive for the site's climate.
6. **Write the cells** into the CSV. Leave a conditional column blank only if
   that calculation is off. Keep `lucode` and any `description` column intact.
7. **Record provenance.** In `logs/`, one line per class per parameter:
   value, `source_key`, and the full citation from
   `invest://coefficients/sources`. This is what makes the run defensible.
8. **Check.** `check_table_vs_raster("{model_id}", table_path=..., lulc_path=...)`.
   Fix every `error` (missing rows/columns, empty required cells, invariant
   violations). For each `warning` under `ranges.out_of_typical`, either justify
   the value in `logs/` (cite why the site is outside the usual band) or correct
   it. Re-run until `severity` is `ok` or every warning is explained.
9. **Hand off.** The table is now ready for `validate_invest_args("{model_id}", args)`
   → `run_invest_model`. Ask the user to eyeball the final CSV first.
"""


def recommend_model(question: str = "", project_root: str = "") -> str:
    q = f"> {question.strip()}\n\n" if question.strip() else ""
    root = f"`{project_root}`" if project_root else "(no project folder given yet)"
    return f"""\
# Playbook — recommend the InVEST model(s) for a question

{q}Match a real-world question to one or more InVEST models and lay out what each
needs. The recommendation is your reasoning to make — these are the steps and the
reference material. Project folder: {root}.

1. **Sharpen the question with the user.** Nail down:
   - the ecosystem service / outcome (water quantity, seasonal flow, erosion &
     sediment, nutrients & water quality, carbon, habitat/biodiversity,
     pollination, urban flooding, urban heat, green-space access, coastal
     exposure, blue carbon, recreation, scenery, energy…);
   - the decision it informs (baseline accounting, land-use change, restoration
     siting, BMP targeting, infrastructure/permitting, a scenario trade-off);
   - geography and scale (which watershed / city / coastline), and time frame;
   - whether it is a single assessment or a **baseline vs scenario** comparison.

2. **Read the reference.** `invest://model-guide` maps questions → models with
   each model's headline inputs, outputs and common pairings. Get the installed
   list with `list_invest_models()` (or `invest://models`) — only recommend
   models that appear there, and note the exact `model_id`.

3. **Shortlist and confirm.** For each candidate read
   `invest://model/{{model_id}}/cheatsheet` (or `describe_invest_model(model_id)`).
   Check it actually answers the question; note its required inputs (with units),
   key outputs, and its main assumptions / limitations. Drop candidates that
   don't fit and say why.

4. **Check what data exists.** If a project folder is given, run
   `project_readiness(project_root, models=[shortlist])` to see, per model, which
   required inputs are already present, which need preparation, and which are
   missing. Otherwise list the inputs from the cheat-sheet.

5. **Recommend.** Give a primary model and any complements (e.g. `sdr` + `ndr`
   for an erosion + water-quality story on one watershed; `carbon` +
   `habitat_quality` for a land-use-change assessment; `seasonal_water_yield`
   alongside `annual_water_yield` for timing). For each recommended model state:
   - why it fits, and what it will *not* tell them;
   - the required inputs, tagged available / needs-prep / missing (step 4), each
     with a source from `invest://data-sources` where relevant;
   - the outputs that answer the question, with units;
   - whether they need a baseline-vs-scenario run.

6. **Hand off.** For the chosen model, follow `prepare_and_run_model` (single
   assessment) or `compare_land_use_scenarios` (trade-off); build its
   biophysical / lookup table with `fill_biophysical_table`.

If the question is outside InVEST's scope (real-time forecasting, hydrodynamic
inundation, species-distribution modelling, air-quality dispersion, detailed
groundwater), say so and stop — see the last section of `invest://model-guide`.
"""


def register(server) -> None:
    server.prompt(
        name="prepare_and_run_model",
        title="Prepare and run an InVEST model end to end",
        description="Scaffold a project, gather and coregister inputs, build the "
                    "biophysical table, validate, run, and summarise one model.",
    )(prepare_and_run_model)
    server.prompt(
        name="compare_land_use_scenarios",
        title="Quantify a land-use trade-off",
        description="Run one InVEST model under two land-cover maps and measure "
                    "the difference with compare_scenarios.",
    )(compare_land_use_scenarios)
    server.prompt(
        name="fill_biophysical_table",
        title="Fill a biophysical table from cited coefficients",
        description="From the tables_from_template skeleton to a sourced, "
                    "range-checked biophysical / lookup table using the "
                    "invest://coefficients knowledge base and check_table_vs_raster.",
    )(fill_biophysical_table)
    server.prompt(
        name="recommend_model",
        title="Recommend the InVEST model(s) for a question",
        description="From a real-world question to InVEST model(s) plus the data "
                    "each needs, using invest://model-guide, the per-model "
                    "cheat-sheets and project_readiness.",
    )(recommend_model)
