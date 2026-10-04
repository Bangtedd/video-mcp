"""suggest_segments: motion scoring, determinism, bounds and fallbacks."""

from __future__ import annotations

import shutil

import pytest

from video_mcp.config import Config
from video_mcp.editor import Editor
from video_mcp.errors import VideoMCPError


def check_windows(res, duration, length, count):
    segs = res["segments"]
    assert 1 <= len(segs) <= count
    for s in segs:
        assert s["end"] - s["start"] == pytest.approx(min(length, duration), abs=1e-3)
        assert 0 <= s["start"] < s["end"] <= duration + 1e-3
    for a, b in zip(segs, segs[1:]):
        assert a["end"] <= b["start"] + 1e-6, "windows overlap"
    return segs


def test_prefers_motion_and_skips_edges(editor, extra):
    # motion.mp4: 5 s of a frozen frame, then 5 s of moving test pattern.
    res = editor.suggest_segments("motion.mp4", 2.0, 1)
    assert res["method"] == "motion"
    (seg,) = check_windows(res, 10, 2.0, 1)
    assert seg["start"] >= 4.5, seg        # (almost) all in the moving half
    assert seg["end"] <= 10 - 0.5           # last 0.5 s skipped

    res = editor.suggest_segments("motion.mp4", 1.5, 3)
    segs = check_windows(res, 10, 1.5, 3)
    assert len(segs) == 3
    assert all(s["start"] >= 0.5 and s["end"] <= 9.5 for s in segs)
    # The best-scoring windows are the moving ones; any static pick scores lowest.
    moving = [s for s in segs if s["start"] >= 4.5]
    assert len(moving) >= 2
    assert all(s["score"] > 0 for s in moving)


def test_fits_as_many_windows_as_possible(editor, extra):
    # 0.5..5.5 usable on the 6 s clip: two 2.4 s windows only fit side by side.
    segs = check_windows(editor.suggest_segments("red.mp4", 2.4, 2), 6, 2.4, 2)
    assert len(segs) == 2
    segs = check_windows(editor.suggest_segments("motion.mp4", 2.0, 10), 10, 2.0, 10)
    assert len(segs) == 4  # 9 usable seconds hold four 2 s windows


def test_deterministic_and_cached(editor, extra):
    first = editor.suggest_segments("red.mp4", 1.0, 3)
    again = editor.suggest_segments("red.mp4", 1.0, 3)
    assert first == again
    cache = list((editor.ws.cache / "segments").glob("*.json"))
    assert cache
    # Recomputing from scratch gives exactly the same answer.
    shutil.rmtree(editor.ws.cache / "segments")
    assert editor.suggest_segments("red.mp4", 1.0, 3) == first
    check_windows(first, 6, 1.0, 3)


def test_short_file_falls_back_to_even_windows(editor):
    # silent.mp4 is 3 s: 2.5 s windows can't skip 0.5 s at both ends.
    res = editor.suggest_segments("silent.mp4", 2.5, 2)
    assert res["method"] == "even"
    (seg,) = check_windows(res, 3, 2.5, 2)
    assert seg == {"start": 0.25, "end": 2.75}
    # Longer than the file: the whole file.
    res = editor.suggest_segments("silent.mp4", 5, 1)
    assert res["segments"] == [{"start": 0.0, "end": 3.0}]


def test_static_file_falls_back_to_even_windows(editor, extra):
    res = editor.suggest_segments("static.mp4", 1.0, 3)
    assert res["method"] == "even"
    segs = check_windows(res, 6, 1.0, 3)
    assert len(segs) == 3
    gaps = [b["start"] - a["start"] for a, b in zip(segs, segs[1:])]
    assert gaps[0] == pytest.approx(gaps[1], abs=1e-3)


def test_failed_analysis_falls_back_to_even_windows(workspace, job_manager, extra):
    # ffprobe works but the ffmpeg used for analysis is missing.
    ed = Editor(Config(workspace_dir=workspace, ffmpeg=str(workspace / "no-ffmpeg")), jobs=job_manager)
    res = ed.suggest_segments("motion.mp4", 2.0, 3)
    assert res["method"] == "even"
    segs = check_windows(res, 10, 2.0, 3)
    assert len(segs) == 3
    assert segs[0]["start"] >= 0.5 and segs[-1]["end"] <= 9.5
    assert not (workspace / "cache" / "segments").exists() or not list(
        (workspace / "cache" / "segments").glob("*.json")
    )  # failures are not cached


def test_validation(editor):
    with pytest.raises(VideoMCPError, match="count"):
        editor.suggest_segments("landscape.mp4", 1, 0)
    with pytest.raises(VideoMCPError, match="length"):
        editor.suggest_segments("landscape.mp4", 0, 1)
    with pytest.raises(VideoMCPError, match="no video"):
        editor.suggest_segments("music.m4a", 1, 1)
    with pytest.raises(VideoMCPError, match=r"\.\."):
        editor.suggest_segments("../x.mp4", 1, 1)
