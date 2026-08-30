"""Output-summary planning -- pure logic, no GDAL needed.

The GDAL-backed statistics run in invest_mcp.geo.summarize inside the invest-geo
env and are covered by the end-to-end run, not here.
"""

from invest_mcp.geo.client import output_meta_map, plan_rasters

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
        "summary_csv": {"path": "raster_values_summary.csv", "about": "totals"},
    },
}


def _make_ws(tmp_path):
    (tmp_path / "intermediate_outputs").mkdir()
    (tmp_path / "taskgraph_cache").mkdir()
    for rel in ("c_storage_bas.tif", "c_storage_alt.tif",
                "intermediate_outputs/c_above_bas.tif",
                "taskgraph_cache/taskgraph.db", "raster_values_summary.csv"):
        (tmp_path / rel).write_bytes(b"x")
    return tmp_path


def test_output_meta_map_keys_on_relative_path():
    meta = output_meta_map(CARBON_SPEC)
    assert meta["c_storage_bas.tif"]["units"] == "t/ha"
    assert meta["intermediate_outputs/c_above_bas.tif"]["id"] == "c_above_bas"


def test_plan_rasters_skips_intermediate_and_taskgraph_by_default(tmp_path):
    ws = _make_ws(tmp_path)
    planned = plan_rasters(ws, output_meta_map(CARBON_SPEC))
    rels = [r["relpath"] for r in planned]
    assert rels == ["c_storage_bas.tif", "c_storage_alt.tif"]
    assert planned[0]["label"] == "c_storage_bas"
    assert planned[0]["units"] == "t/ha"


def test_plan_rasters_include_intermediate(tmp_path):
    ws = _make_ws(tmp_path)
    planned = plan_rasters(ws, output_meta_map(CARBON_SPEC), include_intermediate=True)
    rels = [r["relpath"] for r in planned]
    assert "intermediate_outputs/c_above_bas.tif" in rels
    assert "taskgraph_cache/taskgraph.db" not in rels  # not a raster suffix anyway


def test_plan_rasters_explicit_selection_by_bare_name(tmp_path):
    ws = _make_ws(tmp_path)
    planned = plan_rasters(ws, output_meta_map(CARBON_SPEC),
                           explicit=["c_storage_alt.tif"])
    assert [r["relpath"] for r in planned] == ["c_storage_alt.tif"]


def test_plan_rasters_orders_by_spec_then_extras(tmp_path):
    ws = _make_ws(tmp_path)
    (ws / "zzz_extra.tif").write_bytes(b"x")
    planned = plan_rasters(ws, output_meta_map(CARBON_SPEC), include_intermediate=True)
    rels = [r["relpath"] for r in planned]
    # spec-ordered names come before the unknown extra
    assert rels.index("c_storage_bas.tif") < rels.index("zzz_extra.tif")
    assert rels[-1] == "zzz_extra.tif"
