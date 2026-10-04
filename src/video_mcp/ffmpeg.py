"""Thin wrappers around the ffprobe / ffmpeg binaries (argument lists only)."""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from .errors import VideoMCPError

FRAME_MAX_SIDE = 768


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise VideoMCPError(
            f"{args[0]!r} was not found. Install FFmpeg (sudo apt install ffmpeg)."
        )
    except subprocess.TimeoutExpired:
        raise VideoMCPError(f"{Path(args[0]).name} timed out after {timeout:.0f}s.")


def _parse_rate(rate: str | None) -> float | None:
    if not rate or rate in ("0/0", "0"):
        return None
    try:
        value = float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        return None
    return round(value, 3) if value > 0 else None


def _rotation(stream: dict) -> int:
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                return int(round(float(sd["rotation"])))
            except (TypeError, ValueError):
                pass
    rotate = (stream.get("tags") or {}).get("rotate")
    if rotate is not None:
        try:
            return -int(rotate)  # tag is clockwise, display matrix counter-clockwise
        except ValueError:
            pass
    return 0


def probe(ffprobe: str, path: Path) -> dict[str, Any]:
    """Probe a media file. Raises VideoMCPError if it is not readable media."""
    proc = _run(
        [
            ffprobe, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        timeout=60,
    )
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = msg[-1] if msg else "unknown error"
        raise VideoMCPError(f"Cannot read {path.name!r} as media: {detail}")
    data = json.loads(proc.stdout or b"{}")
    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    video = next(
        (
            s for s in streams
            if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = None
    for src in (fmt, video or {}, audio or {}):
        try:
            d = float(src.get("duration"))
        except (TypeError, ValueError):
            continue
        if d > 0:
            duration = d
            break
    if duration is None:
        raise VideoMCPError(f"Cannot determine the duration of {path.name!r}.")

    info: dict[str, Any] = {
        "duration": round(duration, 3),
        "size_bytes": int(fmt.get("size") or path.stat().st_size),
        "format": fmt.get("format_name"),
        "has_video": video is not None,
        "has_audio": audio is not None,
        "width": None,
        "height": None,
        "fps": None,
        "rotation": 0,
        "orientation": None,
    }
    if video is not None:
        w, h = int(video.get("width") or 0), int(video.get("height") or 0)
        rot = _rotation(video)
        if rot % 180 != 0:
            w, h = h, w  # report display size, as autorotate will produce it
        info.update(
            width=w,
            height=h,
            fps=_parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate")),
            rotation=rot,
            orientation="portrait" if h > w else ("landscape" if w > h else "square"),
            video_codec=video.get("codec_name"),
            pix_fmt=video.get("pix_fmt"),
        )
    if audio is not None:
        info.update(
            audio_codec=audio.get("codec_name"),
            sample_rate=int(audio.get("sample_rate") or 0) or None,
            channels=audio.get("channels"),
        )
    return info


def extract_frame(ffmpeg: str, path: Path, time: float, out: Path) -> None:
    """Write one JPEG frame at `time`, long side scaled to at most 768 px."""
    vf = (
        f"scale=w='min({FRAME_MAX_SIDE},iw)':h='min({FRAME_MAX_SIDE},ih)'"
        ":force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuvj420p"
    )
    proc = _run(
        [
            ffmpeg, "-hide_banner", "-v", "error", "-y",
            "-ss", f"{time:.3f}", "-i", str(path),
            "-frames:v", "1", "-vf", vf, "-q:v", "3", "-f", "image2", "-c:v", "mjpeg",
            str(out),
        ],
        timeout=120,
    )
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = msg[-1] if msg else "no frame decoded"
        raise VideoMCPError(f"Could not extract a frame at {time}s: {detail}")
