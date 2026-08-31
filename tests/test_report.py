"""`build_report`: the pure Markdown renderer + the tool's collation and guard
rails (no live invest.exe — the registry + output sandbox are stubbed)."""

import json
from pathlib import Path

from invest_mcp import tools
from invest_mcp.workspace import report


# ---------------------------------------------------------------------------
# pure renderer
# ---------------------------------------------------------------------------
def _mini(**extra):
    job = {"job_id": "carbon-1", "model_id": "carbon",
           "model_title": "Carbon Storage and Sequestration",
           "status": "succeeded", "returncode": 0,
           "started_at": "t0", "ended_at": "t1", "workspace": "/ws"}
    job.update(extra)
    return {"title": "Demo", "generated_utc": "2026-08-31T00:00:00",
            "invest_mcp_version": "0.1.0", "jobs": [job], "comparisons": []}


def test_render_minimal_has_header_overview_footer():
    md = report.render(_mini())
    assert md.startswith("# Demo\n")
    assert "_generated 2026-08-31T00:00:00 · invest-mcp 0.1.0 · 1 job(s)_" in md
    assert "## Overview" in md
    assert "| `carbon-1` | `carbon` | succeeded |" in md
    assert "## Carbon Storage and Sequestration — `carbon-1`" in md
    assert md.rstrip().endswith("._")


def test_render_omits_absent_sections():
    md = report.render(_mini())
    assert "### Parameters" not in md
    assert "### Provenance" not in md
    assert "### Results" not in md


def test_render_parameters_and_provenance():
    md = report.render(_mini(
        args={"lulc_bas_path": "/d/l.tif", "k_param": 2},
        provenance={"invest_version": "3.20.1", "python": "3.14",
                    "inputs": [{"arg": "lulc_bas_path", "path": "/d/l.tif",
                                "sha256": "a" * 64}]},
    ))
    assert "### Parameters" in md and "`lulc_bas_path`" in md and "`/d/l.tif`" in md
    assert "### Provenance" in md and "InVEST **3.20.1**" in md
    assert "`" + "a" * 16 + "…`" in md          # digest truncated


def test_render_results_table_and_invest_csv():
    md = report.render(_mini(summary={
        "rasters": [{"name": "c.tif", "label": "Carbon", "units": "t/ha",
                     "stats": {"valid_count": 1234, "mean": 50.5, "min": 0,
                               "max": 100, "sum": 62_300}}],
        "aoi": {"path": "/d/ws.shp", "feature_count": 1,
                "features": [{"feature_index": 0, "properties": {"id": 1},
                              "rasters": {"c.tif": {"valid_count": 1000,
                                                    "mean": 49.0}}}]},
        "invest_raster_values_summary": [{"Raster": "Total", "Total": "62300",
                                          "Units": "Mg"}],
    }))
    assert "### Results" in md
    assert "| `c.tif` | Carbon | 1,234 | 50.5 | 0 – 100 | 62,300 | t/ha |" in md
    assert "AOI zonal summary" in md and "feature 0 (id=1)" in md
    assert "raster_values_summary" in md and "| Total | 62300 | Mg |" in md


def test_render_comparison_section():
    pair = {
        "relpath": "out/c.tif",
        "stats": {"baseline_sum": 1000, "scenario_sum": 900, "delta_sum": -100,
                  "pct_change": -10.0, "increased_px": 0, "decreased_px": 50,
                  "unchanged_px": 950},
    }
    cmp = {
        "baseline_job_id": "carbon-1",
        "scenario_job_id": "carbon-2",
        "compare": {"pairs": [pair], "aoi": None},
        "figure": {"ref": "diff.png", "caption": "scenario − baseline"},
    }
    payload = _mini()
    payload["comparisons"] = [cmp]
    md = report.render(payload)
    assert "## Scenario comparison — baseline `carbon-1` vs scenario `carbon-2`" in md
    assert "| `c.tif` | 1,000 | 900 | -100 | -10.0% | 0 | 50 | 950 |" in md
    assert "![scenario − baseline](diff.png)" in md


def test_render_failed_job_gets_log_tail():
    md = report.render(_mini(status="failed", returncode=1,
                             log_tail="Traceback...\nValueError: boom"))
    assert "### Run log (tail)" in md and "ValueError: boom" in md


def test_num_and_bytes_formatting():
    assert report._num(None) == "n/a"
    assert report._num(0) == "0"
    assert report._num(1234.5) == "1,234.5"
    assert "e" in report._num(0.000001) or "0" in report._num(0.000001)
    assert report._bytes(0) == "0 B"
    assert report._bytes(2048) == "2.0 KB"
    assert report._bytes(-1) == "?"


def test_table_escapes_pipes():
    t = report._table(["a"], [["x|y"]])
    assert r"x\|y" in t


# ---------------------------------------------------------------------------
# build_report tool
# ---------------------------------------------------------------------------
class _FakeJob:
    def __init__(self, d: Path, jid="carbon-t", status="succeeded"):
        self.id = jid
        self.model_id = "carbon"
        self.status = status
        self.returncode = 0 if status == "succeeded" else 1
        self.created_at = self.started_at = "2026-01-01T00:00:00+00:00"
        self.ended_at = "2026-01-01T00:00:05+00:00"
        self.workspace = str(d / "workspace")
        self.datastack_path = str(d / "datastack.json")
        self.provenance_path = str(d / "provenance.json")
        self.log_path = str(d / "run.log")


def _make_job_dir(d: Path, *, with_summary=True):
    (d / "workspace").mkdir(parents=True)
    (d / "workspace" / "c_storage.tif").write_bytes(b"x" * 10)
    (d / "workspace" / "raster_values_summary.csv").write_text(
        "Raster,Total,Units\nBaseline,5,Mg\n", encoding="utf-8")
    (d / "datastack.json").write_text(json.dumps(
        {"model_id": "carbon",
         "args": {"lulc_bas_path": "/d/l.tif", "workspace_dir": "/ws"}}))
    (d / "provenance.json").write_text(json.dumps(
        {"invest_version": "3.20.1", "invest_mcp_version": "0.1.0",
         "python": "3.14", "platform": "win",
         "inputs": [{"arg": "lulc_bas_path", "path": "/d/l.tif", "sha256": "a" * 64}]}))
    (d / "run.log").write_text("log line\n")
    if with_summary:
        (d / "summary").mkdir()
        (d / "summary" / "summary.json").write_text(json.dumps(
            {"rasters": [{"name": "c_storage.tif", "label": "Storage",
                          "units": "t/ha",
                          "stats": {"valid_count": 100, "mean": 10, "min": 1,
                                    "max": 20, "sum": 1000}}],
             "aoi": None, "preview": None}))


def _wire(monkeypatch, job):
    monkeypatch.setattr(tools._STORE, "get",
                        lambda i: job if i == job.id else None)
    monkeypatch.setattr(tools.registry, "resolve_model_id", lambda m: m)
    monkeypatch.setattr(tools.registry, "get_spec",
                        lambda *a, **k: {"model_title": "Carbon Storage and Sequestration"})
    monkeypatch.setattr(tools, "resolve_output_path", lambda p, roots: Path(p))


def test_build_report_writes_markdown(tmp_path, monkeypatch):
    jd = tmp_path / "carbon-t"
    _make_job_dir(jd)
    _wire(monkeypatch, _FakeJob(jd))

    dst = tmp_path / "report.md"
    out = tools.build_report("carbon-t", str(dst))
    assert out["ok"] is True and Path(out["path"]) == dst and dst.is_file()
    md = dst.read_text(encoding="utf-8")
    assert "# InVEST report — Carbon Storage and Sequestration" in md
    assert "### Parameters" in md and "lulc_bas_path" in md
    assert "workspace_dir" not in md                      # stripped
    assert "### Provenance" in md and "3.20.1" in md
    assert "### Results" in md and "Storage" in md
    assert "### Outputs" in md
    assert out["jobs"] == ["carbon-t"] and out["notes"] == []


def test_build_report_toggles_off_sections(tmp_path, monkeypatch):
    jd = tmp_path / "carbon-t"
    _make_job_dir(jd)
    _wire(monkeypatch, _FakeJob(jd))
    dst = tmp_path / "r.md"
    tools.build_report("carbon-t", str(dst), include_args=False,
                       include_provenance=False, include_artifacts=False)
    md = dst.read_text(encoding="utf-8")
    assert "### Parameters" not in md
    assert "### Provenance" not in md
    assert "### Outputs" not in md
    assert "### Results" in md                            # summary still there


def test_read_invest_summary_csv_plain_and_suffixed(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    # no file yet
    assert tools._read_invest_summary_csv(str(ws)) is None
    # a results_suffix run only writes the suffixed name
    (ws / "raster_values_summary_baseline.csv").write_text(
        "Raster,Total,Units\nBaseline carbon storage,4061555.98,Mg\n",
        encoding="utf-8")
    rows = tools._read_invest_summary_csv(str(ws))
    assert rows and rows[0]["Total"] == "4061555.98"
    # the un-suffixed name still wins when both are present
    (ws / "raster_values_summary.csv").write_text(
        "Raster,Total,Units\nplain,1,Mg\n", encoding="utf-8")
    assert tools._read_invest_summary_csv(str(ws))[0]["Raster"] == "plain"


def test_build_report_guard_rails(tmp_path, monkeypatch):
    monkeypatch.setattr(tools._STORE, "get", lambda i: None)
    monkeypatch.setattr(tools, "resolve_output_path", lambda p, roots: Path(p))
    assert tools.build_report([], str(tmp_path / "r.md"))["ok"] is False
    assert "must end in .md" in tools.build_report("j", str(tmp_path / "r.txt"))["error"]
    assert "No such job" in tools.build_report("ghost", str(tmp_path / "r.md"))["error"]


def test_build_report_registered():
    assert "build_report" in {fn.__name__ for fn in tools._TOOLS}
