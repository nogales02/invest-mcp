"""`clone_job`: the pure arg-diff helper + the tool's guard rails (no live
invest.exe — `_RUNNER.submit` is stubbed)."""

import json

from invest_mcp import tools


# ---------------------------------------------------------------------------
# _clone_args (pure)
# ---------------------------------------------------------------------------
def test_clone_args_override_sets_and_records_diff():
    base = {"lulc_path": "a.tif", "k_param": 2, "workspace_dir": "/ws"}
    new, diff = tools._clone_args(base, {"lulc_path": "b.tif"}, None)
    assert new == {"lulc_path": "b.tif", "k_param": 2}      # workspace_dir stripped
    assert diff == {"lulc_path": {"from": "a.tif", "to": "b.tif"}}


def test_clone_args_add_new_arg():
    new, diff = tools._clone_args({"lulc_path": "a.tif"}, {"lulc_alt_path": "s.tif"}, None)
    assert new["lulc_alt_path"] == "s.tif"
    assert diff == {"lulc_alt_path": {"from": None, "to": "s.tif"}}


def test_clone_args_drop_arg():
    new, diff = tools._clone_args({"lulc_path": "a.tif", "drainage_path": "d.tif"},
                                  None, ["drainage_path"])
    assert new == {"lulc_path": "a.tif"}
    assert diff == {"drainage_path": {"from": "d.tif", "to": None}}


def test_clone_args_noop_override_is_not_a_diff():
    new, diff = tools._clone_args({"k_param": 2}, {"k_param": 2}, None)
    assert new == {"k_param": 2} and diff == {}


def test_clone_args_drop_missing_and_workspace_override_ignored():
    new, diff = tools._clone_args({"k_param": 2}, {"workspace_dir": "/x"}, ["nope"])
    assert new == {"k_param": 2} and diff == {}


# ---------------------------------------------------------------------------
# clone_job tool — guard rails with a stubbed runner/store
# ---------------------------------------------------------------------------
class _FakeJob:
    def __init__(self, jid, status="succeeded", datastack_path=""):
        self.id = jid
        self.status = status
        self.datastack_path = datastack_path
        self.model_id = "carbon"

    def public_dict(self):
        return {"job_id": self.id, "status": self.status, "model_id": self.model_id}


def _install(monkeypatch, src_job, submit=None):
    monkeypatch.setattr(tools._STORE, "get", lambda jid: src_job if src_job and jid == src_job.id else None)
    if submit is not None:
        monkeypatch.setattr(tools._RUNNER, "submit", submit)


def test_clone_job_unknown_job(monkeypatch):
    _install(monkeypatch, None)
    out = tools.clone_job("nope", overrides={"a": 1})
    assert out["ok"] is False and "No such job" in out["error"]


def test_clone_job_missing_datastack(monkeypatch, tmp_path):
    src = _FakeJob("carbon-x", datastack_path=str(tmp_path / "gone.json"))
    _install(monkeypatch, src)
    out = tools.clone_job("carbon-x", overrides={"a": 1})
    assert out["ok"] is False and "cannot" in out["error"]


def test_clone_job_needs_a_change(monkeypatch, tmp_path):
    dsp = tmp_path / "datastack.json"
    dsp.write_text(json.dumps({"model_id": "carbon",
                               "args": {"lulc_bas_path": "a.tif", "workspace_dir": "/ws"}}))
    src = _FakeJob("carbon-x", datastack_path=str(dsp))
    _install(monkeypatch, src)
    out = tools.clone_job("carbon-x")
    assert out["ok"] is False and "identical" in out["error"]


def test_clone_job_submits_with_merged_args(monkeypatch, tmp_path):
    dsp = tmp_path / "datastack.json"
    dsp.write_text(json.dumps({
        "model_id": "carbon",
        "args": {"lulc_bas_path": "base.tif", "carbon_pools_path": "p.csv",
                 "workspace_dir": "/old/ws"},
    }))
    src = _FakeJob("carbon-base", status="succeeded", datastack_path=str(dsp))
    seen = {}

    def fake_submit(model_id, args):
        seen["model_id"] = model_id
        seen["args"] = args
        return _FakeJob("carbon-clone", status="queued")

    _install(monkeypatch, src, submit=fake_submit)
    out = tools.clone_job("carbon-base", overrides={"lulc_bas_path": "scenario.tif"})

    assert out["ok"] is True
    assert out["cloned_from"] == "carbon-base" and out["source_status"] == "succeeded"
    assert out["job_id"] == "carbon-clone"
    assert out["diff"] == {"lulc_bas_path": {"from": "base.tif", "to": "scenario.tif"}}
    assert out["arg_count"] == 2 and out["unchanged_arg_count"] == 1
    assert "compare_scenarios('carbon-base', 'carbon-clone'" in out["hint"]
    # runner got the merged args, no workspace_dir
    assert seen["model_id"] == "carbon"
    assert seen["args"] == {"lulc_bas_path": "scenario.tif", "carbon_pools_path": "p.csv"}


def test_clone_job_registered():
    assert "clone_job" in {fn.__name__ for fn in tools._TOOLS}
