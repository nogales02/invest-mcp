"""`check_table_vs_raster` core: the pure table checker + the KB range lookup it
leans on. Fake specs, real knowledge base, no GDAL, no invest.exe."""

from invest_mcp import tools
from invest_mcp.knowledge import coefficients as kb
from invest_mcp.workspace import biotable

# non-key column specs, shaped like spec_translate.table_arg_specs(...)["columns"]
SDR_COLS = [
    {"id": "usle_c", "about": "cover-management", "required": True, "units": None},
    {"id": "usle_p", "about": "support practice", "required": True, "units": None},
]
SWY_COLS = [
    {"id": "cn_[SOIL_GROUP]", "about": "curve number", "required": True, "units": None},
    {"id": "kc_[MONTH]", "about": "crop coefficient", "required": True, "units": None},
]
NDR_COLS = [
    {"id": "load_n", "about": "N load", "required": "calc_n", "units": None},
    {"id": "eff_n", "about": "N retention", "required": "calc_n", "units": None},
    {"id": "note", "about": "free text", "required": False, "units": None},
]
NDR_COLS_FULL = NDR_COLS + [
    {"id": "load_type_n", "about": "load mode", "required": "calc_n", "units": None},
    {"id": "load_p", "about": "P load", "required": "calc_p", "units": None},
]


def _check(cols, csv_text, codes=None, **kw):
    return biotable.check_table(
        "lucode", cols, csv_text,
        raster_codes=codes,
        column_ranges=kb.column_ranges(),
        **kw,
    )


# ---------------------------------------------------------------------------
# knowledge base: column -> parameter + typical band
# ---------------------------------------------------------------------------
def test_parameter_for_column_maps_the_known_columns():
    assert kb.parameter_for_column("usle_c") == "usle_c"
    assert kb.parameter_for_column("CN_A") == "curve_number"
    assert kb.parameter_for_column("kc") == "kc"
    assert kb.parameter_for_column("kc_6") == "kc"
    assert kb.parameter_for_column("c_above") == "carbon_pools"
    assert kb.parameter_for_column("proportion_subsurface_n") == "ndr_nutrient"
    assert kb.parameter_for_column("lucode") is None
    assert kb.parameter_for_column("description") is None
    assert kb.parameter_for_column("kc_13") is None


def test_column_ranges_cover_every_kb_parameter_and_carry_a_resource():
    r = kb.column_ranges()
    for col in ("usle_c", "usle_p", "load_n", "eff_n", "cn_a", "cn_d",
               "root_depth", "c_above", "kc", "kc_12"):
        assert col in r, col
        lo, hi = r[col]["typical"]
        assert lo <= hi
        assert r[col]["resource"].startswith("invest://coefficients/")
    params = {v["parameter"] for v in r.values()}
    assert params == set(kb.PARAMETERS)


# ---------------------------------------------------------------------------
# a clean table
# ---------------------------------------------------------------------------
def test_clean_table_is_ok():
    csv_text = "lucode,usle_c,usle_p\n10,0.01,1\n20,0.15,1\n"
    out = _check(SDR_COLS, csv_text, codes=[10, 20])
    assert out["severity"] == "ok" and out["pass"] is True
    assert out["checks"]["coverage"]["missing_rows"] == []
    assert out["row_count"] == 2


# ---------------------------------------------------------------------------
# coverage vs the raster
# ---------------------------------------------------------------------------
def test_missing_row_for_a_raster_class_is_an_error():
    csv_text = "lucode,usle_c,usle_p\n10,0.01,1\n"
    out = _check(SDR_COLS, csv_text, codes=[10, 20, 30])
    assert out["severity"] == "error" and out["pass"] is False
    assert out["checks"]["coverage"]["missing_rows"] == [20, 30]


def test_orphan_and_duplicate_rows_are_warnings():
    csv_text = "lucode,usle_c,usle_p\n10,0.01,1\n20,0.1,1\n20,0.2,1\n99,0.3,1\n"
    out = _check(SDR_COLS, csv_text, codes=[10, 20])
    assert out["severity"] == "warning" and out["pass"] is True
    assert out["checks"]["coverage"]["orphan_rows"] == [99]
    assert out["checks"]["coverage"]["duplicate_rows"] == [20]


def test_no_raster_still_checks_structure_and_values():
    csv_text = "lucode,usle_c,usle_p\n10,0.01,1\n"
    out = _check(SDR_COLS, csv_text, codes=None)
    assert out["checks"]["coverage"] == {}
    assert out["severity"] == "ok"


# ---------------------------------------------------------------------------
# columns
# ---------------------------------------------------------------------------
def test_missing_required_column_is_an_error():
    csv_text = "lucode,usle_c\n10,0.01\n"
    out = _check(SDR_COLS, csv_text, codes=[10])
    assert out["severity"] == "error"
    assert out["checks"]["columns"]["missing"] == ["usle_p"]


def test_unexpected_column_is_a_warning_but_description_is_allowed():
    csv_text = "lucode,usle_c,usle_p,description,made_up\n10,0.01,1,forest,7\n"
    out = _check(SDR_COLS, csv_text, codes=[10])
    assert out["checks"]["columns"]["unexpected"] == ["made_up"]
    assert out["severity"] == "warning"


def test_conditional_columns_are_reported_not_required():
    csv_text = "lucode,load_n,eff_n\n10,5,0.3\n"
    out = _check(NDR_COLS, csv_text, codes=[10])
    assert set(out["conditional_columns"]) == {"load_n", "eff_n"}
    assert out["severity"] == "ok"          # nothing missing/blank/out of range
    assert out["enforced_conditions"] == []


# ---------------------------------------------------------------------------
# conditions resolved from the model args (H3: NDR load_type_* columns)
# ---------------------------------------------------------------------------
def test_active_condition_makes_a_missing_conditional_column_an_error():
    csv_text = "lucode,load_n,eff_n\n10,5,0.3\n"          # no load_type_n
    out = _check(NDR_COLS_FULL, csv_text, codes=[10], conditions={"calc_n": True})
    assert out["severity"] == "error" and out["pass"] is False
    assert "load_type_n" in out["checks"]["columns"]["missing"]
    assert out["enforced_conditions"] == ["calc_n"]


def test_active_condition_makes_a_blank_conditional_cell_an_error():
    csv_text = "lucode,load_n,eff_n,load_type_n\n10,5,0.3,\n"   # blank load_type_n
    out = _check(NDR_COLS_FULL, csv_text, codes=[10], conditions={"calc_n": True})
    assert out["severity"] == "error"
    assert {"row": "10", "column": "load_type_n"} in out["checks"]["cells"]["empty_required"]


def test_inactive_condition_drops_columns_from_required_and_unexpected():
    # calc_p off: load_p present but not required, and not flagged unexpected
    csv_text = "lucode,load_n,eff_n,load_type_n,load_p\n10,5,0.3,application,\n"
    out = _check(NDR_COLS_FULL, csv_text, codes=[10],
                 conditions={"calc_n": True, "calc_p": False})
    assert out["severity"] == "ok" and out["pass"] is True
    assert out["checks"]["columns"]["unexpected"] == []
    assert "load_p" not in out["conditional_columns"]


def test_unlisted_condition_stays_advisory():
    csv_text = "lucode,load_n,eff_n,load_type_n\n10,5,0.3,application\n"
    out = _check(NDR_COLS_FULL, csv_text, codes=[10], conditions={"calc_n": True})
    assert "load_p" in out["conditional_columns"]        # calc_p not mentioned
    assert out["severity"] == "ok"


def test_table_conditions_helper_reads_calc_flags_from_args():
    cols = [{"id": "load_n", "required": "calc_n"},
            {"id": "load_p", "required": "calc_p"},
            {"id": "usle_c", "required": True}]
    assert tools._table_conditions(cols, {"calc_n": True, "calc_p": False}) == {
        "calc_n": True, "calc_p": False}
    assert tools._table_conditions(cols, {"calc_n": True}) == {"calc_n": True}
    assert tools._table_conditions(cols, None) == {}
    assert tools._table_conditions(cols, {"something_else": 1}) == {}


# ---------------------------------------------------------------------------
# cells
# ---------------------------------------------------------------------------
def test_empty_required_cell_is_an_error():
    csv_text = "lucode,usle_c,usle_p\n10,,1\n"
    out = _check(SDR_COLS, csv_text, codes=[10])
    assert out["severity"] == "error"
    assert out["checks"]["cells"]["empty_required"] == [{"row": "10", "column": "usle_c"}]


def test_non_numeric_coefficient_is_an_error():
    csv_text = "lucode,usle_c,usle_p\n10,low,1\n"
    out = _check(SDR_COLS, csv_text, codes=[10])
    assert out["severity"] == "error"
    assert out["checks"]["cells"]["non_numeric"] == [
        {"row": "10", "column": "usle_c", "value": "low"}
    ]


# ---------------------------------------------------------------------------
# ranges + hard invariants
# ---------------------------------------------------------------------------
def test_curve_numbers_out_of_order_break_an_invariant():
    csv_text = "lucode,cn_a,cn_b,cn_c,cn_d\n10,80,70,90,95\n"
    out = _check(SWY_COLS, csv_text, codes=[10])
    assert out["severity"] == "error"
    viols = out["checks"]["ranges"]["invariant_violations"]
    assert any("ordered" in v["rule"] for v in viols)


def test_fraction_above_one_breaks_an_invariant():
    csv_text = "lucode,load_n,eff_n\n10,5,1.4\n"
    out = _check(NDR_COLS, csv_text, codes=[10])
    assert out["severity"] == "error"
    assert any(v["column"] == "eff_n" for v in out["checks"]["ranges"]["invariant_violations"])


def test_value_outside_the_cited_band_is_a_warning_with_a_source():
    # load_n typical band tops out well below 500; no hard upper bound on a load
    csv_text = "lucode,load_n,eff_n\n10,500,0.3\n"
    out = _check(NDR_COLS, csv_text, codes=[10])
    assert out["severity"] == "warning" and out["pass"] is True
    oot = out["checks"]["ranges"]["out_of_typical"]
    assert oot and oot[0]["column"] == "load_n"
    assert oot[0]["resource"] == "invest://coefficients/ndr_nutrient"


def test_negative_load_breaks_an_invariant():
    csv_text = "lucode,load_n,eff_n\n10,-3,0.3\n"
    out = _check(NDR_COLS, csv_text, codes=[10])
    assert out["severity"] == "error"
    assert any(v["column"] == "load_n" for v in out["checks"]["ranges"]["invariant_violations"])


# ---------------------------------------------------------------------------
# tool wiring
# ---------------------------------------------------------------------------
def test_check_table_vs_raster_is_registered():
    assert "check_table_vs_raster" in {fn.__name__ for fn in tools._TOOLS}


def test_choose_table_arg_helper():
    specs = {
        "biophysical_table_path": {"index_col": "lucode"},
        "demand_table_path": {"index_col": "lucode"},
    }
    chosen, err = tools._choose_table_arg("awy", specs, "biophysical_table_path")
    assert chosen == "biophysical_table_path" and err is None

    chosen, err = tools._choose_table_arg("awy", specs, "")
    assert chosen is None and "several" in err["error"]

    chosen, err = tools._choose_table_arg("carbon", {"carbon_pools_path": {"index_col": "lucode"}}, "")
    assert chosen == "carbon_pools_path" and err is None
