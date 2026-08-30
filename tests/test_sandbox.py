import pytest

from invest_mcp.workspace import sandbox


def test_accepts_path_within_allowed_root(tmp_path):
    f = tmp_path / "lulc.tif"
    f.write_bytes(b"x")
    resolved = sandbox.resolve_input_path(str(f), [tmp_path])
    assert resolved == f.resolve()


def test_rejects_path_outside_allowed_roots(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.tif"
    outside.write_bytes(b"x")
    with pytest.raises(sandbox.SandboxError):
        sandbox.resolve_input_path(str(outside), [tmp_path])


def test_rejects_missing_path(tmp_path):
    with pytest.raises(sandbox.SandboxError):
        sandbox.resolve_input_path(str(tmp_path / "nope.tif"), [tmp_path])


def test_check_input_paths_only_validates_present_path_args(tmp_path):
    raster = tmp_path / "lulc.tif"
    raster.write_bytes(b"x")
    args = {"lulc_bas_path": str(raster), "price_per_metric_ton_of_c": 40}
    path_types = {"lulc_bas_path": "raster", "carbon_pools_path": "csv"}
    out = sandbox.check_input_paths(args, path_types, [tmp_path])
    assert set(out) == {"lulc_bas_path"}


def test_allow_dir_extends_session(tmp_path, tmp_path_factory):
    extra = tmp_path_factory.mktemp("extra")
    f = extra / "d.tif"
    f.write_bytes(b"x")
    sandbox.allow_dir(str(extra))
    assert sandbox.resolve_input_path(str(f), [tmp_path]) == f.resolve()
