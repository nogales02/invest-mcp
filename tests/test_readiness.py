"""Project data-readiness -- role guessing, project scan, per-model assessment.

All pure: fake specs, a fake project tree. No InVEST CLI, no GDAL.
"""

from invest_mcp.workspace import readiness

CARBON_SPEC = {
    "model_id": "carbon",
    "model_title": "Carbon Storage and Sequestration",
    "args": {
        "workspace_dir": {"type": "directory", "required": True},
        "lulc_bas_path": {"type": "raster", "required": True, "about": "Baseline LULC."},
        "lulc_alt_path": {"type": "raster", "required": False},
        "carbon_pools_path": {"type": "csv", "required": True, "about": "Carbon pools."},
    },
}

SDR_SPEC = {
    "model_id": "sdr",
    "model_title": "Sediment Delivery Ratio",
    "args": {
        "workspace_dir": {"type": "directory", "required": True},
        "dem_path": {"type": "raster", "required": True},
        "erosivity_path": {"type": "raster", "required": True},
        "erodibility_path": {"type": "raster", "required": True},
        "lulc_path": {"type": "raster", "required": True},
        "watersheds_path": {"type": "vector", "required": True},
        "biophysical_table_path": {"type": "csv", "required": True},
        "drainage_path": {"type": "raster", "required": False},
        "k_param": {"type": "number", "required": True},
        "sdr_max": {"type": "ratio", "required": True},
    },
}


def _tree(root, rels):
    for rel in rels:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return root


# ---------------------------------------------------------------------------
# guess_role
# ---------------------------------------------------------------------------
def test_guess_role_matches_filenames_and_arg_names():
    assert readiness.guess_role("DEM.tif") == "dem"
    assert readiness.guess_role("LULC_2021.tif") == "lulc"
    assert readiness.guess_role("Basin.shp") == "watersheds"
    assert readiness.guess_role("SubBasin.shp") == "watersheds"
    assert readiness.guess_role("01-Biophysical_Table.csv") == "biophysical_table"
    assert readiness.guess_role("Carbon_Pools.csv") == "carbon_table"
    assert readiness.guess_role("erosivity_path") == "erosivity"
    assert readiness.guess_role("random_thing.tif") is None


# ---------------------------------------------------------------------------
# scan_project
# ---------------------------------------------------------------------------
def test_scan_project_classifies_and_skips(tmp_path):
    root = _tree(tmp_path / "p", [
        "data/raw/DEM.tif",
        "data/processed/LULC.tif",
        "data/raw/Basin.shp",
        "tables/Biophysical_Table.csv",
        "data/raw/notes.txt",              # non-geo -> skipped
        "jobs/run1/workspace/out.tif",     # under jobs/ -> skipped
        "logs/run.log",                    # under logs/ -> skipped
    ])
    inv = readiness.scan_project(root)
    rels = {i["relpath"]: i for i in inv}
    assert set(rels) == {
        "data/raw/DEM.tif", "data/processed/LULC.tif",
        "data/raw/Basin.shp", "tables/Biophysical_Table.csv",
    }
    assert rels["data/raw/DEM.tif"]["kind"] == "raster"
    assert rels["data/raw/DEM.tif"]["role"] == "dem"
    assert rels["data/raw/Basin.shp"]["kind"] == "vector"
    assert rels["tables/Biophysical_Table.csv"]["kind"] == "table"
    assert rels["tables/Biophysical_Table.csv"]["role"] == "biophysical_table"


# ---------------------------------------------------------------------------
# assess_model
# ---------------------------------------------------------------------------
def test_assess_model_all_inputs_matched(tmp_path):
    root = _tree(tmp_path / "p", [
        "data/processed/LULC.tif", "tables/Carbon_Pools.csv",
    ])
    inv = readiness.scan_project(root)
    a = readiness.assess_model("carbon", CARBON_SPEC, inv)
    assert a["can_attempt"] is True
    assert set(a["matched"]) == {"lulc_bas_path", "carbon_pools_path"}
    assert a["missing"] == [] and a["ambiguous"] == []
    assert a["needs_values"] == []          # workspace_dir is server-managed


def test_assess_model_reports_missing_inputs(tmp_path):
    root = _tree(tmp_path / "p", ["data/processed/LULC.tif"])   # no carbon pools table
    inv = readiness.scan_project(root)
    a = readiness.assess_model("carbon", CARBON_SPEC, inv)
    assert a["can_attempt"] is False
    assert [m["arg"] for m in a["missing"]] == ["carbon_pools_path"]
    assert a["missing"][0]["role"] == "carbon_table"


def test_assess_model_flags_ambiguous_when_two_files_share_a_role(tmp_path):
    root = _tree(tmp_path / "p", [
        "data/raw/Basin.shp", "data/raw/SubBasin.shp",   # both -> watersheds
        "data/processed/DEM.tif", "data/processed/erosivity.tif",
        "data/processed/erodibility.tif", "data/processed/LULC.tif",
        "tables/Biophysical_Table.csv",
    ])
    inv = readiness.scan_project(root)
    a = readiness.assess_model("sdr", SDR_SPEC, inv)
    assert a["can_attempt"] is False
    amb = {x["arg"] for x in a["ambiguous"]}
    assert "watersheds_path" in amb
    # scalar required args surface separately, never as blockers
    assert set(a["needs_values"]) == {"k_param", "sdr_max"}


def test_narrative_mentions_ready_and_gap_models(tmp_path):
    root = _tree(tmp_path / "p", ["data/processed/LULC.tif", "tables/Carbon_Pools.csv"])
    inv = readiness.scan_project(root)
    assessments = [
        readiness.assess_model("carbon", CARBON_SPEC, inv),
        readiness.assess_model("sdr", SDR_SPEC, inv),
    ]
    text = readiness.narrative(inv, assessments)
    assert "`carbon`" in text and "ready to attempt" in text
    assert "`sdr`" in text
