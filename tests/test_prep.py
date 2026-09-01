"""Data-prep -- project scaffold, the write-sandbox, and payload building.

The GDAL-backed reproject / clip / align_stack ops run in the invest-geo env and
are covered by the end-to-end run, not here.
"""

import json

import pytest

from invest_mcp.geo import client as geo_client
from invest_mcp.geo import prep as prep_worker
from invest_mcp.workspace import project, sandbox


# ---------------------------------------------------------------------------
# project scaffold
# ---------------------------------------------------------------------------
def test_scaffold_creates_tree_and_manifest(tmp_path):
    root = tmp_path / "caso_rio"
    info = project.scaffold(root, name="Caso Rio", target_crs="EPSG:32618",
                            aoi_path=None)

    assert not info["manifest_existed"]
    for sub in project.SUBDIRS:
        assert (root / sub).is_dir()
    assert set(info["created_dirs"]) == set(project.SUBDIRS)

    on_disk = json.loads((root / "project.json").read_text(encoding="utf-8"))
    assert on_disk == info["manifest"]
    assert on_disk["name"] == "Caso Rio"
    assert on_disk["target_crs"] == "EPSG:32618"
    assert on_disk["schema_version"] == project.SCHEMA_VERSION
    assert on_disk["datasets"] == []
    assert on_disk["aoi"] is None


def test_scaffold_is_idempotent_and_keeps_manifest(tmp_path):
    root = tmp_path / "p"
    first = project.scaffold(root, name="one", target_crs=None, aoi_path=None)
    second = project.scaffold(root, name="two", target_crs=None, aoi_path=None)

    assert second["manifest_existed"] is True
    assert second["created_dirs"] == []          # nothing new to make
    assert second["manifest"]["name"] == "one"   # not overwritten
    assert second["manifest"] == first["manifest"]


def test_scaffold_overwrite_rewrites_manifest(tmp_path):
    root = tmp_path / "p"
    project.scaffold(root, name="one", target_crs=None, aoi_path=None)
    out = project.scaffold(root, name="two", target_crs="EPSG:4326",
                           aoi_path=None, overwrite=True)
    assert out["manifest_existed"] is False
    assert json.loads((root / "project.json").read_text())["name"] == "two"


def test_scaffold_records_aoi_relpath_when_inside_project(tmp_path):
    root = tmp_path / "p"
    (root / "data" / "raw").mkdir(parents=True)
    aoi = root / "data" / "raw" / "aoi.shp"
    aoi.write_bytes(b"x")
    out = project.scaffold(root, name="p", target_crs=None, aoi_path=str(aoi))
    assert out["manifest"]["aoi"]["relpath"] == "data/raw/aoi.shp"
    assert out["manifest"]["aoi"]["path"] == str(aoi)


def test_scaffold_aoi_outside_project_has_no_relpath(tmp_path):
    root = tmp_path / "p"
    aoi = tmp_path / "elsewhere" / "aoi.shp"
    aoi.parent.mkdir(parents=True)
    aoi.write_bytes(b"x")
    out = project.scaffold(root, name="p", target_crs=None, aoi_path=str(aoi))
    assert out["manifest"]["aoi"]["relpath"] is None


def test_load_returns_none_without_manifest(tmp_path):
    assert project.load(tmp_path) is None
    project.scaffold(tmp_path / "p", name="p", target_crs=None, aoi_path=None)
    assert project.load(tmp_path / "p")["name"] == "p"


# ---------------------------------------------------------------------------
# write sandbox
# ---------------------------------------------------------------------------
def test_resolve_output_path_accepts_new_file_under_allowed_root(tmp_path):
    dst = tmp_path / "data" / "processed" / "dem_utm.tif"   # parent doesn't exist yet
    assert sandbox.resolve_output_path(str(dst), [tmp_path]) == dst.resolve()


def test_resolve_output_path_rejects_outside_allowed_roots(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "x.tif"
    with pytest.raises(sandbox.SandboxError):
        sandbox.resolve_output_path(str(outside), [tmp_path])


def test_resolve_output_path_rejects_existing_directory(tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(sandbox.SandboxError):
        sandbox.resolve_output_path(str(d), [tmp_path])


def test_resolve_output_path_overwrite_guard(tmp_path):
    f = tmp_path / "out.tif"
    f.write_bytes(b"x")
    assert sandbox.resolve_output_path(str(f), [tmp_path]) == f.resolve()  # default ok
    with pytest.raises(sandbox.SandboxError):
        sandbox.resolve_output_path(str(f), [tmp_path], allow_overwrite=False)


# ---------------------------------------------------------------------------
# payload building (worker not invoked -- _run_prep is stubbed)
# ---------------------------------------------------------------------------
@pytest.fixture()
def captured_payload(monkeypatch):
    box = {}

    def fake_run_prep(payload, settings):
        box["payload"] = payload
        box["settings"] = settings
        return {"ok": True, "op": payload["op"], "outputs": [], "notes": []}

    monkeypatch.setattr(geo_client, "_run_prep", fake_run_prep)
    return box


def test_run_reproject_builds_payload(captured_payload):
    geo_client.run_reproject("a.tif", "b.tif", "EPSG:32618", settings=None,
                             resampling="bilinear", resolution=[30.0, 30.0])
    assert captured_payload["payload"] == {
        "op": "reproject", "src": "a.tif", "dst": "b.tif",
        "target_crs": "EPSG:32618", "resampling": "bilinear",
        "resolution": [30.0, 30.0], "kind": "auto",
    }


def test_run_clip_builds_payload(captured_payload):
    geo_client.run_clip("a.tif", "b.tif", "aoi.shp", settings=None, all_touched=True)
    assert captured_payload["payload"] == {
        "op": "clip", "src": "a.tif", "dst": "b.tif", "aoi": "aoi.shp",
        "kind": "auto", "all_touched": True,
    }


def test_run_align_stack_builds_payload_with_reference(captured_payload):
    rasters = [{"src": "a.tif", "dst": "a_al.tif"}, {"src": "b.tif", "dst": "b_al.tif"}]
    geo_client.run_align_stack(rasters, settings=None, reference="ref.tif")
    p = captured_payload["payload"]
    assert p["op"] == "align_stack"
    assert p["rasters"] == rasters
    assert p["reference"] == "ref.tif"
    assert p["target_crs"] is None and p["extent"] is None


def test_prep_payload_is_json_serialisable(captured_payload):
    geo_client.run_align_stack([{"src": "a.tif", "dst": "o.tif"}], settings=None,
                               target_crs="EPSG:4326", resolution=[0.001, 0.001],
                               extent=[0, 0, 1, 1])
    json.dumps(captured_payload["payload"])  # must not raise


def test_run_raster_classes_builds_payload(captured_payload):
    geo_client.run_raster_classes("lulc.tif", settings=None, max_classes=50)
    assert captured_payload["payload"] == {
        "op": "raster_classes", "src": "lulc.tif", "max_classes": 50,
    }


def test_run_align_stack_defaults_to_auto_resampling(captured_payload):
    geo_client.run_align_stack([{"src": "a.tif", "dst": "o.tif"}], settings=None,
                               reference="ref.tif")
    assert captured_payload["payload"]["resampling"] == "auto"


def test_run_resample_builds_payload(captured_payload):
    geo_client.run_resample("a.tif", "b.tif", settings=None,
                            target_resolution=[30.0, 30.0], categorical=True)
    assert captured_payload["payload"] == {
        "op": "resample", "src": "a.tif", "dst": "b.tif",
        "target_resolution": [30.0, 30.0], "reference": None,
        "target_crs": None, "method": "auto", "categorical": True,
    }


def test_run_plan_grid_builds_payload(monkeypatch):
    box = {}
    monkeypatch.setattr(geo_client, "_run_geo_worker",
                        lambda module, payload, settings, **kw: box.setdefault("p", payload)
                        or {"ok": True})
    geo_client.run_plan_grid(["a.tif", "b.tif"], settings=None, target_crs="EPSG:4326")
    assert box["p"] == {"op": "plan_grid", "rasters": ["a.tif", "b.tif"],
                        "reference": None, "target_crs": "EPSG:4326"}


# ---------------------------------------------------------------------------
# prep worker op routing (pure -- no GDAL needed for the error paths)
# ---------------------------------------------------------------------------
def test_prep_unknown_op_is_a_clean_error():
    out = prep_worker.run({"op": "bogus"})
    assert out["ok"] is False and "unknown op" in out["error"]
    assert "resample" in out["error"] and "plan_grid" in out["error"]


def test_prep_resample_without_target_or_reference_errors():
    with pytest.raises(ValueError, match="target_resolution"):
        prep_worker._op_resample({"src": "a.tif", "dst": "b.tif"})


def test_prep_align_stack_without_grid_errors():
    with pytest.raises(ValueError, match="reference"):
        prep_worker._op_align_stack({"rasters": [{"src": "a.tif", "dst": "b.tif"}]})


def test_prep_plan_grid_requires_rasters():
    with pytest.raises(ValueError, match="non-empty"):
        prep_worker._op_plan_grid({"rasters": []})


# ---------------------------------------------------------------------------
# resample_raster / plan_grid tool guards
# ---------------------------------------------------------------------------
def test_resample_raster_needs_a_target(tmp_path, monkeypatch):
    from invest_mcp import tools

    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    src = tmp_path / "a.tif"
    src.write_bytes(b"x")
    out = tools.resample_raster(str(src), str(tmp_path / "b.tif"))
    assert out["ok"] is False and "target_resolution" in out["error"]


def test_resample_raster_passes_through_and_reports_env_missing(tmp_path, monkeypatch):
    from invest_mcp import tools

    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    src = tmp_path / "a.tif"
    src.write_bytes(b"x")
    box = {}

    def fake(src_, dst_, settings, **kw):
        box.update(kw)
        raise RuntimeError("invest-geo python not found")

    monkeypatch.setattr(geo_client, "run_resample", fake)
    out = tools.resample_raster(str(src), str(tmp_path / "b.tif"),
                                target_resolution=[90.0, 90.0], method="mode",
                                categorical=True)
    assert out["ok"] is False and out["env_missing"] is True
    assert box["target_resolution"] == [90.0, 90.0]
    assert box["method"] == "mode" and box["categorical"] is True


def test_plan_grid_rejects_empty_and_resolves_paths(tmp_path, monkeypatch):
    from invest_mcp import tools

    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    assert tools.plan_grid([])["ok"] is False

    r1, r2 = tmp_path / "lulc.tif", tmp_path / "dem.tif"
    r1.write_bytes(b"x")
    r2.write_bytes(b"x")
    seen = {}

    def fake_plan(srcs, settings, **kw):
        seen["srcs"] = srcs
        return {"ok": True, "op": "plan_grid", "outputs": [{"layers": []}], "notes": []}

    monkeypatch.setattr(geo_client, "run_plan_grid", fake_plan)
    out = tools.plan_grid([str(r1), str(r2)])
    assert out["ok"] is True
    assert seen["srcs"] == [str(r1.resolve()), str(r2.resolve())]
