"""Tool implementations. Each public method returns a JSON-serialisable dict
and raises VideoMCPError with an actionable message on bad input."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import ffmpeg
from .config import Config
from .errors import VideoMCPError
from .jobs import JobManager
from .render import build_render
from .timeline import (
    FITS,
    PRESETS,
    SPEED_RANGE,
    TEXT_POSITIONS,
    TEXT_SIZES,
    VOLUME_RANGE,
    check_choice,
    check_color,
    check_nonneg,
    check_range,
    check_span,
    check_text,
    summarize,
    timeline_duration,
)
from .workspace import Workspace, check_project_name

MEDIA_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".3gp", ".mts",
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus",
}

class Editor:
    def __init__(self, cfg: Config, jobs: JobManager | None = None):
        self.cfg = cfg
        self.ws = Workspace(cfg.workspace_dir)
        self.jobs = jobs or JobManager()
        self._lock = threading.RLock()
        self._probe_cache: dict[tuple[str, float, int], dict] = {}

    # ---------------------------------------------------------------- media

    def _probe(self, path: Path) -> dict:
        st = path.stat()
        key = (str(path), st.st_mtime, st.st_size)
        if key not in self._probe_cache:
            self._probe_cache[key] = ffmpeg.probe(self.cfg.ffprobe, path)
        return self._probe_cache[key]

    def list_media(self) -> dict:
        items = []
        for path in sorted(self.ws.inbox.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            try:
                resolved = self.ws.resolve_file(str(path.relative_to(self.ws.inbox)))
            except VideoMCPError:
                continue  # e.g. symlink escaping the workspace
            entry = {"file": path.relative_to(self.ws.inbox).as_posix()}
            try:
                info = self._probe(resolved)
                entry.update(
                    duration=info["duration"],
                    width=info["width"],
                    height=info["height"],
                    fps=info["fps"],
                    has_audio=info["has_audio"],
                    has_video=info["has_video"],
                )
            except VideoMCPError as exc:
                entry["error"] = str(exc)
            items.append(entry)
        return {"inbox": self.ws.relative(self.ws.inbox), "count": len(items), "files": items}

    def probe_media(self, file: str) -> dict:
        path = self.ws.resolve_file(file)
        return {"file": self.ws.relative(path), **self._probe(path)}

    def get_frame(self, file: str, time_s: float) -> bytes:
        path = self.ws.resolve_file(file)
        info = self._probe(path)
        if not info["has_video"]:
            raise VideoMCPError(f"{file!r} has no video stream.")
        t = check_nonneg("time", time_s)
        if t >= info["duration"]:
            raise VideoMCPError(
                f"time {t:g}s is past the end of {file!r} ({info['duration']:.3f}s). "
                f"Use a time below {info['duration']:.3f}."
            )
        frames_dir = self.ws.cache / "frames"
        frames_dir.mkdir(exist_ok=True)
        out = frames_dir / f"{uuid.uuid4().hex}.jpg"
        try:
            ffmpeg.extract_frame(self.cfg.ffmpeg, path, t, out)
            return out.read_bytes()
        finally:
            out.unlink(missing_ok=True)

    # ------------------------------------------------------------- projects

    def _load(self, name: str) -> dict:
        path = self.ws.project_path(name)
        if not path.exists():
            raise VideoMCPError(
                f"Project {name!r} does not exist. Create it with create_project() "
                "or see list_projects()."
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, project: dict) -> None:
        project["updated_at"] = time.time()
        path = self.ws.project_path(project["name"])
        fd, tmp = tempfile.mkstemp(dir=self.ws.projects, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(project, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    def create_project(self, name: str, preset: str = "vertical", fit: str = "crop") -> dict:
        check_project_name(name)
        check_choice("preset", preset, tuple(PRESETS))
        check_choice("fit", fit, FITS)
        with self._lock:
            if self.ws.project_path(name).exists():
                raise VideoMCPError(
                    f"Project {name!r} already exists. Pick another name or use get_project()."
                )
            now = time.time()
            project = {
                "name": name,
                "preset": preset,
                "fit": fit,
                "clips": [],
                "music": None,
                "texts": [],
                "next_clip_id": 1,
                "next_text_id": 1,
                "created_at": now,
                "updated_at": now,
            }
            self._save(project)
        return summarize(project)

    def list_projects(self) -> dict:
        out = []
        for path in sorted(self.ws.projects.glob("*.json")):
            try:
                p = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append(
                {
                    "name": p["name"],
                    "preset": p["preset"],
                    "clips": len(p["clips"]),
                    "output_duration": timeline_duration(p) if p["clips"] else 0.0,
                }
            )
        return {"count": len(out), "projects": out}

    def get_project(self, name: str) -> dict:
        return summarize(self._load(name))

    # ---------------------------------------------------------------- clips

    @staticmethod
    def _find(items: list[dict], item_id: str, kind: str) -> int:
        for i, item in enumerate(items):
            if item["id"] == item_id:
                return i
        ids = ", ".join(x["id"] for x in items) or "none"
        raise VideoMCPError(f"No {kind} with id {item_id!r}. Existing ids: {ids}.")

    @staticmethod
    def _position(value, length: int, name: str = "position") -> int:
        if value is None:
            return length
        if isinstance(value, bool) or not isinstance(value, int):
            raise VideoMCPError(f"{name} must be an integer index (0 = first).")
        if not 0 <= value <= length:
            raise VideoMCPError(f"{name} must be between 0 and {length}, got {value}.")
        return value

    def add_clip(
        self,
        project: str,
        file: str,
        start: float | None = None,
        end: float | None = None,
        position: int | None = None,
    ) -> dict:
        path = self.ws.resolve_file(file)
        info = self._probe(path)
        if not info["has_video"]:
            raise VideoMCPError(f"{file!r} has no video stream; use set_music() for audio files.")
        dur = info["duration"]
        s = 0.0 if start is None else check_nonneg("start", start)
        e = dur if end is None else check_nonneg("end", end)
        check_span(f"Clip from {file!r}", s, e, dur, "the file")
        with self._lock:
            p = self._load(project)
            pos = self._position(position, len(p["clips"]))
            clip = {
                "id": f"c{p['next_clip_id']}",
                "file": self.ws.relative(path),
                "start": round(s, 3),
                "end": round(e, 3),
                "speed": 1.0,
                "volume": 1.0,
                "source_duration": dur,
                "has_audio": info["has_audio"],
            }
            p["next_clip_id"] += 1
            p["clips"].insert(pos, clip)
            self._save(p)
        return {"clip": clip, "project": summarize(p)}

    def update_clip(
        self,
        project: str,
        clip_id: str,
        start: float | None = None,
        end: float | None = None,
        speed: float | None = None,
        volume: float | None = None,
    ) -> dict:
        with self._lock:
            p = self._load(project)
            clip = p["clips"][self._find(p["clips"], clip_id, "clip")]
            new = dict(clip)
            if start is not None:
                new["start"] = round(check_nonneg("start", start), 3)
            if end is not None:
                new["end"] = round(check_nonneg("end", end), 3)
            if speed is not None:
                new["speed"] = check_range("speed", speed, *SPEED_RANGE)
            if volume is not None:
                new["volume"] = check_range("volume", volume, *VOLUME_RANGE)
            check_span(f"Clip {clip_id}", new["start"], new["end"], clip["source_duration"], "the file")
            clip.update(new)
            self._save(p)
        return {"clip": clip, "project": summarize(p)}

    def move_clip(self, project: str, clip_id: str, position: int) -> dict:
        with self._lock:
            p = self._load(project)
            idx = self._find(p["clips"], clip_id, "clip")
            clip = p["clips"].pop(idx)
            pos = self._position(position, len(p["clips"]))
            p["clips"].insert(pos, clip)
            self._save(p)
        return {"order": [c["id"] for c in p["clips"]], "project": summarize(p)}

    def remove_clip(self, project: str, clip_id: str) -> dict:
        with self._lock:
            p = self._load(project)
            removed = p["clips"].pop(self._find(p["clips"], clip_id, "clip"))
            self._save(p)
        return {"removed": removed["id"], "project": summarize(p)}

    # ---------------------------------------------------------------- music

    def set_music(
        self,
        project: str,
        file: str | None,
        volume: float = 0.3,
        offset: float = 0.0,
        fade_in: float = 0.0,
        fade_out: float = 0.0,
    ) -> dict:
        if file is None:
            with self._lock:
                p = self._load(project)
                p["music"] = None
                self._save(p)
            return {"music": None, "project": summarize(p)}

        path = self.ws.resolve_file(file)
        info = self._probe(path)
        if not info["has_audio"]:
            raise VideoMCPError(f"{file!r} has no audio stream.")
        vol = check_range("volume", volume, *VOLUME_RANGE)
        off = check_nonneg("offset", offset)
        if off >= info["duration"]:
            raise VideoMCPError(
                f"offset {off:g}s is past the end of {file!r} ({info['duration']:.3f}s)."
            )
        fi = check_range("fade_in", fade_in, 0, 60)
        fo = check_range("fade_out", fade_out, 0, 60)
        music = {
            "file": self.ws.relative(path),
            "volume": vol,
            "offset": round(off, 3),
            "fade_in": fi,
            "fade_out": fo,
            "duration": info["duration"],
        }
        with self._lock:
            p = self._load(project)
            p["music"] = music
            self._save(p)
        return {"music": music, "project": summarize(p)}

    # ---------------------------------------------------------------- texts

    def _check_text_span(self, p: dict, label: str, start: float, end: float) -> None:
        if not p["clips"]:
            raise VideoMCPError("Add clips before adding text; overlays must fit the timeline.")
        check_span(label, start, end, timeline_duration(p), "the timeline")

    def add_text(
        self,
        project: str,
        text: str,
        start: float,
        end: float,
        position: str = "bottom",
        size: str = "medium",
        color: str = "white",
        box: bool = False,
    ) -> dict:
        txt = check_text(text)
        s = check_nonneg("start", start)
        e = check_nonneg("end", end)
        check_choice("position", position, TEXT_POSITIONS)
        check_choice("size", size, TEXT_SIZES)
        col = check_color(color)
        if not isinstance(box, bool):
            raise VideoMCPError("box must be true or false.")
        with self._lock:
            p = self._load(project)
            self._check_text_span(p, "Text", s, e)
            item = {
                "id": f"t{p['next_text_id']}",
                "text": txt,
                "start": round(s, 3),
                "end": round(e, 3),
                "position": position,
                "size": size,
                "color": col,
                "box": box,
            }
            p["next_text_id"] += 1
            p["texts"].append(item)
            self._save(p)
        return {"text": item, "project": summarize(p)}

    def update_text(
        self,
        project: str,
        text_id: str,
        text: str | None = None,
        start: float | None = None,
        end: float | None = None,
        position: str | None = None,
        size: str | None = None,
        color: str | None = None,
        box: bool | None = None,
    ) -> dict:
        with self._lock:
            p = self._load(project)
            item = p["texts"][self._find(p["texts"], text_id, "text")]
            new = dict(item)
            if text is not None:
                new["text"] = check_text(text)
            if start is not None:
                new["start"] = round(check_nonneg("start", start), 3)
            if end is not None:
                new["end"] = round(check_nonneg("end", end), 3)
            if position is not None:
                new["position"] = check_choice("position", position, TEXT_POSITIONS)
            if size is not None:
                new["size"] = check_choice("size", size, TEXT_SIZES)
            if color is not None:
                new["color"] = check_color(color)
            if box is not None:
                if not isinstance(box, bool):
                    raise VideoMCPError("box must be true or false.")
                new["box"] = box
            self._check_text_span(p, f"Text {text_id}", new["start"], new["end"])
            item.update(new)
            self._save(p)
        return {"text": item, "project": summarize(p)}

    def remove_text(self, project: str, text_id: str) -> dict:
        with self._lock:
            p = self._load(project)
            removed = p["texts"].pop(self._find(p["texts"], text_id, "text"))
            self._save(p)
        return {"removed": removed["id"], "project": summarize(p)}

    # --------------------------------------------------------------- render

    def render(self, project: str, quality: str = "preview") -> dict:
        check_choice("quality", quality, ("preview", "final"))
        p = self._load(project)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        output = self.ws.renders / f"{p['name']}-{quality}-{stamp}-{uuid.uuid4().hex[:6]}.mp4"
        work_dir = Path(tempfile.mkdtemp(prefix="render-", dir=self.ws.cache))
        try:
            plan = build_render(p, self.ws, self.cfg, quality, output, work_dir)
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise
        job = self.jobs.submit(
            project=p["name"],
            quality=quality,
            args=plan.args,
            duration=plan.duration,
            output=output,
            output_rel=self.ws.relative(self.ws.renders) + "/" + output.name,
            work_dir=work_dir,
        )
        return {
            "job_id": job.id,
            "status": job.status,
            "expected_duration": round(plan.duration, 3),
            "width": plan.width,
            "height": plan.height,
            "output": job.output_rel,
            "hint": "Poll get_job(job_id) until status is 'done' or 'failed'.",
        }

    def get_job(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise VideoMCPError(
                f"No job with id {job_id!r}. Jobs are kept in memory and are lost on restart."
            )
        return job.to_dict()
