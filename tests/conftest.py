"""Test fixtures. All media is generated at test time with FFmpeg lavfi sources."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from video_mcp.config import Config
from video_mcp.editor import Editor
from video_mcp.jobs import JobManager

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

if not FFMPEG or not FFPROBE:
    raise RuntimeError(
        "ffmpeg/ffprobe not found. Install them (sudo apt install ffmpeg fonts-dejavu-core); "
        "these tests do not mock FFmpeg."
    )


def run_ffmpeg(*args: str) -> None:
    subprocess.run([FFMPEG, "-hide_banner", "-v", "error", "-y", *args], check=True)


def _supports_display_rotation() -> bool:
    out = subprocess.run([FFMPEG, "-hide_banner", "-h", "full"], capture_output=True, text=True)
    return "-display_rotation" in out.stdout


def make_media(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "landscape": directory / "landscape.mp4",
        "portrait": directory / "portrait.mp4",
        "silent": directory / "silent.mp4",
        "music": directory / "music.m4a",
        "rotated": directory / "rotated.mp4",
    }
    # Landscape 16:9, 30 fps, mono 44.1 kHz audio (exercises resample + upmix).
    run_ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=4",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(files["landscape"]),
    )
    # Portrait 9:16, 25 fps, stereo 48 kHz.
    run_ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=25:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=4",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ac", "2", "-shortest", str(files["portrait"]),
    )
    # Square-ish clip with no audio track at all, 24 fps.
    run_ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=480x480:rate=24:duration=3",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-an", str(files["silent"]),
    )
    # Music bed.
    run_ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=44100:duration=20",
        "-c:a", "aac", "-b:a", "96k", str(files["music"]),
    )
    # Landscape-coded clip with 90 degree rotation metadata (displays as portrait).
    if _supports_display_rotation():
        run_ffmpeg(
            "-display_rotation", "90", "-i", str(files["landscape"]),
            "-c", "copy", str(files["rotated"]),
        )
    else:  # FFmpeg 5.1: the mov muxer still honours the 'rotate' tag
        run_ffmpeg(
            "-i", str(files["landscape"]), "-c", "copy",
            "-metadata:s:v:0", "rotate=90", str(files["rotated"]),
        )
    return files


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("media")
    make_media(d)
    return d


@pytest.fixture
def workspace(tmp_path, media_dir) -> Path:
    ws = tmp_path / "workspace"
    (ws / "inbox").mkdir(parents=True)
    for f in media_dir.iterdir():
        shutil.copy(f, ws / "inbox" / f.name)
    return ws


@pytest.fixture(scope="session")
def job_manager() -> JobManager:
    return JobManager()


@pytest.fixture
def editor(workspace, job_manager) -> Editor:
    return Editor(Config(workspace_dir=workspace), jobs=job_manager)


def probe(path: Path) -> dict:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, check=True,
    )
    data = json.loads(out.stdout)
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    a = [s for s in data["streams"] if s["codec_type"] == "audio"]
    return {
        "width": int(v["width"]),
        "height": int(v["height"]),
        "duration": float(data["format"]["duration"]),
        "has_audio": bool(a),
        "audio": a[0] if a else None,
        "video": v,
    }


def wait_job(editor: Editor, job_id: str, timeout: float = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = editor.get_job(job_id)
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def mean_volume(path: Path, start: float, duration: float) -> float:
    """Mean volume in dB of the audio between start and start+duration."""
    out = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostats", "-ss", str(start), "-t", str(duration),
         "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    for line in out.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].split("dB")[0])
    raise AssertionError("volumedetect produced no output:\n" + out.stderr)


def decode_rgb(jpeg: bytes) -> tuple[int, int, bytes]:
    """Decode a JPEG to raw RGB24 using ffmpeg (no imaging library needed)."""
    meta = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", "-i", "pipe:0"],
        input=jpeg, capture_output=True, check=True,
    )
    w, h = (int(x) for x in meta.stdout.decode().strip().split(",")[:2])
    raw = subprocess.run(
        [FFMPEG, "-v", "error", "-f", "image2pipe", "-c:v", "mjpeg", "-i", "pipe:0",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        input=jpeg, capture_output=True, check=True,
    ).stdout
    assert len(raw) == w * h * 3
    return w, h, raw


def region_diff(a: bytes, b: bytes, w: int, y0: int, y1: int) -> float:
    """Mean absolute per-channel difference between two RGB24 images over rows y0..y1."""
    start, end = y0 * w * 3, y1 * w * 3
    seg_a, seg_b = a[start:end], b[start:end]
    return sum(abs(x - y) for x, y in zip(seg_a, seg_b)) / max(1, len(seg_a))


# ------------------------------------------------------------------ v2 media


def _moving_colour(path: Path, colour: str, size: str, seconds: float, audio_hz: int | None) -> None:
    """A solid colour clip with a small test pattern sliding along its top edge."""
    w = int(size.split("x")[0])
    graph = (
        f"color=c={colour}:s={size}:r=30:d={seconds}[bg];"
        f"testsrc=s=96x64:r=30:d={seconds}[fg];"
        f"[bg][fg]overlay=x='mod(t*150,{w - 96})':y=8:shortest=1,format=yuv420p[v]"
    )
    args = ["-filter_complex", graph, "-map", "[v]"]
    if audio_hz:
        args = ["-f", "lavfi", "-i", f"sine=frequency={audio_hz}:sample_rate=48000:duration={seconds}",
                *args, "-map", "0:a", "-c:a", "aac", "-ac", "2"]
    run_ffmpeg(*args, "-c:v", "libx264", "-preset", "ultrafast", "-t", str(seconds), str(path))


def make_extra_media(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "red": directory / "red.mp4",       # landscape, audio
        "blue": directory / "blue.mov",     # portrait, audio
        "green": directory / "green.mp4",   # square, no audio
        "motion": directory / "motion.mp4",  # 5 s frozen frame, then 5 s of motion
        "static": directory / "static.mp4",  # no motion at all
        "mid": directory / "mid.mp4",        # muted mid-tone colours, like real footage
        "song": directory / "song.mp3",
    }
    # Not fully saturated, like real footage, so colour grading has room to act.
    _moving_colour(files["red"], "0xD04838", "640x360", 6, 330)
    _moving_colour(files["blue"], "0x3858C8", "360x640", 5, 550)
    _moving_colour(files["green"], "0x50A050", "480x480", 4, None)
    run_ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=5",
        "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=48000:duration=10",
        "-filter_complex", "[0:v]tpad=start_mode=clone:start_duration=5,format=yuv420p[v]",
        "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-t", "10", str(files["motion"]),
    )
    run_ffmpeg(
        "-f", "lavfi", "-i", "color=c=gray:s=320x240:r=30:d=6",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(files["static"]),
    )
    run_ffmpeg(
        "-f", "lavfi", "-i",
        "testsrc2=size=360x640:rate=30:duration=2,eq=contrast=0.55:saturation=0.6:brightness=0.02",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(files["mid"]),
    )
    run_ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=262:sample_rate=44100:duration=30",
        "-c:a", "libmp3lame" if _has_encoder("libmp3lame") else "mp2", "-b:a", "128k",
        str(files["song"]),
    )
    return files


def _has_encoder(name: str) -> bool:
    out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True)
    return f" {name} " in out.stdout


@pytest.fixture(scope="session")
def extra_media_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("extra_media")
    make_extra_media(d)
    return d


@pytest.fixture
def extra(workspace, extra_media_dir) -> Path:
    """Copy the v2 media into the workspace inbox too."""
    for f in extra_media_dir.iterdir():
        shutil.copy(f, workspace / "inbox" / f.name)
    return workspace


def frame_at(path: Path, time_s: float, width: int = 432, height: int = 768) -> bytes:
    """RGB24 frame of a video at `time_s`, scaled to width x height."""
    raw = subprocess.run(
        [FFMPEG, "-v", "error", "-ss", f"{time_s:.3f}", "-i", str(path), "-frames:v", "1",
         "-vf", f"scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        capture_output=True, check=True,
    ).stdout
    assert len(raw) == width * height * 3, len(raw)
    return raw


def region_mean(rgb: bytes, w: int, y0: int, y1: int, channel: int | None = None) -> float:
    """Mean value over rows y0..y1, for one channel (0=R,1=G,2=B) or all."""
    seg = rgb[y0 * w * 3:y1 * w * 3]
    if channel is not None:
        seg = seg[channel::3]
    return sum(seg) / max(1, len(seg))
