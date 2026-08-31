"""aggregate_to_units -- pure spec normalisation, payload building, tool guards.

The GDAL-backed zonal roll-up runs in invest_mcp.geo.aggregate inside the
invest-geo env and is exercised by an end-to-end run, not here.
"""

import json

import pytest

from invest_mcp import tools
from invest_mcp.geo import client as geo_client


# ---------------------------------------------------------------------------
# _aggregate_raster_specs
# ---------------------------------------------------------------------------
def test_specs_single_path_string():
    specs = tools._aggregate_raster_specs("C:/d/diff_c_storage_bas.tif")
    assert specs == [{"path": "C:/d/diff_c_storage_bas.tif",
                      "label": "diff_c_storage_bas"}]


def test_specs_list_of_paths_slugs_and_dedupes_labels():
    specs = tools._aggregate_raster_specs(
        ["C:/a/carbon delta.tif", "C:/b/carbon delta.tif"])
    assert [s["label"] for s in specs] == ["carbon_delta", "carbon_delta_2"]


def test_specs_global_value_per_unit_is_the_default():
    specs = tools._aggregate_raster_specs(
        [{"path": "a.tif"}, {"path": "b.tif", "value_per_unit": 12}],
        value_per_unit=50)
    assert specs[0]["value_per_unit"] == 50.0      # inherited
    assert specs[1]["value_per_unit"] == 12.0      # per-raster wins


def test_specs_carry_label_and_units():
    specs = tools._aggregate_raster_specs(
        [{"path": "a.tif", "label": "Sed. export!", "units": "t"}])
    assert specs[0]["label"] == "Sed_export" and specs[0]["units"] == "t"


def test_specs_empty_raises():
    for bad in (None, "", [], "   "):
        with pytest.raises(ValueError):
            tools._aggregate_raster_specs(bad)


def test_specs_missing_path_raises():
    with pytest.raises(ValueError):
        tools._aggregate_raster_specs([{"label": "x"}])


def test_specs_non_numeric_value_raises():
    with pytest.raises(ValueError):
        tools._aggregate_raster_specs([{"path": "a.tif", "value_per_unit": "lots"}])


# ---------------------------------------------------------------------------
# payload building (worker stubbed)
# ---------------------------------------------------------------------------
@pytest.fixture()
def captured(monkeypatch):
    box = {}

    def fake(module, payload, settings, *, timeout, label):
        box.update(module=module, payload=payload, timeout=timeout, label=label)
        return {"ok": True, "units": {}, "rasters": [], "features": [], "notes": []}

    monkeypatch.setattr(geo_client, "_run_geo_worker", fake)
    return box


def test_run_aggregate_builds_payload(captured):
    geo_client.run_aggregate_to_units(
        [{"path": "diff.tif", "label": "d", "value_per_unit": 50.0}],
        "units.gpkg", "out.gpkg", settings=None,
        id_columns=["NAME"], stats=["sum", "mean"], area_weighted=True,
        all_touched=True, value_currency="EUR", max_units=100)
    assert captured["module"] == "invest_mcp.geo.aggregate"
    assert captured["label"] == "aggregate to units"
    p = captured["payload"]
    assert p["rasters"][0]["value_per_unit"] == 50.0
    assert p["units_path"] == "units.gpkg" and p["dst_path"] == "out.gpkg"
    assert p["id_columns"] == ["NAME"] and p["stats"] == ["sum", "mean"]
    assert p["area_weighted"] is True and p["all_touched"] is True
    assert p["value_currency"] == "EUR" and p["max_units"] == 100
    json.dumps(p)


def test_run_aggregate_payload_defaults(captured):
    geo_client.run_aggregate_to_units([{"path": "d.tif"}], "u.shp", "o.gpkg",
                                      settings=None)
    p = captured["payload"]
    assert p["stats"] == ["sum", "mean", "count"]
    assert p["id_columns"] is None and p["value_currency"] == "USD"
    assert p["area_weighted"] is False and p["max_units"] == 5000


# ---------------------------------------------------------------------------
# tool guards
# ---------------------------------------------------------------------------
def test_tool_rejects_empty_rasters(tmp_path, monkeypatch):
    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    out = tools.aggregate_to_units([], str(tmp_path / "u.shp"),
                                   str(tmp_path / "o.gpkg"))
    assert out["ok"] is False and "rasters is required" in out["error"]


def test_tool_rejects_unknown_stat(tmp_path, monkeypatch):
    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    r = tmp_path / "d.tif"
    r.write_bytes(b"x")
    out = tools.aggregate_to_units(str(r), str(tmp_path / "u.shp"),
                                   str(tmp_path / "o.gpkg"), stats=["sum", "p95"])
    assert out["ok"] is False and "p95" in out["error"]


def test_tool_rejects_input_outside_allowed_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    out = tools.aggregate_to_units("C:/Windows/Temp/d.tif",
                                   str(tmp_path / "u.shp"),
                                   str(tmp_path / "o.gpkg"))
    assert out["ok"] is False
    assert "outside every allowed folder" in out["error"] or "does not exist" in out["error"]


def test_tool_rejects_dst_outside_allowed_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    r = tmp_path / "d.tif"
    r.write_bytes(b"x")
    u = tmp_path / "u.shp"
    u.write_bytes(b"x")
    out = tools.aggregate_to_units(str(r), str(u), "C:/Windows/Temp/o.gpkg")
    assert out["ok"] is False and "outside every allowed folder" in out["error"]


def test_tool_reports_env_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    r = tmp_path / "d.tif"
    r.write_bytes(b"x")
    u = tmp_path / "u.shp"
    u.write_bytes(b"x")

    def boom(*a, **k):
        raise RuntimeError("invest-geo python not found")

    monkeypatch.setattr(geo_client, "run_aggregate_to_units", boom)
    out = tools.aggregate_to_units(str(r), str(u), str(tmp_path / "o.gpkg"))
    assert out["ok"] is False and out["env_missing"] is True


def test_tool_happy_path_resolves_paths_and_builds_narrative(tmp_path, monkeypatch):
    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    r = tmp_path / "diff_c_storage_bas.tif"
    r.write_bytes(b"x")
    u = tmp_path / "municipios.shp"
    u.write_bytes(b"x")

    def fake_run(specs, units, dst, settings, **kw):
        assert specs[0]["path"] == str(r.resolve())      # resolved absolute
        assert units == str(u.resolve())
        return {
            "ok": True,
            "units": {"path": units, "feature_count": 2,
                      "id_columns": ["NOMBRE"], "truncated": False},
            "rasters": [{"label": "diff_c_storage_bas", "units": "t",
                         "unit_sum_total": -130285.96, "value_per_unit": 50.0,
                         "value_total": -6514298.0, "value_currency": "USD"}],
            "features": [
                {"feature_index": 0, "properties": {"NOMBRE": "A"},
                 "values": {"diff_c_storage_bas_sum": -1000.0}},
                {"feature_index": 1, "properties": {"NOMBRE": "B"},
                 "values": {"diff_c_storage_bas_sum": -2000.0}},
            ],
            "output_vector": {"path": dst}, "csv_path": dst.replace(".gpkg", ".csv"),
            "notes": [],
        }

    monkeypatch.setattr(geo_client, "run_aggregate_to_units", fake_run)
    out = tools.aggregate_to_units(
        str(r), str(u), str(tmp_path / "agg.gpkg"),
        value_per_unit=50.0, stats=["sum"])
    assert out["ok"] is True
    assert out["dst"].endswith("agg.gpkg")
    assert "valued" in out["narrative"] and "NOMBRE=A" in out["narrative"]
