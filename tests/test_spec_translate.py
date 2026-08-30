from invest_mcp.models import spec_translate as st

CARBON_SPEC = {
    "model_id": "carbon",
    "model_title": "Carbon Storage and Sequestration",
    "userguide": "carbonstorage.html",
    "input_field_order": [["workspace_dir", "results_suffix"], ["lulc_bas_path", "carbon_pools_path"]],
    "args": {
        "workspace_dir": {"type": "workspace", "required": True, "about": "out"},
        "n_workers": {"type": "number", "required": False, "hidden": True, "about": "x"},
        "results_suffix": {"type": "freestyle_string", "required": False, "about": "suffix"},
        "lulc_bas_path": {
            "type": "raster", "required": True, "projected": True,
            "projection_units": "m", "about": "baseline LULC",
        },
        "carbon_pools_path": {"type": "csv", "required": True, "about": "pools table"},
        "calc_sequestration": {"type": "boolean", "required": "do_valuation", "about": "seq"},
        "price_per_metric_ton_of_c": {
            "type": "number", "required": "do_valuation",
            "units": "currency units/t", "about": "price",
        },
    },
    "outputs": {"c_storage_bas": {"path": "c_storage_bas.tif", "about": "carbon per pixel"}},
}


def test_schema_required_only_true_flags():
    schema = st.spec_to_args_schema(CARBON_SPEC)
    assert set(schema["required"]) == {"lulc_bas_path", "carbon_pools_path"}
    # server-managed and hidden args are dropped
    assert "workspace_dir" not in schema["properties"]
    assert "n_workers" not in schema["properties"]


def test_type_mapping():
    schema = st.spec_to_args_schema(CARBON_SPEC)["properties"]
    assert schema["calc_sequestration"]["type"] == "boolean"
    assert schema["price_per_metric_ton_of_c"]["type"] == "number"
    assert schema["lulc_bas_path"]["type"] == "string"
    assert "projected coordinate system" in schema["lulc_bas_path"]["description"]


def test_conditional_required_documented_not_enforced():
    schema = st.spec_to_args_schema(CARBON_SPEC)
    assert "calc_sequestration" not in schema.get("required", [])
    assert "do_valuation" in schema["properties"]["calc_sequestration"]["description"]


def test_path_args():
    assert st.path_args(CARBON_SPEC) == {"lulc_bas_path": "raster", "carbon_pools_path": "csv"}


def test_briefing_mentions_model_and_outputs():
    text = st.human_briefing(CARBON_SPEC)
    assert "Carbon Storage and Sequestration" in text
    assert "c_storage_bas.tif" in text
