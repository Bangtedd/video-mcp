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

from . import ffmpeg, segments
from .config import Config
from .errors import VideoMCPError
from .jobs import JobManager
from .render import build_render
from .templates import check_template_id, load_templates, text_timing
from .timeline import (
    FADE_RANGE,
    FITS,
    GRADIENTS,
    LOOKS,
    PRESETS,
    SPEED_RANGE,
    TEXT_POSITIONS,
    TEXT_SIZES,
    TRANSITION_RANGE,
    TRANSITIONS,
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
            project = self._new_project(name, preset, fit, now)
            self._save(project)
        return summarize(project)

    @staticmethod
    def _new_project(name: str, preset: str, fit: str, created_at: float) -> dict:
        return {
            "name": name,
            "preset": preset,
            "fit": fit,
            "clips": [],
            "music": None,
            "texts": [],
            "transition": {"type": "cut", "duration": 0.5},
            "look": "none",
            "gradient": "none",
            "fade_in": 0.0,
            "fade_out": 0.0,
            "next_clip_id": 1,
            "next_text_id": 1,
            "created_at": created_at,
            "updated_at": created_at,
        }

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
        fade: bool = False,
    ) -> dict:
        txt = check_text(text)
        s = check_nonneg("start", start)
        e = check_nonneg("end", end)
        check_choice("position", position, TEXT_POSITIONS)
        check_choice("size", size, TEXT_SIZES)
        col = check_color(color)
        if not isinstance(box, bool):
            raise VideoMCPError("box must be true or false.")
        if not isinstance(fade, bool):
            raise VideoMCPError("fade must be true or false.")
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
                "fade": fade,
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
        fade: bool | None = None,
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
            if fade is not None:
                if not isinstance(fade, bool):
                    raise VideoMCPError("fade must be true or false.")
                new["fade"] = fade
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

    # ------------------------------------------------------------- effects

    def _set_fields(self, project: str, **fields) -> dict:
        with self._lock:
            p = self._load(project)
            p.update(fields)
            self._save(p)
        return summarize(p)

    def set_transition(self, project: str, type: str = "cut", duration: float = 0.5) -> dict:
        check_choice("type", type, TRANSITIONS)
        d = check_range("duration", duration, *TRANSITION_RANGE)
        out = self._set_fields(project, transition={"type": type, "duration": d})
        return {"transition": out["transition"], "project": out}

    def set_look(self, project: str, look: str) -> dict:
        check_choice("look", look, LOOKS)
        return {"look": look, "project": self._set_fields(project, look=look)}

    def set_gradient(self, project: str, gradient: str) -> dict:
        check_choice("gradient", gradient, GRADIENTS)
        return {"gradient": gradient, "project": self._set_fields(project, gradient=gradient)}

    def set_fades(self, project: str, fade_in: float = 0.0, fade_out: float = 0.0) -> dict:
        fi = check_range("fade_in", fade_in, *FADE_RANGE)
        fo = check_range("fade_out", fade_out, *FADE_RANGE)
        out = self._set_fields(project, fade_in=fi, fade_out=fo)
        return {"fade_in": fi, "fade_out": fo, "project": out}

    # ------------------------------------------------------------ segments

    def suggest_segments(self, file: str, length: float, count: int = 1) -> dict:
        path = self.ws.resolve_file(file)
        info = self._probe(path)
        if not info["has_video"]:
            raise VideoMCPError(f"{file!r} has no video stream.")
        length = check_range("length", length, 0.1, 600)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
            raise VideoMCPError("count must be an integer between 1 and 100.")
        dur = info["duration"]
        windows = None
        method = "motion"
        if dur - 2 * segments.EDGE_SKIP >= length:
            try:
                samples = segments.cached_analysis(self.cfg.ffmpeg, self.ws.cache, path)
                windows = segments.pick_windows(samples, dur, length, count)
            except RuntimeError:
                windows = None
        if windows is None:
            method = "even"
            windows = [(s, e, None) for s, e in segments.even_windows(dur, length, count)]
        return {
            "file": self.ws.relative(path),
            "duration": dur,
            "length": length,
            "count": count,
            "method": method,
            "segments": [
                {"start": s, "end": e, **({"score": sc} if sc is not None else {})}
                for s, e, sc in windows
            ],
        }

    # ------------------------------------------------------------ templates

    def _templates(self) -> dict[str, dict]:
        return load_templates(self.cfg.templates_dir)[0]

    def list_templates(self) -> dict:
        templates, errors = load_templates(self.cfg.templates_dir)
        items = []
        for t in templates.values():
            items.append(
                {k: t[k] for k in ("id", "name", "description", "preset", "fit", "look",
                                   "gradient", "transition", "fade_in", "fade_out", "slots",
                                   "clip_volume", "music", "nominal_duration")}
                | {"texts": [
                    {k: f[k] for k in ("key", "label", "placeholder")} for f in t["texts"]
                ]}
            )
        out = {"count": len(items), "templates": items}
        if errors:
            out["errors"] = errors
        return out

    def get_template(self, template_id: str) -> dict:
        check_template_id(template_id)
        t = self._templates().get(template_id)
        if t is None:
            ids = ", ".join(self._templates()) or "none"
            raise VideoMCPError(f"No template {template_id!r}. Available: {ids}.")
        return t

    def apply_template(
        self,
        project: str,
        template_id: str,
        files: list[str],
        texts: dict[str, str] | None = None,
        music: str | None = None,
    ) -> dict:
        """Build `project` from a template, replacing its timeline if it exists."""
        check_project_name(project)
        t = self.get_template(template_id)
        if not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files):
            raise VideoMCPError("files must be a non-empty list of file names from list_media().")
        if len(files) > 200:
            raise VideoMCPError("Too many files; use at most 200.")
        texts = texts or {}
        if not isinstance(texts, dict):
            raise VideoMCPError("texts must be an object mapping text keys to strings.")
        keys = {f["key"] for f in t["texts"]}
        unknown = sorted(set(texts) - keys)
        if unknown:
            raise VideoMCPError(
                f"Unknown text field(s) {', '.join(unknown)} for template {template_id!r}. "
                f"It has: {', '.join(sorted(keys)) or 'none'}."
            )

        videos: list[tuple[Path, dict]] = []
        audio_only: list[Path] = []
        for f in files:
            path = self.ws.resolve_file(f)
            info = self._probe(path)
            if info["has_video"]:
                videos.append((path, info))
            elif info["has_audio"]:
                audio_only.append(path)
            else:
                raise VideoMCPError(f"{f!r} has neither video nor audio.")
        if not videos:
            raise VideoMCPError("Pass at least one video clip in files.")
        music_path = self.ws.resolve_file(music) if music else None
        if audio_only:
            if music_path is not None or len(audio_only) > 1:
                raise VideoMCPError("Pass at most one music file.")
            music_path = audio_only[0]
        music_info = self._probe(music_path) if music_path is not None else None
        if music_info is not None and not music_info["has_audio"]:
            raise VideoMCPError("The music file has no audio stream.")

        # Slots: one per file at least; the slot pattern repeats, files cycle.
        slots = t["slots"]
        n = max(len(slots), len(videos))
        uses: dict[int, list[int]] = {}
        for k in range(n):
            uses.setdefault(k % len(videos), []).append(k)
        picked: dict[int, tuple[float, float]] = {}
        for vi, ks in uses.items():
            path, info = videos[vi]
            longest = max(slots[k % len(slots)] for k in ks)
            segs = self.suggest_segments(self.ws.relative(path), longest, len(ks))["segments"]
            starts = [s["start"] for s in segs]
            if len(starts) < len(ks):
                # Not enough room for separate windows: add distinct, evenly spaced
                # (overlapping) starts so each use still shows a different moment.
                lo, hi = segments.usable_span(info["duration"], longest)
                room = max(0.0, hi - lo - longest)
                for i in range(len(ks)):
                    cand = round(lo + room * i / max(1, len(ks) - 1), 3)
                    if len(starts) < len(ks) and all(abs(cand - s) > 0.05 for s in starts):
                        starts.append(cand)
            for u, k in enumerate(ks):
                slot = slots[k % len(slots)]
                dur = info["duration"]
                if dur <= slot:
                    picked[k] = (0.0, dur)  # shorter than its slot: whole clip
                    continue
                start = min(starts[u % len(starts)], dur - slot)
                picked[k] = (round(start, 3), round(start + slot, 3))

        with self._lock:
            path = self.ws.project_path(project)
            created = time.time()
            if path.exists():
                created = self._load(project).get("created_at", created)
            p = self._new_project(project, t["preset"], t["fit"], created)
            p.update(
                template=t["id"],
                transition=dict(t["transition"]),
                look=t["look"],
                gradient=t["gradient"],
                fade_in=t["fade_in"],
                fade_out=t["fade_out"],
            )
            for k in range(n):
                vpath, info = videos[k % len(videos)]
                start, end = picked[k]
                p["clips"].append({
                    "id": f"c{p['next_clip_id']}",
                    "file": self.ws.relative(vpath),
                    "start": start,
                    "end": end,
                    "speed": 1.0,
                    "volume": t["clip_volume"],
                    "source_duration": info["duration"],
                    "has_audio": info["has_audio"],
                })
                p["next_clip_id"] += 1
            if music_path is not None:
                p["music"] = {
                    "file": self.ws.relative(music_path),
                    "volume": t["music"]["volume"],
                    "offset": 0.0,
                    "fade_in": t["music"]["fade_in"],
                    "fade_out": t["music"]["fade_out"],
                    "duration": music_info["duration"],
                }
            total = timeline_duration(p)
            skipped = []
            for field in t["texts"]:
                value = texts.get(field["key"])
                if value is None or not str(value).strip():
                    skipped.append(field["key"])
                    continue
                timing = text_timing(field, total)
                if timing is None:
                    skipped.append(field["key"])
                    continue
                p["texts"].append({
                    "id": f"t{p['next_text_id']}",
                    "key": field["key"],
                    "text": check_text(str(value)),
                    "start": timing[0],
                    "end": timing[1],
                    "position": field["position"],
                    "size": field["size"],
                    "color": field["color"],
                    "box": field["box"],
                    "fade": field["fade"],
                })
                p["next_text_id"] += 1
            self._save(p)
        return {"template": t["id"], "skipped_texts": skipped, "project": summarize(p)}

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
            lock_path=self.ws.render_lock,
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
