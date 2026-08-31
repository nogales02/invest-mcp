"""The cited coefficient knowledge base: files parse, citations resolve, the
loader + resources are wired, and the headline numbers are physically sane."""

import json

import pytest

from invest_mcp import resources
from invest_mcp.knowledge import coefficients as kb


def _payload(name):
    return json.loads(kb.entry(name))


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------
def test_every_parameter_file_parses_and_has_the_core_keys():
    for name in kb.PARAMETERS:
        p = _payload(name)
        assert p["parameter"] == name
        assert p["models"] and p["invest_column"]
        assert p["definition"].strip()
        assert isinstance(p["records"], list) and p["records"]
        for r in p["records"]:
            assert r["id"] and r["source_key"] and r["cover"]
            assert r["confidence"] in {"high", "medium", "low"}
            assert "value" in r or "values" in r or "by_source" in r


def test_readme_and_sources_and_profile_resolve():
    assert kb.entry("readme").lstrip().startswith("#")
    src = json.loads(kb.entry("sources"))
    assert src["sources"] and all("citation" in s for s in src["sources"].values())
    prof = json.loads(kb.entry("moorabool_fs28"))
    assert prof["profile"] == "moorabool_fs28"
    assert prof["biophysical_table"]["rows"]


def test_unknown_entry_raises():
    with pytest.raises(kb.UnknownEntryError):
        kb.entry("not_a_real_table")


# ---------------------------------------------------------------------------
# citations
# ---------------------------------------------------------------------------
def test_every_source_key_used_is_defined_in_the_bibliography():
    known = set(json.loads(kb.entry("sources"))["sources"])
    used = set()
    for name in kb.PARAMETERS:
        used |= {r["source_key"] for r in _payload(name)["records"] if r.get("source_key")}
    used |= set(json.loads(kb.entry("moorabool_fs28")).get("sources_used", []))
    missing = used - known
    assert not missing, f"source keys used but not in sources.json: {sorted(missing)}"


def test_bibliography_entries_carry_context_and_verification():
    for key, s in json.loads(kb.entry("sources"))["sources"].items():
        assert s.get("context"), f"{key} has no context"
        assert s.get("verified"), f"{key} has no 'verified' note"
        assert s.get("scope"), f"{key} has no scope"


# ---------------------------------------------------------------------------
# physical sanity of the headline numbers
# ---------------------------------------------------------------------------
def test_usle_c_point_values_between_0_and_1():
    for r in _payload("usle_c")["records"]:
        v = r.get("value")
        if isinstance(v, (int, float)):
            assert 0.0 <= v <= 1.0


def test_curve_numbers_ordered_by_soil_group_and_in_range():
    for r in _payload("curve_number")["records"]:
        vals = r.get("values") or {}
        quad = [vals.get(k) for k in ("CN_A", "CN_B", "CN_C", "CN_D")]
        if all(isinstance(x, (int, float)) for x in quad):
            assert all(0 < x <= 100 for x in quad)
            assert quad == sorted(quad), f"{r['id']} CN not A<=B<=C<=D"


def test_ndr_efficiencies_are_fractions():
    for r in _payload("ndr_nutrient")["records"]:
        vals = r.get("values") or {}
        for k in ("eff_n", "eff_p", "proportion_subsurface_n"):
            if isinstance(vals.get(k), (int, float)):
                assert 0.0 <= vals[k] <= 1.0


def test_index_is_json_and_lists_all_parameters():
    idx = json.loads(kb.index())
    assert {p["parameter"] for p in idx["parameters"]} == set(kb.PARAMETERS)
    assert idx["source_key_count"] > 0
    for p in idx["parameters"]:
        assert p["record_count"] > 0


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------
def test_resources_register_wires_the_coefficient_uris():
    seen = []

    class FakeServer:
        def resource(self, uri, **kw):
            seen.append(uri)
            return lambda fn: fn

    resources.register(FakeServer())
    assert "invest://coefficients" in seen
    assert "invest://coefficients/{name}" in seen


def test_resource_entry_helper_returns_error_json_for_junk():
    out = json.loads(resources.coefficients_entry("bogus"))
    assert "error" in out and "carbon_pools" in out["known"]
