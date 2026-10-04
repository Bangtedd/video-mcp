"""Upload sessions for the web app.

Each session is a directory inbox/<session_id>/ holding the uploaded files
under generated names, a hidden .session.json with their order and metadata,
and a hidden .thumbs/ folder. Deleting the directory deletes the session.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from ..effects import gradient_edges, gradient_png, look_filters
from ..errors import VideoMCPError
from ..timeline import PRESETS
from ..workspace import Workspace

SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
FILE_ID_RE = re.compile(r"^[0-9a-f]{16}$")
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav"}
ALLOWED_EXTS = VIDEO_EXTS | AUDIO_EXTS
MAX_CLIPS = 30
SESSION_TTL = 7 * 24 * 3600
THUMB_LONG_SIDE = 320
META = ".session.json"


class NotFound(VideoMCPError):
    pass


def check_session_id(sid) -> str:
    if not isinstance(sid, str) or not SESSION_ID_RE.match(sid):
        raise NotFound("Unknown session.")
    return sid


def check_file_id(fid) -> str:
    if not isinstance(fid, str) or not FILE_ID_RE.match(fid):
        raise NotFound("Unknown file.")
    return fid


def project_name(sid: str) -> str:
    return f"web-{sid}"


class Store:
    def __init__(self, ws: Workspace, ffmpeg: str):
        self.ws = ws
        self.ffmpeg = ffmpeg
        self._lock = threading.Lock()

    # ------------------------------------------------------------ sessions

    def session_dir(self, sid: str) -> Path:
        check_session_id(sid)
        path = self.ws.inbox / sid
        self.ws._contained(path, sid)
        return path

    def _meta_path(self, sid: str) -> Path:
        return self.session_dir(sid) / META

    def create(self) -> dict:
        sid = os.urandom(16).hex()
        d = self.session_dir(sid)
        d.mkdir()
        (d / ".thumbs").mkdir()
        now = time.time()
        meta = {"id": sid, "created": now, "last_activity": now, "clips": [], "music": None,
                "template": None, "texts": {}}
        self._write(sid, meta)
        return meta

    def load(self, sid: str, touch: bool = True) -> dict:
        path = self._meta_path(sid)
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise NotFound("Unknown or expired session. Reload the page to start a new one.")
        if touch:
            meta["last_activity"] = time.time()
            self._write(sid, meta)
        return meta

    def _write(self, sid: str, meta: dict) -> None:
        d = self.session_dir(sid)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".meta-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        os.replace(tmp, d / META)

    def update(self, sid: str, fn) -> dict:
        with self._lock:
            meta = self.load(sid, touch=False)
            fn(meta)
            meta["last_activity"] = time.time()
            self._write(sid, meta)
            return meta

    def delete(self, sid: str) -> None:
        d = self.session_dir(sid)
        shutil.rmtree(d, ignore_errors=True)
        self.ws.project_path(project_name(sid)).unlink(missing_ok=True)

    def cleanup(self, max_age: float = SESSION_TTL) -> list[str]:
        """Delete sessions (and their uploads and project) idle for `max_age` seconds."""
        removed = []
        cutoff = time.time() - max_age
        for d in self.ws.inbox.iterdir():
            if not d.is_dir() or d.is_symlink() or not SESSION_ID_RE.match(d.name):
                continue
            try:
                last = float(json.loads((d / META).read_text())["last_activity"])
            except (OSError, ValueError, KeyError, TypeError):
                last = d.stat().st_mtime
            if last < cutoff:
                self.delete(d.name)
                removed.append(d.name)
        return removed

    # --------------------------------------------------------------- files

    def new_upload_path(self, sid: str, ext: str) -> tuple[str, Path]:
        fid = os.urandom(8).hex()
        return fid, self.session_dir(sid) / f"{fid}{ext}"

    def find(self, meta: dict, fid: str) -> dict:
        check_file_id(fid)
        for item in meta["clips"] + ([meta["music"]] if meta["music"] else []):
            if item["id"] == fid:
                return item
        raise NotFound("Unknown file.")

    def file_path(self, sid: str, item: dict) -> Path:
        return self.ws.resolve_file(item["file"])

    def remove_file(self, sid: str, fid: str) -> dict:
        check_file_id(fid)
        removed: list[dict] = []

        def fn(meta):
            item = self.find(meta, fid)
            removed.append(item)
            if meta["music"] and meta["music"]["id"] == fid:
                meta["music"] = None
            else:
                meta["clips"] = [c for c in meta["clips"] if c["id"] != fid]

        meta = self.update(sid, fn)
        for item in removed:
            (self.ws.inbox / item["file"]).unlink(missing_ok=True)
            for thumb in (self.session_dir(sid) / ".thumbs").glob(f"*{fid}*"):
                thumb.unlink(missing_ok=True)
        return meta

    def reorder(self, sid: str, ids: list) -> dict:
        def fn(meta):
            current = [c["id"] for c in meta["clips"]]
            if not isinstance(ids, list) or sorted(map(str, ids)) != sorted(current):
                raise VideoMCPError("order must list every clip id exactly once.")
            by_id = {c["id"]: c for c in meta["clips"]}
            meta["clips"] = [by_id[i] for i in ids]

        return self.update(sid, fn)

    # ---------------------------------------------------------- thumbnails

    def _thumb_dir(self, sid: str) -> Path:
        d = self.session_dir(sid) / ".thumbs"
        d.mkdir(exist_ok=True)
        return d

    def _ffmpeg_still(self, args: list[str], out: Path) -> Path:
        tmp = out.with_name(out.stem + ".tmp.jpg")
        proc = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y", *args,
             "-frames:v", "1", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", str(tmp)],
            capture_output=True, timeout=120, check=False,
        )
        if proc.returncode != 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            raise VideoMCPError("Could not make a thumbnail.")
        os.replace(tmp, out)
        return out

    @staticmethod
    def thumb_time(item: dict) -> float:
        return round(min(1.0, item["duration"] / 2), 3)

    def clip_thumb(self, sid: str, item: dict) -> Path:
        out = self._thumb_dir(sid) / f"clip-{item['id']}.jpg"
        if out.is_file():
            return out
        src = self.file_path(sid, item)
        vf = (
            f"scale={THUMB_LONG_SIDE}:{THUMB_LONG_SIDE}:force_original_aspect_ratio=decrease:"
            "force_divisible_by=2,format=yuvj420p"
        )
        return self._ffmpeg_still(
            ["-ss", f"{self.thumb_time(item):.3f}", "-i", str(src), "-vf", vf], out
        )

    def template_thumb(self, sid: str, item: dict, template: dict) -> Path:
        """The clip's thumbnail frame with the template's look and gradient applied."""
        out = self._thumb_dir(sid) / f"tpl-{template['id']}-{item['id']}.jpg"
        if out.is_file():
            return out
        src = self.file_path(sid, item)
        fw, fh = PRESETS[template["preset"]]
        scale = THUMB_LONG_SIDE / max(fw, fh)
        W, H = int(fw * scale) // 2 * 2, int(fh * scale) // 2 * 2
        if template["fit"] == "crop":
            geom = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
        else:
            geom = (f"scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                    f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black")
        args = ["-ss", f"{self.thumb_time(item):.3f}", "-i", str(src)]
        chains = [f"[0:v]{geom},setsar=1,format=yuv420p" +
                  "".join("," + f for f in look_filters(template["look"])) + "[v0]"]
        label = "v0"
        for n, edge in enumerate(gradient_edges(template["gradient"]), start=1):
            args += ["-i", str(gradient_png(self.ws.cache, edge, W, H))]
            y = "0" if edge == "top" else "main_h-overlay_h"
            chains.append(f"[{label}][{n}:v]overlay=x=0:y={y}[v{n}]")
            label = f"v{n}"
        chains.append(f"[{label}]format=yuvj420p[out]")
        return self._ffmpeg_still(
            args + ["-filter_complex", ";".join(chains), "-map", "[out]"], out
        )
