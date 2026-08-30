"""DEM fetch -- Copernicus GLO-30 tile maths, payload building, tool guards.

The actual /vsicurl/ download + mosaic runs in the invest-geo env against a live
public bucket and is exercised by an end-to-end run, not here.
"""

import json

import pytest

from invest_mcp import tools
from invest_mcp.geo import client as geo_client
from invest_mcp.geo import fetch


# ---------------------------------------------------------------------------
# Copernicus GLO-30 tile names / coverage
# ---------------------------------------------------------------------------
def test_cop30_tile_name_hemispheres():
    assert fetch._cop30_tile_name(45, 6) == "Copernicus_DSM_COG_10_N45_00_E006_00_DEM"
    assert fetch._cop30_tile_name(-13, 16) == "Copernicus_DSM_COG_10_S13_00_E016_00_DEM"
    assert fetch._cop30_tile_name(-1, -150) == "Copernicus_DSM_COG_10_S01_00_W150_00_DEM"
    assert fetch._cop30_tile_name(0, 0) == "Copernicus_DSM_COG_10_N00_00_E000_00_DEM"


def test_cop30_urls_single_tile_when_bbox_inside_one_degree():
    urls = fetch._cop30_urls([16.67, -12.65, 16.98, -12.21])
    assert [n for n, _ in urls] == ["Copernicus_DSM_COG_10_S13_00_E016_00_DEM"]
    assert urls[0][1].startswith("/vsicurl/https://copernicus-dem-30m.s3.amazonaws.com/")


def test_cop30_urls_span_multiple_degrees():
    names = [n for n, _ in fetch._cop30_urls([5.8, 45.2, 7.1, 46.3])]
    assert set(names) == {
        "Copernicus_DSM_COG_10_N45_00_E005_00_DEM",
        "Copernicus_DSM_COG_10_N45_00_E006_00_DEM",
        "Copernicus_DSM_COG_10_N45_00_E007_00_DEM",
        "Copernicus_DSM_COG_10_N46_00_E005_00_DEM",
        "Copernicus_DSM_COG_10_N46_00_E006_00_DEM",
        "Copernicus_DSM_COG_10_N46_00_E007_00_DEM",
    }


def test_cop30_urls_upper_bound_on_degree_line_excludes_next_tile():
    # bbox top edge exactly on 46.0 must not pull in the N46 row
    names = [n for n, _ in fetch._cop30_urls([6.1, 45.1, 6.9, 46.0])]
    assert names == ["Copernicus_DSM_COG_10_N45_00_E006_00_DEM"]


# ---------------------------------------------------------------------------
# payload building
# ---------------------------------------------------------------------------
@pytest.fixture()
def captured(monkeypatch):
    box = {}

    def fake(module, payload, settings, *, timeout, label):
        box.update(module=module, payload=payload, timeout=timeout, label=label)
        return {"ok": True, "raster": {}, "tiles_used": ["t"], "tiles_missing": []}

    monkeypatch.setattr(geo_client, "_run_geo_worker", fake)
    return box


def test_run_fetch_dem_builds_payload(captured):
    geo_client.run_fetch_dem(
        "dem.tif", settings=None, aoi_path="aoi.shp", target_crs="EPSG:32733",
        target_resolution=[30.0, 30.0], buffer_deg=0.1)
    assert captured["module"] == "invest_mcp.geo.fetch"
    p = captured["payload"]
    assert p["op"] == "dem" and p["source"] == "cop30"
    assert p["aoi_path"] == "aoi.shp" and p["bbox_wgs84"] is None
    assert p["target_crs"] == "EPSG:32733" and p["target_resolution"] == [30.0, 30.0]
    assert p["clip_to_aoi"] is True and p["buffer_deg"] == 0.1
    json.dumps(p)


# ---------------------------------------------------------------------------
# tool guards
# ---------------------------------------------------------------------------
def test_tool_requires_aoi_or_bbox():
    out = tools.fetch_dem("/x/dem.tif")
    assert out["ok"] is False and "aoi_path or bbox" in out["error"]


def test_tool_rejects_bad_bbox_length(tmp_path, monkeypatch):
    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    out = tools.fetch_dem(str(tmp_path / "dem.tif"), bbox=[1, 2, 3])
    assert out["ok"] is False and "minx, miny, maxx, maxy" in out["error"]


def test_tool_rejects_dst_outside_allowed_roots(tmp_path):
    out = tools.fetch_dem("C:/Windows/Temp/dem.tif", bbox=[16.6, -12.7, 17.0, -12.2])
    assert out["ok"] is False and "outside every allowed folder" in out["error"]
