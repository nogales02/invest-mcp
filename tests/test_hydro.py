"""Watershed delineation -- payload building and tool guards.

The pygeoprocessing D8 chain runs in the invest-geo env and is covered by the
end-to-end run, not here.
"""

import json

import pytest

from invest_mcp import tools
from invest_mcp.geo import client as geo_client


@pytest.fixture()
def captured(monkeypatch):
    box = {}

    def fake(module, payload, settings, *, timeout, label):
        box.update(module=module, payload=payload, timeout=timeout, label=label)
        return {"ok": True, "watersheds": {"feature_count": 3}, "snap_report": []}

    monkeypatch.setattr(geo_client, "_run_geo_worker", fake)
    return box


def test_run_delineate_watersheds_builds_payload(captured):
    geo_client.run_delineate_watersheds(
        "dem.tif", "pts.shp", "ws.gpkg", settings=None,
        threshold_flow_accumulation=500, snap_distance_px=15,
        fill_pits=False, keep_intermediate=True)
    assert captured["module"] == "invest_mcp.geo.hydro"
    assert captured["payload"] == {
        "dem_path": "dem.tif", "outlets_path": "pts.shp", "dst_path": "ws.gpkg",
        "threshold_flow_accumulation": 500, "snap_distance_px": 15,
        "fill_pits": False, "keep_intermediate": True,
    }
    json.dumps(captured["payload"])  # serialisable


def test_run_delineate_watersheds_defaults(captured):
    geo_client.run_delineate_watersheds("d.tif", "o.shp", "w.shp", settings=None)
    p = captured["payload"]
    assert p["threshold_flow_accumulation"] == 1000
    assert p["snap_distance_px"] == 10
    assert p["fill_pits"] is True and p["keep_intermediate"] is False
    assert captured["timeout"] == geo_client._HYDRO_TIMEOUT_S


# ---------------------------------------------------------------------------
# tool-level guards (sandbox + numeric validation) -- reached before any worker
# ---------------------------------------------------------------------------
def test_tool_rejects_dem_outside_allowed_roots(tmp_path):
    out = tools.delineate_watersheds(
        str(tmp_path / "nope_dem.tif"),          # doesn't exist / not allowed
        str(tmp_path / "pts.shp"),
        str(tmp_path / "ws.gpkg"))
    assert out["ok"] is False and "error" in out


def test_tool_rejects_bad_threshold(tmp_path, monkeypatch):
    dem = tmp_path / "DEM.tif"; dem.write_bytes(b"x")
    pts = tmp_path / "pts.shp"; pts.write_bytes(b"x")
    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    out = tools.delineate_watersheds(str(dem), str(pts), str(tmp_path / "ws.gpkg"),
                                     threshold_flow_accumulation=0)
    assert out["ok"] is False
    assert "threshold_flow_accumulation must be > 0" in out["error"]


def test_tool_rejects_negative_snap(tmp_path, monkeypatch):
    dem = tmp_path / "DEM.tif"; dem.write_bytes(b"x")
    pts = tmp_path / "pts.shp"; pts.write_bytes(b"x")
    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    out = tools.delineate_watersheds(str(dem), str(pts), str(tmp_path / "ws.gpkg"),
                                     snap_distance_px=-1)
    assert out["ok"] is False
    assert "snap_distance_px must be >= 0" in out["error"]
