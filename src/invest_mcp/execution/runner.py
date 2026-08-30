"""Launch and supervise ``invest run`` subprocesses.

Design notes
------------
* One OS process per model run -> crashes (GDAL segfaults) stay contained and
  cancellation is a real kill, not a cooperative flag.
* A bounded semaphore caps concurrent runs; extra jobs sit in ``queued``.
* Each job gets a daemon monitor thread that waits on the process, captures the
  exit code, writes provenance and flips the job to a terminal state.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from invest_mcp import invest_cli
from invest_mcp.config import Settings
from invest_mcp.execution.jobs import TERMINAL, Job, JobStore
from invest_mcp.models import registry
from invest_mcp.models.spec_translate import path_args
from invest_mcp.provenance import write_provenance
from invest_mcp.workspace.sandbox import SandboxError, check_input_paths


class RunnerError(RuntimeError):
    pass


class JobRunner:
    def __init__(self, settings: Settings, store: JobStore):
        self._settings = settings
        self._store = store
        self._sem = threading.BoundedSemaphore(settings.max_concurrent_jobs)
        self._procs: dict[str, subprocess.Popen] = {}
        self._done: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def submit(self, model_id: str, args: dict, extra_allowed: list[Path] | None = None) -> Job:
        canonical = registry.resolve_model_id(model_id)
        spec = registry.get_spec(canonical)

        user_args = dict(args or {})
        user_args.pop("workspace_dir", None)  # server-managed

        # Validate that every input path is real and inside an allowed root.
        checked = check_input_paths(
            user_args, path_args(spec), self._settings.allowed_roots(extra_allowed)
        )

        job = self._store.new_job(canonical)
        full_args = {**user_args, "workspace_dir": job.workspace}
        datastack = {"model_id": canonical, "args": full_args}
        Path(job.datastack_path).write_text(json.dumps(datastack, indent=2), encoding="utf-8")

        with self._lock:
            self._done[job.id] = threading.Event()
        t = threading.Thread(target=self._supervise, args=(job, checked), daemon=True)
        t.start()
        return job

    # ------------------------------------------------------------------
    def _supervise(self, job: Job, input_paths: dict[str, str]) -> None:
        acquired = self._sem.acquire(timeout=self._settings.invest_timeout_seconds)
        if not acquired:
            job.status = "failed"
            job.error_summary = "Timed out waiting for a free run slot."
            job.ended_at = _now()
            self._store.save(job)
            return
        try:
            self._launch_and_wait(job)
        except Exception as exc:  # noqa: BLE001 - record anything that escapes
            job.status = "failed"
            job.error_summary = f"{type(exc).__name__}: {exc}"
        finally:
            job.ended_at = _now()
            with self._lock:
                self._procs.pop(job.id, None)
            try:
                write_provenance(job, self._settings, input_paths)
            except Exception:  # provenance must never mask the run result
                pass
            self._store.save(job)
            self._sem.release()
            with self._lock:
                ev = self._done.get(job.id)
            if ev is not None:
                ev.set()  # unblock wait() only once the job is fully finalized

    def _launch_and_wait(self, job: Job) -> None:
        if job.cancel_requested:
            job.status = "cancelled"
            return

        exe = str(self._settings.resolved_invest_exe)
        cmd = [
            exe, "run", job.model_id,
            "-d", job.datastack_path,
            "-w", job.workspace,
            "--no-report",
        ]
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if self._settings.locale:
            env.setdefault("LANG", self._settings.locale)

        job.status = "running"
        job.started_at = _now()
        self._store.save(job)

        with open(job.log_path, "w", encoding="utf-8", errors="replace") as log:
            log.write(f"$ {' '.join(cmd)}\n\n")
            log.flush()
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            proc = subprocess.Popen(
                cmd, stdout=log, stderr=subprocess.STDOUT, env=env,
                cwd=str(Path(job.workspace).parent), creationflags=creationflags,
            )
            with self._lock:
                self._procs[job.id] = proc
            job.pid = proc.pid
            self._store.save(job)

            try:
                rc = proc.wait(timeout=self._settings.invest_timeout_seconds)
            except subprocess.TimeoutExpired:
                self._kill(proc)
                job.status = "failed"
                job.error_summary = (
                    f"Run exceeded the {self._settings.invest_timeout_seconds}s timeout "
                    "and was terminated."
                )
                return

        job.returncode = rc
        if job.cancel_requested:
            job.status = "cancelled"
        elif rc == 0:
            job.status = "succeeded"
        else:
            job.status = "failed"
            job.error_summary = _tail(job.log_path, 40)

    # ------------------------------------------------------------------
    def cancel(self, job_id: str) -> Job:
        job = self._store.get(job_id)
        if job is None:
            raise RunnerError(f"No such job: {job_id}")
        if job.status in TERMINAL:
            return job
        job.cancel_requested = True
        self._store.save(job)
        with self._lock:
            proc = self._procs.get(job_id)
        if proc is not None:
            self._kill(proc)
        return job

    def wait(self, job_id: str, seconds: float) -> Job:
        job = self._store.get(job_id)
        if job is None:
            raise RunnerError(f"No such job: {job_id}")
        with self._lock:
            ev = self._done.get(job_id)
        if ev is not None:
            # resolves only after provenance is written and the job is saved
            ev.wait(timeout=max(0.0, seconds))
        else:
            # job predates this process (loaded from disk) -> already terminal
            deadline = time.monotonic() + max(0.0, seconds)
            while time.monotonic() < deadline and job.status not in TERMINAL:
                time.sleep(0.5)
        return self._store.get(job_id) or job

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, check=False,
                )
            else:  # pragma: no cover - posix path
                proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _now() -> str:
    from invest_mcp.execution.jobs import _now as jn  # reuse the same clock

    return jn()


def _tail(path: str, n: int) -> str:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:]).strip()


__all__ = ["JobRunner", "RunnerError", "SandboxError", "_tail"]
