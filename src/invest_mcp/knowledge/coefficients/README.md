# Cited coefficient knowledge base

Starting-point values for the **biophysical / lookup tables** of the InVEST
models this project cares about — **AWY, SWY, SDR, NDR, Carbon** — every one
carrying its **source** and the **context that source was measured in**.

This is a *reference*, not an oracle. It gives the model elements to reason with;
it never picks a value for you. **Every number here is a starting point to
verify** against local data, local literature or expert judgement.

---

## Why it is not keyed to one land-cover legend

Your land-cover raster can use any legend — ESA WorldCover, a national map, a
custom reclassification. So a record is **not** filed under "WorldCover class
10". It is described by *what the cover actually is*:

| attribute | example values |
|---|---|
| `form` | forest, shrubland, grassland, cropland, wetland, bare, built, water |
| `canopy_density` | closed (>70%), open (40–70%), sparse (<40%) |
| `condition` | undisturbed, degraded, recovering, managed |
| `management` | none, grazed, conventional tillage, no-till + cover crop, irrigated |
| `biome` / `climate_zone` | temperate, tropical, semi-arid, boreal, Mediterranean … |
| `region` | global, or a named country / basin |
| `spatial_scale` | the grid or plot scale the value was derived at |

To use the KB: describe each class of *your* raster in those terms, then find the
record(s) whose attributes are closest. `crosswalk` hints (a WorldCover code, the
original study's class label) are **advisory only** — for orientation, not
matching.

## How to choose a value

1. **Match on the cover itself** — form + canopy density + condition +
   management. A "grassland" that is heavily grazed and one that is ungrazed
   native pasture are different records.
2. **Prefer the closest context.** Regional beats global. Same biome / climate
   zone beats a different one. A plot-scale field measurement and a 1 km
   grid class-mean answer different questions — note which you are using.
3. **Prefer the right model variant.** USLE vs RUSLE C-factors, InVEST NDR
   `eff` conventions, curve-number antecedent-moisture condition (ARC II is the
   InVEST default).
4. **Carry the citation.** Whatever value you write into a table, record its
   `source_key` (resolve it in `sources.json`) next to it so the run is
   reproducible. If you interpolate or adapt, say so.
5. **Flag low confidence.** Records have a `confidence` field and `caveats`.
   Some `source_key`s are marked `disambiguation_needed` — do not use those
   silently.

## Files

| resource | InVEST column(s) | models |
|---|---|---|
| `invest://coefficients/usle_c` | `usle_c` | SDR |
| `invest://coefficients/usle_p` | `usle_p` | SDR |
| `invest://coefficients/ndr_nutrient` | `load_n`, `load_p`, `eff_n`, `eff_p`, `crit_len_n`, `crit_len_p`, `proportion_subsurface_n` | NDR |
| `invest://coefficients/curve_number` | `CN_A`, `CN_B`, `CN_C`, `CN_D` | SWY |
| `invest://coefficients/kc` | `Kc` (AWY), `Kc_1`…`Kc_12` (SWY) | AWY, SWY |
| `invest://coefficients/root_depth` | `root_depth` | AWY |
| `invest://coefficients/carbon_pools` | `c_above`, `c_below`, `c_soil`, `c_dead` | Carbon |
| `invest://coefficients/sources` | — bibliography (`source_key` → full citation + context + how it was verified) |
| `invest://coefficients/moorabool_fs28` | a fully worked local table (Moorabool River basin, Victoria, Australia — FS28), kept as a **documented example** |

## Record shape

```jsonc
{
  "id": "usle_c/forest/closed-canopy/global/yang2003",
  "cover": {
    "form": "forest",
    "label_source": "Dense forest",          // the study's own class name
    "canopy_density": "closed (>70%)",
    "condition": "undisturbed",
    "management": "none",
    "crosswalk": { "esa_worldcover": [10], "note": "advisory only" }
  },
  "value": 0.001,
  "range": [0.0001, 0.003],                    // null if the source gives a point value
  "context": {
    "biome": "not differentiated (global map)",
    "region": "global",
    "climate_zone": null,
    "spatial_scale": "0.5-degree global grid",
    "erosion_model": "USLE",
    "derivation": "assigned per land-cover class",
    "study_area": "global potential soil erosion assessment"
  },
  "source_key": "yang2003",                    // -> sources.json
  "confidence": "medium",                      // high | medium | low
  "caveats": "Global class-mean; not climate- or region-specific.",
  "provenance": "FS28 Moorabool masterfile, sheet 'SDR Params'"
}
```

## Provenance of v1

Most records are lifted from the **FS28 Moorabool masterfile**
(`FS28-Moorabool_MasterFile.xlsx`), a worked InVEST parameterisation for the
Moorabool River basin (Victoria, Australia). That file compiled the published
source tables reproduced here; each `provenance` field names the sheet and table
it came from. Citations were then checked against the primary literature (journal,
year, DOI, and the study's biome / region / scale) — see each entry's `verified`
note in `sources.json`. `root_depth` and `carbon_pools` are **not** in the
masterfile; they were added from standard references (Canadell et al. 1996;
Schenk & Jackson 2002; FAO-56; IPCC 2006 Vol. 4 / 2019 Refinement).
