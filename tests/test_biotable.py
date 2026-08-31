"""Biophysical-table skeletons -- column expansion, CSV assembly, spec parsing.

All pure: fake specs, no GDAL, no invest.exe.
"""

import csv
import io

from invest_mcp.models import spec_translate
from invest_mcp.workspace import biotable

CARBON_SPEC = {
    "model_id": "carbon",
    "model_title": "Carbon Storage and Sequestration",
    "args": {
        "carbon_pools_path": {
            "type": "csv", "required": True, "index_col": "lucode",
            "columns": [
                {"id": "lucode", "about": "LULC code", "required": True, "allowed": True},
                {"id": "c_above", "about": "Aboveground C", "required": True,
                 "allowed": True, "units": "t/ha"},
                {"id": "c_soil", "about": "Soil C", "required": True, "allowed": True},
                {"id": "c_secret", "about": "hidden", "required": True, "hidden": True},
            ],
        },
        "workspace_dir": {"type": "directory", "required": True},
    },
}

NDR_SPEC = {
    "model_id": "ndr",
    "args": {
        "biophysical_table_path": {
            "type": "csv", "required": True, "index_col": "lucode",
            "columns": [
                {"id": "lucode", "required": True, "allowed": True},
                {"id": "load_n", "about": "N load", "required": "calc_n", "allowed": True},
                {"id": "eff_n", "about": "N retention eff", "required": "calc_n",
                 "allowed": True},
                {"id": "load_type_n", "about": "load mode", "required": "calc_n",
                 "allowed": True},
                {"id": "load_p", "about": "P load", "required": "calc_p", "allowed": True},
                {"id": "note", "about": "free text", "required": False, "allowed": True},
            ],
        },
    },
}

SWY_SPEC = {
    "model_id": "seasonal_water_yield",
    "args": {
        "biophysical_table_path": {
            "type": "csv", "required": True, "index_col": "lucode",
            "columns": [
                {"id": "lucode", "required": True, "allowed": True},
                {"id": "cn_[SOIL_GROUP]", "about": "curve number", "required": True,
                 "allowed": True},
                {"id": "kc_[MONTH]", "about": "crop coefficient", "required": True,
                 "allowed": True},
            ],
        },
    },
}


# ---------------------------------------------------------------------------
# spec_translate.table_arg_specs
# ---------------------------------------------------------------------------
def test_table_arg_specs_extracts_columns_and_key():
    t = spec_translate.table_arg_specs(CARBON_SPEC)
    assert set(t) == {"carbon_pools_path"}
    spec = t["carbon_pools_path"]
    assert spec["index_col"] == "lucode"
    ids = [c["id"] for c in spec["columns"]]
    assert ids == ["lucode", "c_above", "c_soil"]          # hidden dropped
    assert spec["columns"][1]["units"] == "t/ha"


# ---------------------------------------------------------------------------
# expand_column
# ---------------------------------------------------------------------------
def test_expand_column_known_tokens():
    cols, note = biotable.expand_column("kc_[MONTH]")
    assert cols == [f"kc_{m}" for m in range(1, 13)] and note is None
    cols, note = biotable.expand_column("cn_[SOIL_GROUP]")
    assert cols == ["cn_a", "cn_b", "cn_c", "cn_d"] and note is None


def test_expand_column_unknown_token_kept_with_note():
    cols, note = biotable.expand_column("nesting_[SUBSTRATE]_index")
    assert cols == ["nesting_[SUBSTRATE]_index"]
    assert note and "[SUBSTRATE]" in note


def test_expand_column_plain():
    assert biotable.expand_column("usle_c") == (["usle_c"], None)


# ---------------------------------------------------------------------------
# build_template
# ---------------------------------------------------------------------------
def _rows(csv_text):
    return list(csv.reader(io.StringIO(csv_text)))


def test_build_template_one_row_per_code_blank_cells():
    cols = spec_translate.table_arg_specs(CARBON_SPEC)["carbon_pools_path"]["columns"]
    out = biotable.build_template("lucode", cols, [1, 2, 10])
    rows = _rows(out["csv"])
    assert rows[0] == ["lucode", "c_above", "c_soil"]
    assert rows[1] == ["1", "", ""]
    assert [r[0] for r in rows[1:]] == ["1", "2", "10"]
    assert out["column_help"]["c_above"]["requirement"] == "required"


def test_build_template_conditional_and_optional_columns():
    cols = spec_translate.table_arg_specs(NDR_SPEC)["biophysical_table_path"]["columns"]
    full = biotable.build_template("lucode", cols, [1], include_optional=True)
    assert "note" in full["headers"]
    assert full["column_help"]["load_n"]["requirement"] == "required if: calc_n"

    lean = biotable.build_template("lucode", cols, [1], include_optional=False)
    assert "note" not in lean["headers"]                 # optional dropped
    assert "load_n" in lean["headers"]                   # conditional kept


def test_build_template_resolves_conditions_from_args():
    cols = spec_translate.table_arg_specs(NDR_SPEC)["biophysical_table_path"]["columns"]
    out = biotable.build_template(
        "lucode", cols, [1], conditions={"calc_n": True, "calc_p": False}
    )
    # calc_n on -> its columns are hard-required
    assert "load_type_n" in out["headers"] and "load_n" in out["headers"]
    assert out["column_help"]["load_n"]["requirement"] == "required"
    # calc_p off -> its columns are dropped entirely
    assert "load_p" not in out["headers"]


def test_build_template_unlisted_condition_stays_advisory():
    cols = spec_translate.table_arg_specs(NDR_SPEC)["biophysical_table_path"]["columns"]
    out = biotable.build_template("lucode", cols, [1], conditions={"calc_n": True})
    # calc_p not mentioned -> column kept, still flagged conditional
    assert "load_p" in out["headers"]
    assert out["column_help"]["load_p"]["requirement"] == "required if: calc_p"


def test_build_template_expands_placeholder_columns():
    cols = spec_translate.table_arg_specs(SWY_SPEC)["biophysical_table_path"]["columns"]
    out = biotable.build_template("lucode", cols, [1, 2])
    assert "cn_a" in out["headers"] and "cn_d" in out["headers"]
    assert "kc_1" in out["headers"] and "kc_12" in out["headers"]
    assert out["column_help"]["kc_1"]["expanded_from"] == "kc_[MONTH]"


def test_build_template_with_legend_adds_description():
    cols = spec_translate.table_arg_specs(CARBON_SPEC)["carbon_pools_path"]["columns"]
    out = biotable.build_template("lucode", cols, [1, 2],
                                  descriptions={1: "forest", 2: "cropland"})
    rows = _rows(out["csv"])
    assert rows[0][-1] == "description"
    assert rows[1][-1] == "forest" and rows[2][-1] == "cropland"


# ---------------------------------------------------------------------------
# parse_legend
# ---------------------------------------------------------------------------
def test_parse_legend_skips_header_and_reads_two_cols():
    text = "code,name\n1,Forest\n2,Cropland,extra\n"
    assert biotable.parse_legend(text) == {1: "Forest", 2: "Cropland"}
