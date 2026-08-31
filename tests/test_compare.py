"""Scenario-comparison planning -- pure logic, no GDAL needed.

The GDAL-backed differencing runs in invest_mcp.geo.compare inside the
invest-geo env and is covered by the end-to-end run, not here.
"""

from invest_mcp.geo.client import (
    _strip_results_suffix,
    output_meta_map,
    plan_comparison,
)

CARBON_SPEC = {
    "model_id": "carbon",
    "model_title": "Carbon Storage and Sequestration",
    "outputs": {
        "c_storage_bas": {"path": "c_storage_bas.tif", "about": "Baseline storage",
                          "units": "t/ha"},
        "c_storage_alt": {"path": "c_storage_alt.tif", "about": "Alt storage",
                          "units": "t/ha"},
        "c_above_bas": {"path": "intermediate_outputs/c_above_bas.tif",
                        "about": "Aboveground", "units": "t/ha"},
    },
}


def _mk_ws(root, names):
    root.mkdir(parents=True, exist_ok=True)
    for rel in names:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return root


def test_plan_comparison_pairs_common_rasters(tmp_path):
    base = _mk_ws(tmp_path / "base", ["c_storage_bas.tif", "c_storage_alt.tif"])
    scen = _mk_ws(tmp_path / "scen", ["c_storage_bas.tif", "c_storage_alt.tif"])
    plan = plan_comparison(base, scen, output_meta_map(CARBON_SPEC))

    assert [p["relpath"] for p in plan["pairs"]] == ["c_storage_bas.tif",
                                                     "c_storage_alt.tif"]
    first = plan["pairs"][0]
    assert first["units"] == "t/ha"
    assert first["baseline_path"].replace("\\", "/").endswith("base/c_storage_bas.tif")
    assert first["scenario_path"].replace("\\", "/").endswith("scen/c_storage_bas.tif")
    assert plan["only_in_baseline"] == []
    assert plan["only_in_scenario"] == []


def test_plan_comparison_reports_unmatched_both_ways(tmp_path):
    base = _mk_ws(tmp_path / "base", ["c_storage_bas.tif", "c_storage_alt.tif"])
    scen = _mk_ws(tmp_path / "scen", ["c_storage_bas.tif", "extra_out.tif"])
    plan = plan_comparison(base, scen, output_meta_map(CARBON_SPEC))

    assert [p["relpath"] for p in plan["pairs"]] == ["c_storage_bas.tif"]
    assert plan["only_in_baseline"] == ["c_storage_alt.tif"]
    assert plan["only_in_scenario"] == ["extra_out.tif"]


def test_plan_comparison_excludes_intermediate_by_default(tmp_path):
    names = ["c_storage_bas.tif", "intermediate_outputs/c_above_bas.tif"]
    base = _mk_ws(tmp_path / "base", names)
    scen = _mk_ws(tmp_path / "scen", names)

    default = plan_comparison(base, scen, output_meta_map(CARBON_SPEC))
    assert [p["relpath"] for p in default["pairs"]] == ["c_storage_bas.tif"]

    with_inter = plan_comparison(base, scen, output_meta_map(CARBON_SPEC),
                                 include_intermediate=True)
    assert "intermediate_outputs/c_above_bas.tif" in [
        p["relpath"] for p in with_inter["pairs"]]


def test_plan_comparison_explicit_selection_by_bare_name(tmp_path):
    names = ["c_storage_bas.tif", "c_storage_alt.tif"]
    base = _mk_ws(tmp_path / "base", names)
    scen = _mk_ws(tmp_path / "scen", names)
    plan = plan_comparison(base, scen, output_meta_map(CARBON_SPEC),
                           explicit=["c_storage_alt.tif"])
    assert [p["relpath"] for p in plan["pairs"]] == ["c_storage_alt.tif"]


def test_plan_comparison_no_overlap_gives_empty_pairs(tmp_path):
    base = _mk_ws(tmp_path / "base", ["c_storage_bas.tif"])
    scen = _mk_ws(tmp_path / "scen", ["c_storage_alt.tif"])
    plan = plan_comparison(base, scen, output_meta_map(CARBON_SPEC))
    assert plan["pairs"] == []
    assert plan["only_in_baseline"] == ["c_storage_bas.tif"]
    assert plan["only_in_scenario"] == ["c_storage_alt.tif"]


def test_strip_results_suffix_only_strips_trailing_stem():
    assert _strip_results_suffix("a/b/wyield_run1.tif", "run1") == "a/b/wyield.tif"
    assert _strip_results_suffix("wyield.tif", "run1") == "wyield.tif"
    # a leading (non-trailing) match must be left alone
    assert _strip_results_suffix("run1_wyield.tif", "run1") == "run1_wyield.tif"
    # empty suffix is a no-op
    assert _strip_results_suffix("wyield_run1.tif", "") == "wyield_run1.tif"
    # stem that is *only* the suffix is left alone (len guard)
    assert _strip_results_suffix("_run1.tif", "run1") == "_run1.tif"


def test_plan_comparison_matches_across_different_results_suffix(tmp_path):
    base = _mk_ws(tmp_path / "base",
                  ["c_storage_bas_baseline.tif", "c_storage_alt_baseline.tif"])
    scen = _mk_ws(tmp_path / "scen",
                  ["c_storage_bas_deforest.tif", "c_storage_alt_deforest.tif"])
    plan = plan_comparison(base, scen, output_meta_map(CARBON_SPEC),
                           baseline_suffix="baseline", scenario_suffix="deforest")

    assert [p["relpath"] for p in plan["pairs"]] == ["c_storage_bas.tif",
                                                     "c_storage_alt.tif"]
    assert plan["only_in_baseline"] == []
    assert plan["only_in_scenario"] == []
    first = plan["pairs"][0]
    assert first["baseline_relpath"] == "c_storage_bas_baseline.tif"
    assert first["scenario_relpath"] == "c_storage_bas_deforest.tif"
    assert first["baseline_path"].replace("\\", "/").endswith(
        "base/c_storage_bas_baseline.tif")
    assert first["scenario_path"].replace("\\", "/").endswith(
        "scen/c_storage_bas_deforest.tif")
    # spec metadata is found via the suffix-free key
    assert first["units"] == "t/ha"


def test_plan_comparison_suffix_mismatch_still_flags_unmatched(tmp_path):
    base = _mk_ws(tmp_path / "base",
                  ["c_storage_bas_a.tif", "c_storage_alt_a.tif"])
    scen = _mk_ws(tmp_path / "scen", ["c_storage_bas_b.tif"])
    plan = plan_comparison(base, scen, output_meta_map(CARBON_SPEC),
                           baseline_suffix="a", scenario_suffix="b")

    assert [p["relpath"] for p in plan["pairs"]] == ["c_storage_bas.tif"]
    assert plan["only_in_baseline"] == ["c_storage_alt_a.tif"]
    assert plan["only_in_scenario"] == []
