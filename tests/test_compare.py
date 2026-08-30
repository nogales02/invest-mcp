"""Scenario-comparison planning -- pure logic, no GDAL needed.

The GDAL-backed differencing runs in invest_mcp.geo.compare inside the
invest-geo env and is covered by the end-to-end run, not here.
"""

from invest_mcp.geo.client import output_meta_map, plan_comparison

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
