"""The resampling decision -- pure logic, no GDAL. The gdal.Warp path
(``warp_raster`` / ``classify_raster`` / ``raster_header``) runs in invest-geo
and is covered by the end-to-end prep run, not here.
"""

import pytest

from invest_mcp.geo import resampling as rs


# ---------------------------------------------------------------------------
# normalize_method
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,expected", [
    ("nearest", "near"), ("near", "near"),
    ("bilinear", "bilinear"), ("linear", "bilinear"),
    ("cubic_spline", "cubicspline"), ("cubicspline", "cubicspline"),
    ("average", "average"), ("mean", "average"),
    ("mode", "mode"), ("majority", "mode"),
    ("median", "med"), ("q1", "q1"),
    ("NEAREST", "near"), ("  Mode ", "mode"),
])
def test_normalize_method(name, expected):
    assert rs.normalize_method(name) == expected


def test_normalize_method_rejects_unknown():
    with pytest.raises(ValueError):
        rs.normalize_method("wibble")


# ---------------------------------------------------------------------------
# scale_direction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("src,dst,expected", [
    (10, 90, "down"),
    (250, 30, "up"),
    (10, 12, "same"),
    (30, 30, "same"),
    (10, 14, "same"),
    (10, 15, "down"),      # exactly at the 1.5x ratio counts as aggregating
    (10, 6, "up"),
    (None, 30, "unknown"),
    (30, 0, "unknown"),
])
def test_scale_direction(src, dst, expected):
    assert rs.scale_direction(src, dst) == expected


# ---------------------------------------------------------------------------
# choose_resampling -- the matrix
# ---------------------------------------------------------------------------
def test_categorical_downsample_is_mode():
    method, note = rs.choose_resampling(categorical=True, src_res_m=10, dst_res_m=90)
    assert method == "mode" and note is None


def test_categorical_upsample_or_same_is_nearest():
    assert rs.choose_resampling(categorical=True, src_res_m=90, dst_res_m=10)[0] == "near"
    assert rs.choose_resampling(categorical=True, src_res_m=30, dst_res_m=30)[0] == "near"


def test_continuous_downsample_is_average():
    method, _ = rs.choose_resampling(categorical=False, src_res_m=30, dst_res_m=300)
    assert method == "average"


def test_continuous_upsample_is_bilinear_with_a_no_detail_note():
    method, note = rs.choose_resampling(categorical=False, src_res_m=250, dst_res_m=30)
    assert method == "bilinear"
    assert note and "cannot add real detail" in note


def test_unknown_scale_falls_back_to_same_side_with_a_note():
    method, note = rs.choose_resampling(categorical=True)
    assert method == "near"
    assert note and "resolution unknown" in note
    assert rs.choose_resampling(categorical=False)[0] == "bilinear"


# ---------------------------------------------------------------------------
# choose_resampling -- explicit override is honoured but sanity-checked
# ---------------------------------------------------------------------------
def test_explicit_method_is_returned_verbatim():
    assert rs.choose_resampling(categorical=False, explicit="cubic")[0] == "cubic"
    assert rs.choose_resampling(categorical=True, explicit="nearest")[0] == "near"


def test_explicit_auto_is_treated_as_unset():
    assert rs.choose_resampling(categorical=True, src_res_m=10, dst_res_m=90,
                                explicit="auto")[0] == "mode"


def test_explicit_interpolation_on_categorical_warns():
    method, note = rs.choose_resampling(categorical=True, explicit="bilinear")
    assert method == "bilinear"
    assert note and "categorical" in note


def test_explicit_nearest_downsampling_classes_warns():
    method, note = rs.choose_resampling(categorical=True, src_res_m=10,
                                        dst_res_m=90, explicit="nearest")
    assert method == "near"
    assert note and "majority class" in note


def test_explicit_bilinear_downsampling_continuous_warns():
    method, note = rs.choose_resampling(categorical=False, src_res_m=30,
                                        dst_res_m=300, explicit="bilinear")
    assert method == "bilinear"
    assert note and "areal mean" in note


def test_matrix_covers_every_kind_direction_combo():
    for kind in ("categorical", "continuous"):
        for direction in ("up", "same", "down"):
            assert (kind, direction) in rs.RESAMPLING_MATRIX
