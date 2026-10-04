"""Pick the most lively windows of a clip.

The clip is decoded once at 2 fps and 160 px wide, and FFmpeg's scene-change
score between consecutive samples serves as a motion measure. Scores are cached
per file in cache/segments/, so picking windows of other lengths is instant.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path

ANALYSIS_FPS = 2
ANALYSIS_WIDTH = 160
EDGE_SKIP = 0.5          # never start in the first / end in the last 0.5 s
STEP = 0.25              # candidate window starts every 0.25 s
STATIC_SCORE = 0.004     # per-sample scene score below this counts as near-static
MIN_MOTION = 1e-4        # a whole file below this is treated as static -> even spacing
CACHE_VERSION = 1

_PTS_RE = re.compile(r"pts_time:\s*([0-9.]+)")
_SCORE_RE = re.compile(r"lavfi\.scene_score=([0-9.]+)")


def _cache_path(cache_dir: Path, path: Path) -> Path:
    st = path.stat()
    key = f"{CACHE_VERSION}|{path}|{st.st_mtime_ns}|{st.st_size}"
    return cache_dir / "segments" / (hashlib.sha1(key.encode()).hexdigest() + ".json")


def analyze(ffmpeg: str, path: Path, timeout: float = 900) -> list[tuple[float, float]]:
    """(time, scene score) samples at 2 fps. Raises RuntimeError if FFmpeg fails."""
    vf = (
        f"fps={ANALYSIS_FPS},scale={ANALYSIS_WIDTH}:-2,"
        "select=gte(scene\\,0),metadata=print"
    )
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "info", "-i", str(path),
             "-an", "-sn", "-dn", "-vf", vf, "-f", "null", "-"],
            capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"analysis failed: {exc}") from None
    if proc.returncode != 0:
        raise RuntimeError(f"analysis failed: ffmpeg exited with {proc.returncode}")
    samples: list[tuple[float, float]] = []
    pending_t: float | None = None
    for line in proc.stderr.decode("utf-8", "replace").splitlines():
        m = _PTS_RE.search(line)
        if m:
            pending_t = float(m.group(1))
            continue
        m = _SCORE_RE.search(line)
        if m and pending_t is not None:
            score = float(m.group(1))
            samples.append((round(pending_t, 3), round(score if math.isfinite(score) else 0.0, 5)))
            pending_t = None
    if len(samples) < 2:
        raise RuntimeError("analysis produced no motion samples")
    return samples


def cached_analysis(ffmpeg: str, cache_dir: Path, path: Path) -> list[tuple[float, float]]:
    cpath = _cache_path(cache_dir, path)
    try:
        data = json.loads(cpath.read_text())
        return [(float(t), float(s)) for t, s in data["samples"]]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    samples = analyze(ffmpeg, path)
    cpath.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=cpath.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump({"file": str(path), "samples": samples}, f)
    os.replace(tmp, cpath)
    return samples


def usable_span(duration: float, length: float) -> tuple[float, float]:
    """Where windows may lie: skip 0.5 s at both ends unless the file is too short."""
    if duration - 2 * EDGE_SKIP >= length:
        return EDGE_SKIP, duration - EDGE_SKIP
    return 0.0, duration


def even_windows(duration: float, length: float, count: int) -> list[tuple[float, float]]:
    """Evenly spaced, non-overlapping windows; fewer than `count` if they don't fit.
    A file shorter than `length` yields one window covering the whole file."""
    lo, hi = usable_span(duration, length)
    span = hi - lo
    if span <= length:
        start = lo + max(0.0, (span - length) / 2)
        return [(round(start, 3), round(min(hi, start + length), 3))]
    k = max(1, min(count, int(span // length)))
    gap = (span - k * length) / k
    return [
        (round(lo + gap / 2 + i * (length + gap), 3),
         round(lo + gap / 2 + i * (length + gap) + length, 3))
        for i in range(k)
    ]


def window_score(samples: list[tuple[float, float]], start: float, end: float) -> float:
    # A sample's score is the change since the previous sample, so it belongs to
    # the window if its time is inside (start, end].
    inside = [s for t, s in samples if start < t <= end + 1e-6]
    if not inside:
        return 0.0
    mean = sum(inside) / len(inside)
    active = sum(1 for s in inside if s > STATIC_SCORE) / len(inside)
    return mean * (0.25 + 0.75 * active)


def pick_windows(
    samples: list[tuple[float, float]], duration: float, length: float, count: int
) -> list[tuple[float, float, float]] | None:
    """Best `count` non-overlapping windows by motion, as (start, end, score) in
    time order. None if the file is too short or static (caller falls back)."""
    lo, hi = usable_span(duration, length)
    if lo == 0.0 or max((s for _, s in samples), default=0) < MIN_MOTION:
        return None
    n = int(math.floor((hi - length - lo) / STEP + 1e-9)) + 1
    starts = [round(lo + i * STEP, 3) for i in range(n)]
    scores = [round(window_score(samples, s, s + length), 6) for s in starts]
    # Next candidate that doesn't overlap a window starting at index i.
    gap = int(math.ceil(length / STEP - 1e-9))
    k = min(count, (n - 1) // gap + 1)
    # best[i][j]: highest total score of j windows using candidates i.. (exact DP,
    # so as many windows as fit are always found; ties favour earlier windows).
    NEG = float("-inf")
    best = [[NEG] * (k + 1) for _ in range(n + gap + 1)]
    for i in range(n + gap, -1, -1):
        best[i][0] = 0.0
    for i in range(n - 1, -1, -1):
        for j in range(1, k + 1):
            take = scores[i] + best[i + gap][j - 1] if best[i + gap][j - 1] > NEG else NEG
            best[i][j] = max(take, best[i + 1][j])
    chosen: list[tuple[float, float, float]] = []
    i, j = 0, k
    while j > 0 and i < n:
        take = scores[i] + best[i + gap][j - 1] if best[i + gap][j - 1] > NEG else NEG
        if take >= best[i + 1][j] - 1e-12:
            chosen.append((starts[i], round(starts[i] + length, 3), scores[i]))
            i, j = i + gap, j - 1
        else:
            i += 1
    return chosen
