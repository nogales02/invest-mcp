"""Hydrography fetch -- HydroSHEDS region detection, URI shapes, payload, guards.

The actual /vsizip//vsicurl/ read runs in the invest-geo env and is exercised by
an end-to-end run, not here.
"""

import json

import pytest

from invest_mcp import tools
from invest_mcp.geo import client as geo_client
from invest_mcp.geo import hydrography as hy


# ---------------------------------------------------------------------------
# region tables + auto-detection
# ---------------------------------------------------------------------------
def test_region_tables_complete():
    assert set(hy._REGIONS) == set(hy._REGION_BBOX) == set(hy._REGION_NAMES)
    assert len(hy._REGIONS) == 9
    for minx, miny, maxx, maxy in hy._REGION_BBOX.values():
        assert minx < maxx and miny < maxy


def test_regions_containing_alps_is_unambiguous():
    assert hy._regions_containing(6.9, 45.9) == ["eu"]


def test_resolve_region_explicit_wins():
    assert hy._resolve_region({"region": "NA"}, [-80, 30, -70, 40]) == "na"


def test_resolve_region_rejects_unknown_code():
    with pytest.raises(ValueError, match="region must be one of"):
        hy._resolve_region({"region": "xx"}, [0, 0, 1, 1])


def test_resolve_region_auto_from_centroid():
    # small AOI near Nairobi -> Africa only
    assert hy._resolve_region({}, [36.7, -1.4, 37.0, -1.1]) == "af"


def test_resolve_region_ambiguous_asks_for_region():
    # Panama isthmus centroid sits in both na and sa envelopes
    with pytest.raises(ValueError, match="disambiguate"):
        hy._resolve_region({}, [-80.2, 8.9, -79.8, 9.3])


def test_resolve_region_outside_all_envelopes():
    with pytest.raises(ValueError, match="outside every HydroSHEDS"):
        hy._resolve_region({}, [-31.0, -31.0, -29.0, -29.0])


# ---------------------------------------------------------------------------
# source URIs
# ---------------------------------------------------------------------------
def test_source_uri_rivers():
    uri = hy._source_uri("rivers", "eu", 8)
    assert uri == (
        "/vsizip//vsicurl/https://data.hydrosheds.org/file/HydroRIVERS/"
        "HydroRIVERS_v10_eu_shp.zip/HydroRIVERS_v10_eu_shp/HydroRIVERS_v10_eu.shp"
    )


def test_source_uri_basins_zero_pads_level():
    uri = hy._source_uri("basins", "sa", 6)
    assert uri.endswith("hydrobasins/standard/hybas_sa_lev06_v1c.zip")
    assert uri.startswith("/vsizip//vsicurl/https://data.hydrosheds.org/")


def test_pick_driver():
    from pathlib import Path

    assert hy._pick_driver(Path("x.gpkg")) == "GPKG"
    assert hy._pick_driver(Path("x.shp")) == "ESRI Shapefile"
    assert hy._pick_driver(Path("x.geojson")) == "GeoJSON"
    with pytest.raises(ValueError, match="unsupported vector extension"):
        hy._pick_driver(Path("x.kml"))


# ---------------------------------------------------------------------------
# payload building
# ---------------------------------------------------------------------------
@pytest.fixture()
def captured(monkeypatch):
    box = {}

    def fake(module, payload, settings, *, timeout, label):
        box.update(module=module, payload=payload, timeout=timeout, label=label)
        return {"ok": True, "product": payload["product"], "feature_count": 0}

    monkeypatch.setattr(geo_client, "_run_geo_worker", fake)
    return box


def test_run_fetch_hydrography_builds_payload(captured):
    geo_client.run_fetch_hydrography(
        "rivers.gpkg", "rivers", settings=None, bbox_wgs84=[6, 45, 7, 46],
        region="eu", target_crs="EPSG:32632")
    assert captured["module"] == "invest_mcp.geo.hydrography"
    p = captured["payload"]
    assert p["product"] == "rivers" and p["source"] == "hydrosheds"
    assert p["region"] == "eu" and p["level"] == 8
    assert p["clip_to_aoi"] is False and p["target_crs"] == "EPSG:32632"
    assert captured["label"] == "hydrography fetch"
    json.dumps(p)


# ---------------------------------------------------------------------------
# tool guards
# ---------------------------------------------------------------------------
def test_tool_rejects_bad_product():
    out = tools.fetch_hydrography("h.gpkg", "lakes", bbox=[0, 0, 1, 1])
    assert out["ok"] is False and "rivers" in out["error"]


def test_tool_rejects_bad_region():
    out = tools.fetch_hydrography("h.gpkg", "rivers", bbox=[0, 0, 1, 1], region="zz")
    assert out["ok"] is False and "region must be one of" in out["error"]


def test_tool_rejects_bad_level():
    out = tools.fetch_hydrography("h.gpkg", "basins", bbox=[0, 0, 1, 1], level=13)
    assert out["ok"] is False and "1..12" in out["error"]


def test_tool_requires_aoi_or_bbox():
    out = tools.fetch_hydrography("h.gpkg", "rivers")
    assert out["ok"] is False and "aoi_path or bbox" in out["error"]


def test_tool_rejects_dst_outside_allowed_roots():
    out = tools.fetch_hydrography("C:/Windows/Temp/h.gpkg", "rivers", bbox=[6, 45, 7, 46])
    assert out["ok"] is False and "outside every allowed folder" in out["error"]
