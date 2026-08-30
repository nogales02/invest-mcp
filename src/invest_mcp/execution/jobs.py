"""Job model + persistent job store.

A *job* is one InVEST model run. Each job owns a directory under
``<data_root>/jobs/<job_id>/`` containing:

    datastack.json   the parameter set handed to `invest run`
    run.log          captured stdout/stderr of the run
    provenance.json  reproducibility manifest (written on completion)
    workspace/       InVEST's own output folder

The store keeps jobs in memory and mirrors each one to ``job.json`` so state
survives a server restart.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from invest_mcp.config import Settings

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
TERMINAL: set[str] = {"succeeded", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    model_id: str
    status: JobStatus = "queued"
    workspace: str = ""
    datastack_path: str = ""
    log_path: str = ""
    provenance_path: str = ""
    pid: int | None = None
    returncode: int | None = None
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    ended_at: str | None = None
    error_summary: str | None = None
    cancel_requested: bool = False

    @property
    def dir(self) -> Path:
        return Path(self.datastack_path).parent if self.datastack_path else Path(self.workspace).parent

    def public_dict(self) -> dict:
        d = asdict(self)
        d.pop("cancel_requested", None)
        d["job_id"] = self.id  # explicit alias; every tool/hint refers to "job_id"
        return d


class JobStore:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._jobs: dict[str, Job] = {}
        self._load_existing()

    # ----- lifecycle -----------------------------------------------------
    def _load_existing(self) -> None:
        jobs_dir = self._settings.jobs_dir
        if not jobs_dir.is_dir():
            return
        for job_json in jobs_dir.glob("*/job.json"):
            try:
                data = json.loads(job_json.read_text(encoding="utf-8"))
                job = Job(**{k: data.get(k) for k in Job.__dataclass_fields__})
            except Exception:
                continue
            # A job left "running" by a crashed server is not actually running.
            if job.status in ("running", "queued"):
                job.status = "failed"
                job.error_summary = "Server restarted while this job was in progress."
            self._jobs[job.id] = job

    def new_job(self, model_id: str) -> Job:
        jid = f"{model_id}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        jdir = self._settings.jobs_dir / jid
        (jdir / "workspace").mkdir(parents=True, exist_ok=True)
        job = Job(
            id=jid,
            model_id=model_id,
            workspace=str(jdir / "workspace"),
            datastack_path=str(jdir / "datastack.json"),
            log_path=str(jdir / "run.log"),
            provenance_path=str(jdir / "provenance.json"),
        )
        self._jobs[jid] = job
        self.save(job)
        return job

    def save(self, job: Job) -> None:
        path = self._settings.jobs_dir / job.id / "job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(job), indent=2), encoding="utf-8")

    # ----- queries -----------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self, limit: int | None = None) -> list[Job]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return jobs[:limit] if limit else jobs
