"""Calibration tool surface — checks that run before the invest-cal subprocess
(sandbox rejection, tool registration). The engine itself is tested in the
invest_calibration_assistant repo."""

from invest_mcp import tools


def test_calibration_tools_registered():
    names = {fn.__name__ for fn in tools._TOOLS}
    assert {"validate_calibration_config", "run_calibration",
            "get_calibration_job", "cancel_calibration_job"} <= names


def test_run_calibration_rejects_out_of_sandbox_paths(tmp_path):
    outside = tmp_path / "obs.csv"       # tmp_path is not an allowed root
    outside.write_text("ws_id,SDR\n1,100\n")
    out = tools.run_calibration(
        model="SDR",
        parameters={"sdr_max": {"min": 0.1, "max": 0.9}},
        objective="RMSE",
        optimizer={"method": "LHS", "n_simulations": 10},
        observed_data_path=str(outside),
        model_inputs={"lulc_path": str(outside)},
    )
    assert out["ok"] is False
    assert out["sandbox_issues"]
    assert any(i["field"] == "observed_data_path" for i in out["sandbox_issues"])


def test_get_calibration_job_unknown_id():
    assert tools.get_calibration_job("nope-123")["ok"] is False
