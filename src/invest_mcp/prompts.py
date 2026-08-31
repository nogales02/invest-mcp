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
