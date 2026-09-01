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
# ESA WorldCover tile names / coverage (3-degree grid)
# ---------------------------------------------------------------------------
def test_floor3_snaps_to_three_degree_grid():
    assert fetch._floor3(45.9) == 45
    assert fetch._floor3(46.5) == 45          # tile spans 45..48
    assert fetch._floor3(48.0) == 48
    assert fetch._floor3(-12.5) == -15        # tile spans -15..-12
    assert fetch._floor3(-15.0) == -15


def test_worldcover_urls_2021_single_tile():
    urls = fetch._worldcover_urls([6.1, 45.2, 6.9, 46.8], 2021)
    assert [n for n, _ in urls] == ["ESA_WorldCover_10m_2021_v200_N45E006_Map"]
    assert urls[0][1] == (
        "/vsicurl/https://esa-worldcover.s3.eu-central-1.amazonaws.com/"
        "v200/2021/map/ESA_WorldCover_10m_2021_v200_N45E006_Map.tif"
    )


def test_worldcover_urls_2020_uses_v100_and_year_dir():
    (name, url), = fetch._worldcover_urls([6.1, 45.2, 6.9, 46.8], 2020)
    assert name == "ESA_WorldCover_10m_2020_v100_N45E006_Map"
    assert "/v100/2020/map/" in url


def test_worldcover_urls_span_multiple_tiles_south_west():
    names = {n for n, _ in fetch._worldcover_urls([-1.5, -13.5, 2.0, -11.0], 2021)}
    assert names == {
        "ESA_WorldCover_10m_2021_v200_S15W003_Map",
        "ESA_WorldCover_10m_2021_v200_S15E000_Map",
        "ESA_WorldCover_10m_2021_v200_S12W003_Map",
        "ESA_WorldCover_10m_2021_v200_S12E000_Map",
    }


def test_worldcover_legend_has_eleven_classes():
    assert set(fetch._WORLDCOVER_LEGEND) == {10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100}


# ---------------------------------------------------------------------------
# block-tiled mosaic grid (peak RAM = one block, ported from FUNCTIONS/)
# ---------------------------------------------------------------------------
def test_fetch_block_px_default_and_override(monkeypatch):
    monkeypatch.delenv("INVEST_MCP_FETCH_BLOCK_PX", raising=False)
    assert fetch._fetch_block_px() == 4096
    monkeypatch.setenv("INVEST_MCP_FETCH_BLOCK_PX", "2048")
    assert fetch._fetch_block_px() == 2048
    monkeypatch.setenv("INVEST_MCP_FETCH_BLOCK_PX", "10")     # floored to 256
    assert fetch._fetch_block_px() == 256
    monkeypatch.setenv("INVEST_MCP_FETCH_BLOCK_PX", "junk")
    assert fetch._fetch_block_px() == 4096


def test_mosaic_grid_rounds_up_and_never_clips():
    # 1 degree bbox at 30 m (1/3600 deg) -> 3600 px, origin at NW corner
    w, h, (west, north) = fetch._mosaic_grid([16.0, -13.0, 17.0, -12.0],
                                             1 / 3600, 1 / 3600)
    assert (w, h) == (3600, 3600)
    assert (west, north) == (16.0, -12.0)

    # non-integer multiple must round *up* so the last row/col is covered
    w, h, _ = fetch._mosaic_grid([0.0, 0.0, 1.0, 1.0], 0.3, 0.3)
    assert (w, h) == (4, 4)


def test_mosaic_grid_block_walk_tiles_the_whole_grid():
    # a grid bigger than one block must split into >1 blocks with no gaps
    w, h, _ = fetch._mosaic_grid([0.0, 0.0, 3.0, 2.0], 3 / 12000, 3 / 12000)
    assert (w, h) == (12000, 8000)
    block = 4096
    seen_w = sum(min(block, w - c) for c in range(0, w, block))
    seen_h = sum(min(block, h - r) for r in range(0, h, block))
    assert seen_w == w and seen_h == h
    assert len(range(0, w, block)) == 3 and len(range(0, h, block)) == 2


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


def test_run_fetch_landcover_builds_payload(captured):
    geo_client.run_fetch_landcover(
        "lc.tif", settings=None, bbox_wgs84=[6.1, 45.2, 6.9, 45.9], year=2020)
    p = captured["payload"]
    assert p["op"] == "landcover" and p["source"] == "worldcover" and p["year"] == 2020
    assert p["resampling"] == "nearest"          # categorical default
    assert p["bbox_wgs84"] == [6.1, 45.2, 6.9, 45.9]
    assert captured["label"] == "land-cover fetch"
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


def test_fetch_landcover_requires_aoi_or_bbox():
    out = tools.fetch_landcover("/x/lc.tif")
    assert out["ok"] is False and "aoi_path or bbox" in out["error"]


def test_fetch_landcover_rejects_bad_year(tmp_path, monkeypatch):
    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    out = tools.fetch_landcover(str(tmp_path / "lc.tif"),
                                bbox=[6.1, 45.2, 6.9, 45.9], year=2019)
    assert out["ok"] is False and "2020 or 2021" in out["error"]
