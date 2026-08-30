"""Server-side driver for calibration jobs.

Runs in the MCP server process (no natcap.invest / spotpy here). Builds the
config, shells out to :mod:`invest_mcp.calibration.worker` in the ``invest-cal``
conda env, streams per-iteration progress to ``<jobdir>/progress.jsonl`` and the
worker's stdout/stderr to the job log, and parses
``<workspace>/calibration_result.json`` when it finishes.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from invest_mcp.config import Settings, geo_subprocess_env
from invest_mcp.execution.jobs import TERMINAL, Job, JobStore

_VALIDATE_TIMEOUT_S = 120


def _now() -> str:
    from invest_mcp.execution.jobs import _now as jn

    return jn()


class CalibrationRunner:
    def __init__(self, settings: Settings, store: JobStore):
        self._settings = settings
        self._store = store
        self._procs: dict[str, subprocess.Popen] = {}
        self._done: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def validate(self, config: dict) -> dict:
        cal_python = self._settings.resolved_cal_python  # RuntimeError if absent
        proc = subprocess.run(
            [str(cal_python), "-m", "invest_mcp.calibration.worker"],
            input=json.dumps({"action": "validate", "config": config}),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=geo_subprocess_env(cal_python), timeout=_VALIDATE_TIMEOUT_S,
        )
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"ok": False, "error": (proc.stderr or proc.stdout)[-2000:]}

    # ------------------------------------------------------------------
    def submit(self, config: dict) -> Job:
        cal_python = self._settings.resolved_cal_python
        model = str(config.get("model", "?")).upper()
        job = self._store.new_job(f"calibrate-{model}")

        # workspace is the job's own workspace dir; the request replaces the datastack
        config = {**config, "workspace_dir": job.workspace}
        Path(job.datastack_path).write_text(json.dumps(config, indent=2), encoding="utf-8")
        progress_path = str(Path(job.datastack_path).parent / "progress.jsonl")
        Path(progress_path).write_text("", encoding="utf-8")

        with self._lock:
            self._done[job.id] = threading.Event()
        t = threading.Thread(
            target=self._supervise, args=(job, cal_python, config, progress_path), daemon=True
        )
        t.start()
        return job

    # ------------------------------------------------------------------
    def _supervise(self, job: Job, cal_python: Path, config: dict, progress_path: str) -> None:
        request = {"action": "calibrate", "config": config, "progress_path": progress_path}
        try:
            job.status = "running"
            job.started_at = _now()
            self._store.save(job)

            with open(job.log_path, "w", encoding="utf-8", errors="replace") as log:
                log.write(f"# calibrate {job.model_id}\n\n")
                log.flush()
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                proc = subprocess.Popen(
                    [str(cal_python), "-m", "invest_mcp.calibration.worker"],
                    stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    env=geo_subprocess_env(cal_python), creationflags=creationflags,
                )
                with self._lock:
                    self._procs[job.id] = proc
                job.pid = proc.pid
                self._store.save(job)
                try:
                    proc.communicate(json.dumps(request),
                                     timeout=self._settings.invest_timeout_seconds)
                except subprocess.TimeoutExpired:
                    _kill(proc)
                    job.status = "failed"
                    job.error_summary = "calibration exceeded the timeout"
                    return
                job.returncode = proc.returncode

            result = _load_result(job.workspace)
            if job.cancel_requested:
                job.status = "cancelled"
            elif result and result.get("ok"):
                job.status = "succeeded"
                job.error_summary = None
            else:
                job.status = "failed"
                job.error_summary = _short_error(result, job.log_path)
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error_summary = f"{type(exc).__name__}: {exc}"
        finally:
            job.ended_at = _now()
            with self._lock:
                self._procs.pop(job.id, None)
            self._store.save(job)
            with self._lock:
                ev = self._done.get(job.id)
            if ev is not None:
                ev.set()

    # ------------------------------------------------------------------
    def cancel(self, job_id: str) -> Job | None:
        job = self._store.get(job_id)
        if job is None:
            return None
        if job.status in TERMINAL:
            return job
        job.cancel_requested = True
        self._store.save(job)
        with self._lock:
            proc = self._procs.get(job_id)
        if proc is not None:
            _kill(proc)
        return job

    def wait(self, job_id: str, seconds: float) -> Job | None:
        job = self._store.get(job_id)
        if job is None:
            return None
        with self._lock:
            ev = self._done.get(job_id)
        if ev is not None:
            ev.wait(timeout=max(0.0, seconds))  # resolves only after the job is saved
        else:
            deadline = time.monotonic() + max(0.0, seconds)
            while time.monotonic() < deadline and job.status not in TERMINAL:
                time.sleep(1.0)
        return self._store.get(job_id) or job


# ---------------------------------------------------------------------------
def read_progress(job: Job, tail: int = 50) -> list[dict]:
    path = Path(job.datastack_path).parent / "progress.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-tail:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return out


def read_result(job: Job) -> dict | None:
    return _load_result(job.workspace)


def _load_result(workspace: str) -> dict | None:
    p = Path(workspace) / "calibration_result.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _short_error(result: dict | None, log_path: str) -> str:
    if result and result.get("errors"):
        return "; ".join(f"[{i['field']}] {i['message']}" for i in result["errors"])
    if result and result.get("error"):
        return str(result["error"])[-1500:]
    try:
        return "\n".join(Path(log_path).read_text(encoding="utf-8",
                         errors="replace").splitlines()[-25:])
    except OSError:
        return "calibration failed (no result file)"


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
        else:  # pragma: no cover
            proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
