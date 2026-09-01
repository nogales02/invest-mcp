"""MCP resources / prompts -- the static ones render, playbooks fill placeholders.

`invest://models` and the model cheat-sheet shell out to invest.exe and are
covered by the live server, not here.
"""

from invest_mcp import prompts, resources
from invest_mcp.workspace import project


def test_project_conventions_lists_every_subdir():
    md = resources.project_conventions()
    assert md.strip()
    for sub in project.SUBDIRS:
        assert f"`{sub}/`" in md


def test_data_sources_catalog_is_nonempty_markdown():
    md = resources.data_sources()
    assert md.lstrip().startswith("#")
    for heading in ("Elevation", "Land cover", "Soil", "Hydrography"):
        assert heading in md


def test_model_guide_is_markdown_and_covers_the_installed_models():
    md = resources.model_guide()
    assert md.lstrip().startswith("#")
    for mid in ("annual_water_yield", "seasonal_water_yield", "sdr", "ndr",
                "carbon", "habitat_quality", "urban_cooling_model",
                "coastal_vulnerability", "pollination"):
        assert f"`{mid}`" in md, mid
    for section in ("answers:", "needs:", "gives:", "Outside InVEST's scope"):
        assert section in md


def test_resampling_guide_covers_the_decision_matrix():
    md = resources.resampling_guide()
    assert md.lstrip().startswith("#")
    for term in ("Categorical", "Continuous", "`mode`", "`average`", "`nearest`",
                 "`bilinear`", "analysis grid", "plan_grid", "align_raster_stack"):
        assert term in md, term


def test_recommend_model_playbook_renders_and_points_at_the_reference():
    out = prompts.recommend_model("where is erosion worst in my basin?",
                                  project_root="C:/proj/x")
    assert "{question}" not in out and "{project_root}" not in out
    assert "where is erosion worst" in out
    assert "`C:/proj/x`" in out
    assert "invest://model-guide" in out
    assert "list_invest_models" in out and "project_readiness" in out

    bare = prompts.recommend_model()
    assert "{question}" not in bare and "no project folder" in bare


def test_prepare_and_run_playbook_substitutes_model_and_root():
    out = prompts.prepare_and_run_model("ndr", project_root="C:/proj/x")
    assert "{model_id}" not in out and "{project_root}" not in out
    assert "**ndr**" in out
    assert "`C:/proj/x`" in out
    assert "validate_invest_args" in out and "summarize_results" in out


def test_compare_scenarios_playbook_defaults_render():
    out = prompts.compare_land_use_scenarios()
    assert "{model_id}" not in out
    assert "compare_scenarios(" in out
    assert "align_raster_stack" in out


def test_fill_biophysical_table_playbook_substitutes_and_points_at_the_kb():
    out = prompts.fill_biophysical_table("ndr", project_root="C:/proj/x")
    assert "{model_id}" not in out and "{project_root}" not in out
    assert "**ndr**" in out and "`C:/proj/x`" in out
    assert "tables_from_template" in out
    assert "invest://coefficients" in out
    assert "check_table_vs_raster" in out


def test_prompt_register_is_wired(monkeypatch):
    seen = []

    class FakeServer:
        def prompt(self, **kw):
            seen.append(kw.get("name"))
            return lambda fn: fn

    prompts.register(FakeServer())
    assert set(seen) == {
        "prepare_and_run_model",
        "compare_land_use_scenarios",
        "fill_biophysical_table",
        "recommend_model",
    }


def test_resource_register_is_wired():
    seen = []

    class FakeServer:
        def resource(self, uri, **kw):
            seen.append(uri)
            return lambda fn: fn

    resources.register(FakeServer())
    assert "invest://models" in seen
    assert "invest://model/{model_id}/cheatsheet" in seen
    assert "invest://data-sources" in seen
    assert "invest://model-guide" in seen
