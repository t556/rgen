"""An in-memory job table around ordinary CLI subprocesses."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from threading import Lock
import time
import uuid

from ..config import OutputError, save_config


class JobBusy(OutputError):
    pass


@dataclass
class Job:
    id: str
    kind: str
    name: str
    directory: Path
    process: subprocess.Popen
    started: float
    finished: float | None = None


class JobManager:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).absolute()
        self.jobs: dict[str, Job] = {}
        self.lock = Lock()

    def start(self, kind: str, cfg, name: str) -> Job:
        with self.lock:
            if kind == "generate" and any(job.kind == "generate" and job.process.poll() is None for job in self.jobs.values()):
                raise JobBusy("A generate job is already running. Wait for it to finish before starting another.")
            identifier = uuid.uuid4().hex
            directory = self.directory / identifier
            directory.mkdir(parents=True)
            snapshot = directory / "config.json"
            save_config(cfg, snapshot)
            command = [sys.executable, "-m", "resultsgen", kind, str(snapshot),
                       "--progress-file", str(directory / "progress.json")]
            started = time.monotonic()
            with (directory / "log.txt").open("wb") as output:
                process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
            job = Job(identifier, kind, name, directory, process, started)
            self.jobs[identifier] = job
            return job

    def status(self, identifier: str) -> dict:
        with self.lock:
            job = self.jobs[identifier]
            return_code = job.process.poll()
            if return_code is not None and job.finished is None:
                job.finished = time.monotonic()
            try:
                progress = json.loads((job.directory / "progress.json").read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                progress = {}
            state = progress.get("status", "running")
            if return_code is not None:
                state = "complete" if return_code == 0 else "failed"
            report = progress.get("report")
            total = progress.get("runs_total", report.get("runs", 0) if report else 0)
            done = total if state == "complete" else progress.get("runs_done", 0)
            with (job.directory / "log.txt").open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 16_384))
                log = stream.read().decode("utf-8", errors="replace")
            return {
                "id": job.id, "kind": job.kind, "name": job.name,
                "status": state, "exit_code": return_code,
                "runs_done": done, "runs_total": total,
                "current_run": progress.get("current_run"),
                "elapsed": (job.finished or time.monotonic()) - job.started,
                "report": report, "log": log,
            }
