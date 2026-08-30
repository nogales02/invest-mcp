"""Climate fetch -- WorldClim member URLs, Hargreaves radiation, guards.

The actual /vsizip//vsicurl/ download runs in the invest-geo env and is
exercised by an end-to-end run, not here.
"""

import json

import pytest

from invest_mcp import tools
from invest_mcp.geo import climate
from invest_mcp.geo import client as geo_client


# ---------------------------------------------------------------------------
# WorldClim member URLs
# ---------------------------------------------------------------------------
def test_wc_member_url_shape():
    u = climate._wc_member_url("prec", "10m", 3)
    assert u == (
        "/vsizip//vsicurl/https://geodata.ucdavis.edu/climate/worldclim/2_1/base/"
        "wc2.1_10m_prec.zip/wc2.1_10m_prec_03.tif"
    )
    assert climate._wc_member_url("tmin", "30s", 12).endswith("wc2.1_30s_tmin_12.tif")


def test_month_tables_have_twelve_entries():
    assert len(climate._DAYS_IN_MONTH) == 12
    assert len(climate._MID_MONTH_DOY) == 12
    assert sum(climate._DAYS_IN_MONTH) == 365


# ---------------------------------------------------------------------------
# _months_for
# ---------------------------------------------------------------------------
def test_months_for_annual_is_all_twelve():
    assert climate._months_for({"period": "annual"}) == list(range(1, 13))


def test_months_for_monthly_subset_sorted_deduped():
    assert climate._months_for({"period": "monthly", "months": [7, 1, 7, 3]}) == [1, 3, 7]


def test_months_for_rejects_out_of_range():
    with pytest.raises(ValueError):
        climate._months_for({"period": "monthly", "months": [0, 5]})
    with pytest.raises(ValueError):
        climate._months_for({"period": "monthly", "months": [13]})


# ---------------------------------------------------------------------------
# extraterrestrial radiation (Hargreaves)
# ---------------------------------------------------------------------------
def test_ra_positive_and_seasonally_ordered():
    np = pytest.importorskip("numpy")  # _ra_mm_per_day is a worker (invest-geo) helper

    ra_jun = climate._ra_mm_per_day(np.array([45.0]), 6)[0]
    ra_dec = climate._ra_mm_per_day(np.array([45.0]), 12)[0]
    assert ra_jun > 0 and ra_dec > 0
    assert ra_jun > ra_dec                       # N-hemisphere summer > winter
    # southern hemisphere is the mirror image
    ra_jun_s = climate._ra_mm_per_day(np.array([-45.0]), 6)[0]
    ra_dec_s = climate._ra_mm_per_day(np.array([-45.0]), 12)[0]
    assert ra_dec_s > ra_jun_s
    # equatorial June value in a sane physical range (mm/day equivalent)
    ra_eq = climate._ra_mm_per_day(np.array([0.0]), 6)[0]
    assert 10.0 < ra_eq < 18.0


# ---------------------------------------------------------------------------
# payload building
# ---------------------------------------------------------------------------
@pytest.fixture()
def captured(monkeypatch):
    box = {}

    def fake(module, payload, settings, *, timeout, label):
        box.update(module=module, payload=payload, timeout=timeout, label=label)
        return {"ok": True, "variable": payload["variable"], "outputs": []}

    monkeypatch.setattr(geo_client, "_run_geo_worker", fake)
    return box


def test_run_fetch_climate_builds_payload(captured):
    geo_client.run_fetch_climate(
        "p_{month}.tif", "precipitation", settings=None, period="monthly",
        months=[6, 7, 8], resolution="30s", aoi_path="aoi.shp")
    assert captured["module"] == "invest_mcp.geo.climate"
    p = captured["payload"]
    assert p["variable"] == "precipitation" and p["source"] == "worldclim"
    assert p["period"] == "monthly" and p["months"] == [6, 7, 8]
    assert p["resolution"] == "30s" and p["aoi_path"] == "aoi.shp"
    assert captured["label"] == "climate fetch"
    json.dumps(p)


# ---------------------------------------------------------------------------
# tool guards
# ---------------------------------------------------------------------------
def test_tool_rejects_bad_variable():
    out = tools.fetch_climate("x_{month}.tif", "humidity", bbox=[0, 0, 1, 1])
    assert out["ok"] is False and "precipitation" in out["error"]


def test_tool_monthly_requires_month_placeholder(tmp_path, monkeypatch):
    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    out = tools.fetch_climate(str(tmp_path / "precip.tif"), "precipitation",
                              bbox=[6.1, 45.2, 6.9, 45.9], period="monthly")
    assert out["ok"] is False and "{month}" in out["error"]


def test_tool_rejects_bad_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    out = tools.fetch_climate(str(tmp_path / "p_{month}.tif"), "precipitation",
                              bbox=[6.1, 45.2, 6.9, 45.9], resolution="1km")
    assert out["ok"] is False and "resolution" in out["error"]


def test_tool_requires_aoi_or_bbox():
    out = tools.fetch_climate("p_{month}.tif", "precipitation")
    assert out["ok"] is False and "aoi_path or bbox" in out["error"]
