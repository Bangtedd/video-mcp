"""In-memory render job queue. One FFmpeg render runs at a time."""

from __future__ import annotations

import collections
import contextlib
import fcntl
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

STDERR_TAIL = 20


@dataclass
class Job:
    id: str
    project: str
    quality: str
    args: list[str]
    duration: float
    output: Path
    output_rel: str
    work_dir: Path
    lock_path: Path | None = None
    status: str = "queued"
    progress: float = 0.0
    error: str | None = None
    stderr_tail: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def to_dict(self) -> dict:
        d = {
            "job_id": self.id,
            "project": self.project,
            "quality": self.quality,
            "status": self.status,
            "progress": round(self.progress, 1),
            "output": self.output_rel if self.status == "done" else None,
            "expected_duration": round(self.duration, 3),
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if self.status == "failed":
            d["stderr"] = "\n".join(self.stderr_tail)
        return d


@contextlib.contextmanager
def render_lock(path: Path | None):
    """Exclusive lock on `path` (blocking), shared by every process using the
    same workspace, so only one FFmpeg render runs at a time across processes."""
    if path is None:
        yield
        return
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._queue: queue.Queue[Job] = queue.Queue()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, name="render-worker", daemon=True)
        self._worker.start()

    def submit(self, **kwargs) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], **kwargs)
        with self._lock:
            self._jobs[job.id] = job
        self._queue.put(job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                with render_lock(job.lock_path):  # status stays 'queued' while waiting
                    self._execute(job)
            except Exception as exc:  # never let the worker die
                job.status = "failed"
                job.error = f"Internal error while rendering: {exc}"
                job.finished_at = time.time()
            finally:
                shutil.rmtree(job.work_dir, ignore_errors=True)

    def _execute(self, job: Job) -> None:
        job.status = "running"
        job.started_at = time.time()
        tmp_out = job.output.with_name(job.output.name + ".part")
        args = list(job.args)
        args[-1] = str(tmp_out)
        tail: collections.deque[str] = collections.deque(maxlen=STDERR_TAIL)

        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            job.status = "failed"
            job.error = f"{args[0]!r} not found. Install FFmpeg (sudo apt install ffmpeg)."
            job.finished_at = time.time()
            return

        def read_stderr() -> None:
            assert proc.stderr is not None
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    tail.append(line)

        err_thread = threading.Thread(target=read_stderr, daemon=True)
        err_thread.start()

        assert proc.stdout is not None
        for raw in proc.stdout:
            key, _, value = raw.decode("utf-8", "replace").strip().partition("=")
            if key in ("out_time_us", "out_time_ms"):  # both are microseconds
                try:
                    seconds = int(value) / 1_000_000
                except ValueError:
                    continue
                if job.duration > 0:
                    job.progress = max(job.progress, min(99.0, seconds / job.duration * 100))
        rc = proc.wait()
        err_thread.join(timeout=5)
        job.stderr_tail = list(tail)
        job.finished_at = time.time()

        if rc == 0 and tmp_out.exists() and tmp_out.stat().st_size > 0:
            os.replace(tmp_out, job.output)
            job.progress = 100.0
            job.status = "done"
        else:
            tmp_out.unlink(missing_ok=True)
            job.status = "failed"
            last = job.stderr_tail[-1] if job.stderr_tail else "no output"
            job.error = f"FFmpeg exited with code {rc}: {last}"
