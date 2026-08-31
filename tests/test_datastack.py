"""Datastack (.invest.json parameter set) read / write + the import/export tools."""

import json
from pathlib import Path

import pytest

from invest_mcp import tools
from invest_mcp.workspace import datastack as ds


# ---------------------------------------------------------------------------
# pure module
# ---------------------------------------------------------------------------
def test_model_id_from_raw_variants():
    assert ds.model_id_from_raw({"model_id": "carbon"}) == "carbon"
    assert ds.model_id_from_raw({"model_name": "natcap.invest.sdr"}) == "sdr"
    assert ds.model_id_from_raw({"model_name": "ndr"}) == "ndr"
    assert ds.model_id_from_raw({"args": {}}) == ""


def test_build_parameter_set_shape():
    ps = ds.build_parameter_set("carbon", {"lulc": "x.tif"})
    assert ps == {"args": {"lulc": "x.tif"}, "model_id": "carbon"}
    ps2 = ds.build_parameter_set("carbon", {}, invest_version="3.20.1")
    assert ps2["invest_version"] == "3.20.1"
    # build copies args
    src = {"a": 1}
    ds.build_parameter_set("m", src)["args"]["a"] = 99
    assert src["a"] == 1


def test_relativize_then_absolutize_roundtrip(tmp_path):
    base = tmp_path / "datastacks"
    base.mkdir()
    dem = tmp_path / "data" / "dem.tif"
    args = {"dem_path": str(dem), "n_workers": -1, "suffix": ""}
    rel, changed = ds.relativize_args(args, ["dem_path"], base)
    assert changed == ["dem_path"]
    assert rel["dem_path"] == "../data/dem.tif"
    back = ds.absolutize_args(rel, ["dem_path"], base)
    assert Path(back["dem_path"]) == dem


def test_relativize_skips_relative_and_non_string():
    args = {"a": "already/rel.tif", "b": 5, "c": ""}
    out, changed = ds.relativize_args(args, ["a", "b", "c"], "/somewhere")
    assert changed == [] and out == args


def test_read_parameter_set_ok(tmp_path):
    p = tmp_path / "s.invest.json"
    p.write_text(json.dumps({"model_id": "carbon", "args": {"k": 1},
                             "invest_version": "3.20.1"}))
    out = ds.read_parameter_set(p)
    assert out["model_id"] == "carbon" and out["args"] == {"k": 1}
    assert out["invest_version"] == "3.20.1"
    assert "args" in out["raw_keys"]


def test_read_parameter_set_rejects_non_paramset(tmp_path):
    p = tmp_path / "nope.json"
    p.write_text(json.dumps({"foo": "bar"}))
    with pytest.raises(ValueError, match="not an InVEST parameter set"):
        ds.read_parameter_set(p)


def test_read_parameter_set_rejects_bad_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        ds.read_parameter_set(p)


def test_write_then_read_roundtrip(tmp_path):
    dst = tmp_path / "out" / "carbon.invest.json"
    ds.write_parameter_set(dst, "carbon", {"lulc_cur_path": "x.tif"},
                           invest_version="3.20.1")
    assert dst.is_file()
    out = ds.read_parameter_set(dst)
    assert out["model_id"] == "carbon"
    assert out["args"] == {"lulc_cur_path": "x.tif"}


# ---------------------------------------------------------------------------
# tools -- guards that run before the model registry is touched
# ---------------------------------------------------------------------------
def test_export_requires_json_suffix():
    out = tools.export_datastack("stack.txt", model_id="carbon", args={"a": 1})
    assert out["ok"] is False and ".json" in out["error"]


def test_export_unknown_job_id():
    out = tools.export_datastack("stack.invest.json", job_id="does-not-exist")
    assert out["ok"] is False and "unknown job_id" in out["error"]


def test_export_needs_model_or_job():
    out = tools.export_datastack("stack.invest.json")
    assert out["ok"] is False and "model_id" in out["error"]


def test_import_rejects_path_outside_sandbox():
    out = tools.import_datastack("C:/Windows/Temp/whatever.invest.json")
    assert out["ok"] is False


# ---------------------------------------------------------------------------
# tools -- full round-trip with the registry stubbed
# ---------------------------------------------------------------------------
_FAKE_SPEC = {
    "model_title": "Carbon Storage and Sequestration",
    "args": {
        "workspace_dir": {"type": "directory", "required": True},
        "lulc_cur_path": {"type": "raster", "required": True},
        "carbon_pools_path": {"type": "csv", "required": True},
        "calc_sequestration": {"type": "boolean", "required": False},
    },
}


@pytest.fixture()
def stub_registry(monkeypatch):
    from invest_mcp.models import registry

    monkeypatch.setattr(registry, "resolve_model_id", lambda m: "carbon")
    monkeypatch.setattr(registry, "get_spec", lambda m: _FAKE_SPEC)
    monkeypatch.setattr(tools.invest_cli, "version", lambda: "InVEST 3.20.1")


def test_export_then_import_roundtrip(tmp_path, monkeypatch, stub_registry):
    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    lulc = tmp_path / "lulc.tif"
    lulc.write_bytes(b"\x00")
    pools = tmp_path / "pools.csv"
    pools.write_text("lucode,c_above\n1,10\n")

    dst = tmp_path / "stacks" / "carbon.invest.json"
    exp = tools.export_datastack(
        str(dst), model_id="carbon",
        args={"lulc_cur_path": str(lulc), "carbon_pools_path": str(pools),
              "workspace_dir": "ignored"})
    assert exp["ok"] is True and Path(exp["path"]).is_file()
    assert exp["model_id"] == "carbon" and exp["arg_count"] == 2  # workspace_dir dropped
    assert {p["arg"] for p in exp["path_args"]} == {"lulc_cur_path", "carbon_pools_path"}
    assert all(p["exists"] for p in exp["path_args"])

    imp = tools.import_datastack(str(dst))
    assert imp["ok"] is True
    assert imp["model_id"] == "carbon"
    assert imp["args"]["lulc_cur_path"] == str(lulc)
    assert imp["required_missing"] == []
    assert imp["files_missing"] == []
    assert imp["ready"] is True


def test_import_flags_missing_files_and_required(tmp_path, monkeypatch, stub_registry):
    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    dst = tmp_path / "s.invest.json"
    dst.write_text(json.dumps({
        "model_id": "carbon",
        "args": {"lulc_cur_path": str(tmp_path / "gone.tif")},
    }))
    imp = tools.import_datastack(str(dst))
    assert imp["ok"] is True
    assert "carbon_pools_path" in imp["required_missing"]
    assert "lulc_cur_path" in imp["files_missing"]
    assert imp["ready"] is False


def test_export_relative_rewrites_paths(tmp_path, monkeypatch, stub_registry):
    monkeypatch.setattr(tools._SETTINGS, "allowed_input_dirs", [tmp_path])
    lulc = tmp_path / "data" / "lulc.tif"
    lulc.parent.mkdir()
    lulc.write_bytes(b"\x00")
    dst = tmp_path / "stacks" / "c.invest.json"
    exp = tools.export_datastack(str(dst), model_id="carbon",
                                 args={"lulc_cur_path": str(lulc)}, relative=True)
    assert exp["ok"] is True and exp["made_relative"] == ["lulc_cur_path"]
    on_disk = json.loads(Path(exp["path"]).read_text())
    assert on_disk["args"]["lulc_cur_path"] == "../data/lulc.tif"
