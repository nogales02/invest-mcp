"""Soil fetch -- SoilGrids VRT URLs, texture triangle, HSG mapping, EPIC K, guards.

The actual /vsicurl/ WarpedVRT read runs in the invest-geo env and is exercised
by an end-to-end run, not here.
"""

import json

import pytest

from invest_mcp import tools
from invest_mcp.geo import client as geo_client
from invest_mcp.geo import soil


# ---------------------------------------------------------------------------
# SoilGrids 2.0 VRT URLs / constants
# ---------------------------------------------------------------------------
def test_sg_vrt_url_shape():
    assert soil._sg_vrt_url("clay", "0-5cm", "mean") == (
        "/vsicurl/https://files.isric.org/soilgrids/latest/data/"
        "clay/clay_0-5cm_mean.vrt"
    )
    assert soil._sg_vrt_url("soc", "100-200cm", "Q0.5").endswith(
        "soc/soc_100-200cm_Q0.5.vrt")


def test_depth_and_stat_constants():
    assert soil._SG_DEPTHS[0] == "0-5cm" and len(soil._SG_DEPTHS) == 6
    assert "mean" in soil._SG_STATS
    assert set(soil._FRACTIONS) == {"sand", "silt", "clay"}
    assert soil._TO_PERCENT["soc"] == 100.0 and soil._TO_PERCENT["clay"] == 10.0


def test_bdticm_url_and_variable():
    assert "depth_to_bedrock" in soil._VARIABLES
    assert soil._BDTICM_URL == (
        "/vsicurl/https://files.isric.org/soilgrids/former/2017-03-10/data/"
        "BDTICM_M_250m_ll.tif"
    )


# ---------------------------------------------------------------------------
# USDA texture triangle
# ---------------------------------------------------------------------------
def test_texture_class_known_points():
    np = pytest.importorskip("numpy")

    def cls(sand, silt, clay):
        code = soil._texture_class_code(
            np.array([[sand]], float), np.array([[silt]], float),
            np.array([[clay]], float))[0, 0]
        return soil._TEXTURE_CLASSES[code - 1] if code else None

    assert cls(95, 3, 2) == "sand"
    assert cls(40, 40, 20) == "loam"
    assert cls(20, 60, 20) == "silt loam"
    assert cls(10, 5, 85) == "clay"
    assert cls(55, 10, 35) == "sandy clay"
    assert cls(33, 33, 34) == "clay loam"
    assert cls(10, 85, 5) == "silt"


def test_texture_class_zero_where_masked():
    np = pytest.importorskip("numpy")

    sand = np.ma.masked_array([[40.0]], mask=[[True]])
    silt = np.ma.array([[40.0]])
    clay = np.ma.array([[20.0]])
    assert soil._texture_class_code(sand, silt, clay)[0, 0] == 0


# ---------------------------------------------------------------------------
# hydrologic soil group
# ---------------------------------------------------------------------------
def test_hsg_from_texture_mapping():
    np = pytest.importorskip("numpy")

    codes = np.array([[1, 4, 7, 12, 0]], dtype="int32")
    out = soil._hsg_from_texture(codes)
    assert out.tolist() == [[1, 2, 3, 4, 0]]
    assert out.dtype == np.uint8


def test_hsg_covers_every_texture_code():
    assert set(soil._HSG_BY_TEXCODE) == set(range(1, 13))
    assert set(soil._HSG_BY_TEXCODE.values()) == {1, 2, 3, 4}


# ---------------------------------------------------------------------------
# soil erodibility K (Williams / EPIC)
# ---------------------------------------------------------------------------
def test_usle_k_in_physical_range_and_silt_most_erodible():
    np = pytest.importorskip("numpy")

    def k(sand, silt, clay, oc):
        return float(soil._usle_k_epic(
            np.array([[sand]], float), np.array([[silt]], float),
            np.array([[clay]], float), np.array([[oc]], float))[0, 0])

    k_loam = k(40, 40, 20, 1.5)
    k_sand = k(90, 5, 5, 0.4)
    k_silt = k(10, 75, 15, 1.0)
    for v in (k_loam, k_sand, k_silt):
        assert 0.0 <= v <= 0.07
    assert k_silt > k_loam > k_sand            # silty soils erode most, sand least


def test_usle_k_masks_nonfinite():
    np = pytest.importorskip("numpy")

    out = soil._usle_k_epic(
        np.ma.masked_array([[40.0]], mask=[[True]]),
        np.ma.array([[40.0]]), np.ma.array([[20.0]]), np.ma.array([[1.0]]))
    assert np.ma.is_masked(out)


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


def test_run_fetch_soil_builds_payload(captured):
    geo_client.run_fetch_soil(
        "k.tif", "usle_k", settings=None, aoi_path="aoi.shp", depth="5-15cm",
        target_crs="EPSG:32733")
    assert captured["module"] == "invest_mcp.geo.soil"
    p = captured["payload"]
    assert p["variable"] == "usle_k" and p["source"] == "soilgrids"
    assert p["depth"] == "5-15cm" and p["stat"] == "mean"
    assert p["aoi_path"] == "aoi.shp" and p["target_crs"] == "EPSG:32733"
    assert captured["label"] == "soil fetch"
    json.dumps(p)


# ---------------------------------------------------------------------------
# tool guards
# ---------------------------------------------------------------------------
def test_tool_rejects_bad_variable():
    out = tools.fetch_soil("s.tif", "porosity", bbox=[0, 0, 1, 1])
    assert out["ok"] is False and "hydrologic_soil_group" in out["error"]
    assert "depth_to_bedrock" in out["error"]


def test_depth_to_bedrock_builds_payload(captured):
    geo_client.run_fetch_soil(
        "bedrock.tif", "depth_to_bedrock", settings=None, bbox_wgs84=[6, 45, 7, 46])
    p = captured["payload"]
    assert p["variable"] == "depth_to_bedrock" and p["source"] == "soilgrids"


def test_tool_texture_requires_fraction_placeholder(tmp_path, monkeypatch):
    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    out = tools.fetch_soil(str(tmp_path / "soil.tif"), "texture",
                           bbox=[6.1, 45.2, 6.9, 45.9])
    assert out["ok"] is False and "{fraction}" in out["error"]


def test_tool_rejects_bad_depth(tmp_path, monkeypatch):
    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    out = tools.fetch_soil(str(tmp_path / "hsg.tif"), "hydrologic_soil_group",
                           bbox=[6.1, 45.2, 6.9, 45.9], depth="0-10cm")
    assert out["ok"] is False and "depth" in out["error"]


def test_tool_requires_aoi_or_bbox():
    out = tools.fetch_soil("hsg.tif", "hydrologic_soil_group")
    assert out["ok"] is False and "aoi_path or bbox" in out["error"]


def test_tool_rejects_dst_outside_allowed_roots():
    out = tools.fetch_soil("C:/Windows/Temp/hsg.tif", "hydrologic_soil_group",
                           bbox=[6.1, 45.2, 6.9, 45.9])
    assert out["ok"] is False and "outside every allowed folder" in out["error"]
