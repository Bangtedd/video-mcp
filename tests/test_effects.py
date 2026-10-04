"""Transitions, looks, gradients, fades and text fades, rendered with real FFmpeg."""

from __future__ import annotations

import pytest

from conftest import frame_at, mean_volume, probe, region_diff, region_mean, wait_job
from video_mcp.errors import VideoMCPError
from video_mcp.timeline import LOOKS

TOL = 0.2


def render_ok(editor, project, quality="preview"):
    job = editor.render(project, quality)
    result = wait_job(editor, job["job_id"])
    assert result["status"] == "done", result
    return editor.ws.root / result["output"]


# -------------------------------------------------------------- transitions


@pytest.mark.parametrize("kind", ["fade", "slide_left", "slide_up", "zoom"])
def test_transition_duration_is_sum_minus_overlaps(editor, kind):
    name = f"tr-{kind}"
    editor.create_project(name, "vertical")
    editor.add_clip(name, "landscape.mp4")   # 4 s
    editor.add_clip(name, "silent.mp4")      # 3 s, no audio
    editor.add_clip(name, "portrait.mp4")    # 4 s
    assert editor.get_project(name)["output_duration"] == pytest.approx(11)
    editor.set_transition(name, kind, 0.5)
    proj = editor.get_project(name)
    assert proj["output_duration"] == pytest.approx(11 - 2 * 0.5)
    assert [t["overlap"] for t in proj["transitions"]] == [0.5, 0.5]
    assert [c["timeline_start"] for c in proj["clips"]] == [0, 3.5, 6.0]
    path = render_ok(editor, name)
    info = probe(path)
    assert info["duration"] == pytest.approx(10, abs=TOL)
    assert float(info["audio"]["duration"]) == pytest.approx(10, abs=TOL)


def test_long_fade_transition(editor):
    editor.create_project("tr-long", "square")
    editor.add_clip("tr-long", "landscape.mp4")
    editor.add_clip("tr-long", "portrait.mp4")
    editor.set_transition("tr-long", "fade", 1.0)
    assert editor.get_project("tr-long")["output_duration"] == pytest.approx(7)
    path = render_ok(editor, "tr-long")
    assert probe(path)["duration"] == pytest.approx(7, abs=TOL)
    # Half way through the dissolve the frame differs from both neighbours.
    mid, before, after = frame_at(path, 3.5, 192, 192), frame_at(path, 2.0, 192, 192), frame_at(path, 5.0, 192, 192)
    assert region_diff(mid, before, 192, 0, 192) > 5
    assert region_diff(mid, after, 192, 0, 192) > 5


def test_short_clip_falls_back_to_cut(editor):
    editor.create_project("tr-short", "vertical")
    editor.add_clip("tr-short", "landscape.mp4")              # 4 s
    editor.add_clip("tr-short", "portrait.mp4")               # 4 s
    editor.add_clip("tr-short", "silent.mp4", 0, 0.8)         # 0.8 s < 2 x 0.5
    editor.add_clip("tr-short", "landscape.mp4", 0, 2)        # 2 s
    editor.set_transition("tr-short", "slide_left", 0.5)
    proj = editor.get_project("tr-short")
    assert [t["type"] for t in proj["transitions"]] == ["slide_left", "cut", "cut"]
    assert proj["output_duration"] == pytest.approx(4 + 4 + 0.8 + 2 - 0.5)
    path = render_ok(editor, "tr-short")
    assert probe(path)["duration"] == pytest.approx(10.3, abs=TOL)


def test_transitions_shrink_the_room_for_text(editor):
    editor.create_project("tr-text", "square")
    editor.add_clip("tr-text", "landscape.mp4")
    editor.add_clip("tr-text", "portrait.mp4")
    editor.add_text("tr-text", "end card", 7, 8)
    editor.set_transition("tr-text", "fade", 1.0)  # timeline is now 7 s
    proj = editor.get_project("tr-text")
    assert proj["problems"] and "ends at 8" in proj["problems"][0]
    with pytest.raises(VideoMCPError, match="Cannot render"):
        editor.render("tr-text")


def test_effect_validation(editor):
    editor.create_project("val")
    with pytest.raises(VideoMCPError, match="between 0.2 and 1"):
        editor.set_transition("val", "fade", 0.1)
    with pytest.raises(VideoMCPError, match="type must be one of"):
        editor.set_transition("val", "spin", 0.5)
    with pytest.raises(VideoMCPError, match="look must be one of"):
        editor.set_look("val", "sepia")
    with pytest.raises(VideoMCPError, match="gradient must be one of"):
        editor.set_gradient("val", "left")
    with pytest.raises(VideoMCPError, match="between 0 and 2"):
        editor.set_fades("val", 3, 0)


def test_old_projects_without_v2_fields_still_work(editor):
    import json

    editor.create_project("old", "square")
    editor.add_clip("old", "landscape.mp4", 0, 1)
    path = editor.ws.project_path("old")
    data = json.loads(path.read_text())
    for key in ("transition", "look", "gradient", "fade_in", "fade_out"):
        del data[key]
    path.write_text(json.dumps(data))
    proj = editor.get_project("old")
    assert proj["transition"]["type"] == "cut" and proj["look"] == "none"
    assert probe(render_ok(editor, "old"))["duration"] == pytest.approx(1, abs=TOL)


# -------------------------------------------------------------------- looks


@pytest.fixture
def plain(editor, extra):
    editor.create_project("plain", "square")
    editor.add_clip("plain", "mid.mp4", 0, 1)
    return frame_at(render_ok(editor, "plain"), 0.5, 270, 270)


@pytest.mark.parametrize("look", [x for x in LOOKS if x != "none"])
def test_each_look_changes_the_picture(editor, plain, look):
    editor.create_project("looked", "square")
    editor.add_clip("looked", "mid.mp4", 0, 1)
    editor.set_look("looked", look)
    graded = frame_at(render_ok(editor, "looked"), 0.5, 270, 270)
    assert region_diff(graded, plain, 270, 0, 270) > 3, look
    if look == "bw":
        assert max(abs(graded[i] - graded[i + 2]) for i in range(0, len(graded), 3)) < 12
    if look == "warm":
        assert region_mean(graded, 270, 0, 270, 0) - region_mean(graded, 270, 0, 270, 2) > \
            region_mean(plain, 270, 0, 270, 0) - region_mean(plain, 270, 0, 270, 2) + 5
    if look == "cool":
        assert region_mean(graded, 270, 0, 270, 2) - region_mean(graded, 270, 0, 270, 0) > \
            region_mean(plain, 270, 0, 270, 2) - region_mean(plain, 270, 0, 270, 0) + 5
    if look == "moody":
        assert region_mean(graded, 270, 0, 270) < region_mean(plain, 270, 0, 270) - 5


# ---------------------------------------------------------------- gradients


@pytest.mark.parametrize("gradient", ["bottom", "top", "both"])
def test_each_gradient_darkens_its_edge(editor, plain, gradient):
    editor.create_project("grad", "square")
    editor.add_clip("grad", "mid.mp4", 0, 1)
    editor.set_gradient("grad", gradient)
    shaded = frame_at(render_ok(editor, "grad"), 0.5, 270, 270)
    top, bottom = (0, 50), (220, 270)
    darker = lambda rows: region_mean(plain, 270, *rows) - region_mean(shaded, 270, *rows)
    assert darker(top) > 20 if gradient in ("top", "both") else abs(darker(top)) < 2
    assert darker(bottom) > 20 if gradient in ("bottom", "both") else abs(darker(bottom)) < 2
    # The middle of the frame (outside the 35% strips) is untouched.
    assert region_diff(shaded, plain, 270, 100, 170) < 2
    assert list((editor.ws.cache / "gradients").glob("*.png"))


# -------------------------------------------------------------------- fades


def test_intro_and_outro_fades(editor):
    editor.create_project("fades", "square")
    editor.add_clip("fades", "landscape.mp4", 0, 3)
    editor.set_fades("fades", 1.0, 1.0)
    path = render_ok(editor, "fades")
    assert probe(path)["duration"] == pytest.approx(3, abs=TOL)
    assert region_mean(frame_at(path, 0.02, 160, 160), 160, 0, 160) < 15
    assert region_mean(frame_at(path, 1.5, 160, 160), 160, 0, 160) > 60
    assert region_mean(frame_at(path, 2.95, 160, 160), 160, 0, 160) < 20
    middle = mean_volume(path, 1.2, 0.6)
    assert mean_volume(path, 0, 0.15) < middle - 10
    assert mean_volume(path, 2.85, 0.15) < middle - 10


def test_text_fade(editor, extra):
    editor.create_project("tfade", "square")
    editor.add_clip("tfade", "static.mp4", 0, 2)  # plain grey, so frames compare cleanly
    ref0 = frame_at(render_ok(editor, "tfade"), 0.05, 270, 270)
    editor.add_text("tfade", "FADING TEXT", 0, 2, position="center", size="large",
                    color="white", box=True, fade=True)
    assert editor.get_project("tfade")["texts"][0]["fade"] is True
    path = render_ok(editor, "tfade")
    start = frame_at(path, 0.02, 270, 270)
    middle = frame_at(path, 1.0, 270, 270)
    band = (110, 160)
    assert region_diff(middle, ref0, 270, *band) > 20         # fully visible
    assert region_diff(start, ref0, 270, *band) < region_diff(middle, ref0, 270, *band) / 3
    editor.update_text("tfade", "t1", fade=False)
    hard = frame_at(render_ok(editor, "tfade"), 0.02, 270, 270)
    assert region_diff(hard, ref0, 270, *band) > 20           # no fade: visible at once
