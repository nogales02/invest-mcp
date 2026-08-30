"""Payload building for the geo preflight -- pure logic, no GDAL needed."""

from invest_mcp.geo.client import build_payload
from invest_mcp.models import spec_translate as st

SDR_SPEC = {
    "model_id": "sdr",
    "different_projections_ok": False,
    "validate_spatial_overlap": True,
    "args": {
        "workspace_dir": {"type": "workspace", "required": True},
        "dem_path": {"type": "raster", "required": True, "projected": True,
                     "projection_units": "m", "about": "DEM"},
        "erosivity_path": {"type": "raster", "required": True, "projected": True, "about": "R"},
        "watersheds_path": {"type": "vector", "required": True, "projected": True, "about": "ws"},
        "biophysical_table_path": {"type": "csv", "required": True, "about": "table"},
        "threshold_flow_accumulation": {"type": "number", "required": True, "about": "tfa"},
    },
}


def test_spatial_arg_specs_excludes_csv_and_scalars():
    specs = st.spatial_arg_specs(SDR_SPEC)
    assert set(specs) == {"dem_path", "erosivity_path", "watersheds_path"}
    assert specs["dem_path"] == {"kind": "raster", "projected_required": True, "projection_units": "m"}
    assert specs["watersheds_path"]["kind"] == "vector"


def test_build_payload_only_includes_provided_paths():
    args = {"dem_path": "C:/d/dem.tif", "watersheds_path": "C:/d/ws.gpkg",
            "biophysical_table_path": "C:/d/t.csv", "threshold_flow_accumulation": 1000}
    payload = build_payload(SDR_SPEC, args)
    args_in = {i["arg"] for i in payload["spatial_inputs"]}
    assert args_in == {"dem_path", "watersheds_path"}
    assert payload["different_projections_ok"] is False
    assert payload["validate_spatial_overlap"] is True
    dem = next(i for i in payload["spatial_inputs"] if i["arg"] == "dem_path")
    assert dem["path"] == "C:/d/dem.tif" and dem["projected_required"] is True


def test_build_payload_empty_when_no_spatial_inputs():
    payload = build_payload(SDR_SPEC, {"threshold_flow_accumulation": 1000})
    assert payload["spatial_inputs"] == []
