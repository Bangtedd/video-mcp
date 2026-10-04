"""End-to-end renders with real FFmpeg, verified with ffprobe."""

from __future__ import annotations

import time

import pytest

from conftest import decode_rgb, mean_volume, probe, wait_job

TOL = 0.2


def render_ok(editor, project, quality="preview"):
    job = editor.render(project, quality)
    result = wait_job(editor, job["job_id"])
    assert result["status"] == "done", result
    assert result["progress"] == 100
    path = editor.ws.root / result["output"]
    assert path.is_file()
    return path, result


def test_mixed_orientation_concat(editor):
    editor.create_project("mixed", "vertical", "crop")
    editor.add_clip("mixed", "landscape.mp4")
    editor.add_clip("mixed", "portrait.mp4")
    editor.add_clip("mixed", "rotated.mp4")
    path, job = render_ok(editor, "mixed", "preview")
    info = probe(path)
    assert (info["width"], info["height"]) == (540, 960)  # preview = half of 1080x1920
    assert info["duration"] == pytest.approx(12, abs=TOL)
    assert info["has_audio"]
    assert int(info["audio"]["sample_rate"]) == 48000
    assert int(info["audio"]["channels"]) == 2
    assert info["video"]["pix_fmt"] == "yuv420p"
    assert info["video"]["avg_frame_rate"] == "30/1"
    assert info["video"].get("sample_aspect_ratio", "1:1") in ("1:1", "N/A")


def test_landscape_pad_adds_black_bars(editor):
    editor.create_project("padded", "landscape", "pad")
    editor.add_clip("padded", "portrait.mp4", 0, 1)
    path, _ = render_ok(editor, "padded")
    info = probe(path)
    assert (info["width"], info["height"]) == (960, 540)
    w, h, rgb = decode_rgb(editor.get_frame(f"renders/{path.name}", 0.5))
    assert (w, h) == (768, 432)
    # Leftmost columns are pillarbox (black); the centre is the colourful test pattern.
    left = rgb[(h // 2 * w + 5) * 3:(h // 2 * w + 5) * 3 + 3]
    assert max(left) < 30
    centre = rgb[(h // 2 * w + w // 2) * 3:(h // 2 * w + w // 2) * 3 + 3]
    assert max(centre) > 60


def test_trim(editor):
    editor.create_project("trim", "square")
    editor.add_clip("trim", "landscape.mp4", 1.0, 2.5)
    path, _ = render_ok(editor, "trim")
    info = probe(path)
    assert (info["width"], info["height"]) == (540, 540)
    assert info["duration"] == pytest.approx(1.5, abs=TOL)


@pytest.mark.parametrize(
    "start,end,speed,expected",
    [(0, 4, 2.0, 2.0), (0, 1, 0.25, 4.0), (0, 4, 4.0, 1.0), (1, 3, 0.75, 2.6667)],
)
def test_speed_change(editor, start, end, speed, expected):
    name = f"speed{int(speed * 100)}"
    editor.create_project(name, "landscape")
    cid = editor.add_clip(name, "landscape.mp4", start, end)["clip"]["id"]
    editor.update_clip(name, cid, speed=speed)
    assert editor.get_project(name)["output_duration"] == pytest.approx(expected, abs=0.04)
    path, _ = render_ok(editor, name)
    info = probe(path)
    assert info["duration"] == pytest.approx(expected, abs=TOL)
    assert float(info["audio"]["duration"]) == pytest.approx(expected, abs=TOL)
    # Sped-up audio is still audible (atempo, not dropped).
    assert mean_volume(path, 0.1, expected - 0.2) > -40


def test_silent_clip_in_middle(editor):
    editor.create_project("gap", "vertical")
    editor.add_clip("gap", "landscape.mp4")
    editor.add_clip("gap", "silent.mp4")
    editor.add_clip("gap", "portrait.mp4")
    path, _ = render_ok(editor, "gap")
    info = probe(path)
    assert info["duration"] == pytest.approx(11, abs=TOL)
    assert float(info["audio"]["duration"]) == pytest.approx(11, abs=TOL)
    assert mean_volume(path, 0.5, 3) > -40      # landscape sine
    assert mean_volume(path, 4.5, 2) < -80      # generated silence
    assert mean_volume(path, 7.5, 3) > -40      # portrait sine


def test_clip_volume(editor):
    editor.create_project("vol", "square")
    cid = editor.add_clip("vol", "landscape.mp4", 0, 2)["clip"]["id"]
    loud, _ = render_ok(editor, "vol")
    editor.update_clip("vol", cid, volume=0.25)
    quiet, _ = render_ok(editor, "vol")
    editor.update_clip("vol", cid, volume=0.0)
    mute, _ = render_ok(editor, "vol")
    assert mean_volume(loud, 0.2, 1.5) - mean_volume(quiet, 0.2, 1.5) == pytest.approx(12, abs=1.5)
    assert mean_volume(mute, 0.2, 1.5) < -80


def test_music_mix_and_fade(editor):
    # Music under a silent clip: we hear only the music, with fades.
    editor.create_project("music", "square")
    editor.add_clip("music", "silent.mp4")  # 3 s
    editor.set_music("music", "music.m4a", volume=1.0, offset=2.0, fade_in=1.0, fade_out=1.0)
    path, _ = render_ok(editor, "music")
    info = probe(path)
    assert info["duration"] == pytest.approx(3, abs=TOL)  # 20 s music never extends video
    assert float(info["audio"]["duration"]) == pytest.approx(3, abs=TOL)
    start, middle, end = mean_volume(path, 0, 0.25), mean_volume(path, 1.2, 0.6), mean_volume(path, 2.7, 0.25)
    assert middle > -30
    assert start < middle - 6
    assert end < middle - 6


def test_music_does_not_duck_clip_audio(editor):
    editor.create_project("duck", "square")
    editor.add_clip("duck", "landscape.mp4", 0, 2)
    plain, _ = render_ok(editor, "duck")
    editor.set_music("duck", "music.m4a", volume=0.3)
    mixed, _ = render_ok(editor, "duck")
    # amix with normalize=0: adding music must not lower the clip level.
    assert mean_volume(mixed, 0.2, 1.5) >= mean_volume(plain, 0.2, 1.5) - 0.5


def test_music_shorter_than_video(editor):
    editor.create_project("short", "square")
    editor.add_clip("short", "landscape.mp4")  # 4 s
    editor.set_music("short", "music.m4a", volume=0.5, offset=18.5, fade_out=2)  # 1.5 s left
    path, _ = render_ok(editor, "short")
    info = probe(path)
    assert info["duration"] == pytest.approx(4, abs=TOL)
    assert float(info["audio"]["duration"]) == pytest.approx(4, abs=TOL)


AWKWARD = [
    "Time: 10:30",
    "it's Bob's \"quote\"",
    "100% done %{pts} %%",
    "back\\slash \\n not a newline \\",
    "emoji 🎉🔥 ok",
    "multi\nline\ntext",
    "[brackets];semi,comma=eq",
]


def test_text_with_awkward_characters(editor):
    editor.create_project("txt", "vertical")
    editor.add_clip("txt", "landscape.mp4")
    editor.add_clip("txt", "silent.mp4")
    positions = ["top", "center", "bottom"]
    sizes = ["small", "medium", "large"]
    for i, text in enumerate(AWKWARD):
        editor.add_text(
            "txt", text, i * 0.9, i * 0.9 + 1.2,
            position=positions[i % 3], size=sizes[i % 3],
            color=["white", "#ffcc00", "red"][i % 3], box=bool(i % 2),
        )
    stored = [t["text"] for t in editor.get_project("txt")["texts"]]
    assert stored == AWKWARD  # stored verbatim
    path, _ = render_ok(editor, "txt")
    info = probe(path)
    assert info["duration"] == pytest.approx(7, abs=TOL)


def test_final_quality_settings(editor):
    editor.create_project("fin", "vertical")
    editor.add_clip("fin", "portrait.mp4", 0, 1.5)
    path, _ = render_ok(editor, "fin", "final")
    info = probe(path)
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["video"]["codec_name"] == "h264"
    assert info["audio"]["codec_name"] == "aac"
    data = path.read_bytes()
    assert data.find(b"moov") < data.find(b"mdat")  # +faststart


def test_render_job_lifecycle(editor):
    editor.create_project("life", "landscape")
    editor.add_clip("life", "landscape.mp4")
    editor.add_clip("life", "portrait.mp4")
    first = editor.render("life", "final")
    second = editor.render("life", "preview")
    assert first["status"] in ("queued", "running")
    assert editor.get_job(second["job_id"])["status"] == "queued"

    seen, progress = set(), []
    deadline = time.time() + 300
    while time.time() < deadline:
        j1 = editor.get_job(first["job_id"])
        j2 = editor.get_job(second["job_id"])
        seen.add((j1["status"], j2["status"]))
        progress.append(j1["progress"])
        assert not (j1["status"] == "running" and j2["status"] == "running")
        if j2["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert j1["status"] == "done" and j2["status"] == "done", (j1, j2)
    assert ("running", "queued") in seen
    assert progress == sorted(progress)  # monotonic
    assert any(0 < p < 100 for p in progress)
    assert j1["progress"] == 100 and j1["output"].startswith("renders/")
    assert j2["started_at"] >= j1["finished_at"]  # one render at a time
    assert j1["output"] != j2["output"]
    with pytest.raises(Exception, match="No job"):
        editor.get_job("doesnotexist")


def test_failed_render_reports_stderr(editor):
    editor.create_project("broken", "square")
    editor.add_clip("broken", "landscape.mp4")
    # Corrupt the source after it was added: FFmpeg itself must fail.
    (editor.ws.inbox / "landscape.mp4").write_bytes(b"this is not a video" * 1000)
    job = editor.render("broken", "preview")
    result = wait_job(editor, job["job_id"])
    assert result["status"] == "failed"
    assert result["output"] is None
    assert "FFmpeg exited" in result["error"]
    lines = result["stderr"].splitlines()
    assert 0 < len(lines) <= 20
    assert "Invalid data" in result["stderr"] or "invalid" in result["stderr"].lower()
    assert not list(editor.ws.renders.glob("broken-*"))  # no partial output left behind


def test_missing_source_rejected_at_submit(editor):
    editor.create_project("gone", "square")
    editor.add_clip("gone", "landscape.mp4")
    (editor.ws.inbox / "landscape.mp4").unlink()
    with pytest.raises(Exception, match="not found"):
        editor.render("gone")


def test_awkward_workspace_and_font_paths(tmp_path, media_dir, job_manager):
    """Paths spliced into the filter graph (textfile, fontfile) are escaped correctly."""
    import shutil

    from video_mcp.config import DEFAULT_FONT, Config
    from video_mcp.editor import Editor

    ws = tmp_path / "my work: it's [v1],x;y"
    (ws / "inbox").mkdir(parents=True)
    shutil.copy(media_dir / "landscape.mp4", ws / "inbox" / "a clip's: name.mp4")
    font_dir = tmp_path / "fonts: o'k"
    font_dir.mkdir()
    shutil.copy(DEFAULT_FONT, font_dir / "Font, Bold.ttf")
    ed = Editor(
        Config(workspace_dir=ws, font_file=str(font_dir / "Font, Bold.ttf")), jobs=job_manager
    )
    ed.create_project("esc", "square")
    ed.add_clip("esc", "a clip's: name.mp4", 0, 1)
    ed.add_text("esc", "hi: there", 0, 1, box=True)
    path, _ = render_ok(ed, "esc")
    assert probe(path)["duration"] == pytest.approx(1, abs=TOL)
