"""Templates: loading, apply_template rules, and real renders of all seven."""

from __future__ import annotations

import json

import pytest

from conftest import probe, wait_job
from video_mcp.errors import VideoMCPError
from video_mcp.templates import validate

TEMPLATE_IDS = [
    "clean_cuts", "warm_promo", "fast_hype", "cinematic", "bw_mood", "product_showcase", "story_vlog",
]
# 8 distinct inputs with mixed orientation, frame rates, rotation and audio.
EIGHT = [
    "red.mp4", "blue.mov", "green.mp4", "landscape.mp4",
    "portrait.mp4", "silent.mp4", "rotated.mp4", "motion.mp4",
]
SAMPLE_TEXTS = {
    "title": "Hello: it's 100%", "cta": "Visit us today", "caption": "A day out",
    "product": "Handmade mug", "price": "$24",
}


def expected_duration(t: dict, durations: list[float]) -> float:
    """Independent model of the output length: each clip is its slot (or the
    whole clip if shorter), and each transition overlaps two long-enough clips."""
    slots = t["slots"]
    n = max(len(slots), len(durations))
    frames = [round(min(slots[k % len(slots)], durations[k % len(durations)]) * 30) for k in range(n)]
    total = sum(frames)
    if t["transition"]["type"] != "cut":
        d = round(t["transition"]["duration"] * 30)
        total -= sum(d for a, b in zip(frames, frames[1:]) if a >= 2 * d and b >= 2 * d)
    return total / 30


def test_seven_templates_ship(editor):
    templates = {t["id"]: t for t in editor.list_templates()["templates"]}
    assert set(templates) == set(TEMPLATE_IDS)
    feel = {
        "clean_cuts": ("none", "cut"), "warm_promo": ("warm", "fade"),
        "fast_hype": ("vivid", "cut"), "cinematic": ("film", "fade"),
        "bw_mood": ("bw", "slide_left"), "product_showcase": ("moody", None),
        "story_vlog": ("cool", "zoom"),
    }
    for tid, (look, tr) in feel.items():
        assert templates[tid]["look"] == look
        if tr:
            assert templates[tid]["transition"]["type"] == tr
    assert set(templates["clean_cuts"]["slots"]) == {2}
    assert all(0.8 <= s <= 1.2 for s in templates["fast_hype"]["slots"])
    assert all(3 <= s <= 4 for s in templates["cinematic"]["slots"])
    assert set(templates["product_showcase"]["slots"]) == {2.5}
    assert all(4 <= s <= 5 for s in templates["story_vlog"]["slots"])
    assert templates["warm_promo"]["gradient"] == "bottom"
    assert templates["bw_mood"]["gradient"] == "both"
    assert {f["key"] for f in templates["product_showcase"]["texts"]} == {"product", "price", "cta"}
    assert templates["story_vlog"]["clip_volume"] >= 0.9 and templates["story_vlog"]["music"]["volume"] <= 0.2


def test_template_validation_messages():
    base = {"id": "x", "name": "X", "slots": [1]}
    assert validate(base)["transition"]["type"] == "cut"
    with pytest.raises(VideoMCPError, match="missing field 'slots'"):
        validate({"id": "x", "name": "X"})
    with pytest.raises(VideoMCPError, match="look must be one of"):
        validate(base | {"look": "sepia"})
    with pytest.raises(VideoMCPError, match="exactly one of from_start or from_end"):
        validate(base | {"texts": [{"key": "a", "duration": 1}]})
    with pytest.raises(VideoMCPError, match="Invalid template id"):
        validate(base | {"id": "../x"})


def test_broken_template_is_reported_not_fatal(workspace, job_manager, tmp_path):
    import shutil

    from video_mcp.config import DEFAULT_TEMPLATES, Config
    from video_mcp.editor import Editor

    tdir = tmp_path / "templates"
    shutil.copytree(DEFAULT_TEMPLATES, tdir)
    (tdir / "broken.json").write_text('{"id": "broken", "name": "B", "slots": [1], "look": "sepia"}')
    (tdir / "garbage.json").write_text("{not json")
    ed = Editor(Config(workspace_dir=workspace, templates_dir=tdir), jobs=job_manager)
    res = ed.list_templates()
    assert res["count"] == 7
    assert any("broken.json" in e and "look" in e for e in res["errors"])
    assert any("garbage.json" in e for e in res["errors"])


@pytest.mark.parametrize("music", [False, True], ids=["no-music", "music"])
@pytest.mark.parametrize("n_clips", [1, 3, 8])
@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_apply_template_renders(editor, extra, template_id, n_clips, music):
    t = editor.get_template(template_id)
    files = EIGHT[:n_clips]
    durations = [editor.probe_media(f)["duration"] for f in files]
    args = {"music": "song.mp3"} if music else {}
    res = editor.apply_template("tpl", template_id, files, SAMPLE_TEXTS_FOR(t), **args)
    proj = res["project"]
    assert len(proj["clips"]) == max(len(t["slots"]), n_clips)
    assert proj["look"] == t["look"] and proj["gradient"] == t["gradient"]
    assert proj["transition"] == t["transition"]
    assert (proj["music"] is not None) == music
    if music:
        assert proj["music"]["volume"] == t["music"]["volume"]
    assert all(c["volume"] == t["clip_volume"] for c in proj["clips"])
    assert len(proj["texts"]) == len(t["texts"])
    want = expected_duration(t, durations)
    assert proj["output_duration"] == pytest.approx(want, abs=0.04)

    job = editor.render("tpl", "preview")
    result = wait_job(editor, job["job_id"])
    assert result["status"] == "done", result
    info = probe(editor.ws.root / result["output"])
    assert (info["width"], info["height"]) == (540, 960)
    assert info["duration"] == pytest.approx(want, abs=0.2)
    assert info["has_audio"]


def SAMPLE_TEXTS_FOR(t):
    return {f["key"]: SAMPLE_TEXTS[f["key"]] for f in t["texts"]}


def test_fewer_files_than_slots_use_different_segments(editor, extra):
    # motion.mp4 is used for slots 1, 3 and 5 (2 s each): three separate windows fit.
    res = editor.apply_template("cyc", "clean_cuts", ["motion.mp4", "red.mp4"], {"caption": "hi"})
    clips = res["project"]["clips"]
    assert len(clips) == 5
    motion = sorted((c["start"], c["end"]) for c in clips if c["file"].endswith("motion.mp4"))
    assert len(motion) == 3
    for (s1, e1), (s2, e2) in zip(motion, motion[1:]):
        assert e1 <= s2 + 1e-6, "segments of the same file should not overlap"
    assert all(e - s == pytest.approx(2) and s >= 0.5 and e <= 9.5 for s, e in motion)
    # One file for five slots: only four 2 s windows fit, the fifth use still differs.
    res = editor.apply_template("cyc", "clean_cuts", ["motion.mp4"], {"caption": "hi"})
    spans = [(c["start"], c["end"]) for c in res["project"]["clips"]]
    assert len(set(spans)) == 5


def test_more_files_than_slots_repeat_the_pattern(editor, extra):
    t = editor.get_template("warm_promo")  # 4 slots
    res = editor.apply_template("rep", "warm_promo", EIGHT[:6], {})
    clips = res["project"]["clips"]
    assert [c["file"].split("/")[-1] for c in clips] == EIGHT[:6]
    for k, c in enumerate(clips):
        want = min(t["slots"][k % 4], c["source_duration"])
        assert c["end"] - c["start"] == pytest.approx(want, abs=1e-3)


def test_clip_shorter_than_slot_uses_whole_clip(editor):
    res = editor.apply_template("whole", "story_vlog", ["silent.mp4"], {})
    for c in res["project"]["clips"]:
        assert (c["start"], c["end"]) == (0.0, 3.0)


def test_texts_skip_empty_and_time_from_end(editor, extra):
    res = editor.apply_template(
        "txt", "product_showcase", ["red.mp4", "blue.mov"],
        {"product": "Mug", "price": "  ", "cta": "Buy now"},
    )
    assert res["skipped_texts"] == ["price"]
    proj = res["project"]
    by_key = {t["key"]: t for t in proj["texts"]}
    assert set(by_key) == {"product", "cta"}
    assert by_key["product"]["start"] == 0.3 and by_key["product"]["fade"] is True
    assert by_key["cta"]["end"] == pytest.approx(proj["output_duration"] - 0.3, abs=1e-3)
    assert by_key["cta"]["end"] - by_key["cta"]["start"] == pytest.approx(2.5, abs=1e-3)
    with pytest.raises(VideoMCPError, match="Unknown text field"):
        editor.apply_template("txt", "product_showcase", ["red.mp4"], {"subtitle": "x"})


def test_result_is_a_normal_editable_project(editor, extra):
    editor.apply_template("edit", "fast_hype", ["red.mp4", "song.mp3"], {"title": "GO"})
    proj = editor.get_project("edit")
    assert proj["music"]["file"].endswith("song.mp3")  # audio-only file in files -> music
    cid = proj["clips"][0]["id"]
    editor.update_clip("edit", cid, speed=2.0)
    editor.set_look("edit", "bw")
    editor.add_text("edit", "extra", 0, 1)
    assert editor.get_project("edit")["look"] == "bw"
    # Re-applying replaces the timeline rather than appending to it.
    editor.apply_template("edit", "clean_cuts", ["red.mp4"], {})
    proj = editor.get_project("edit")
    assert len(proj["clips"]) == 5 and proj["look"] == "none" and proj["texts"] == []
    assert json.loads(editor.ws.project_path("edit").read_text())["template"] == "clean_cuts"


def test_apply_template_errors(editor):
    with pytest.raises(VideoMCPError, match="No template"):
        editor.apply_template("e", "nope", ["landscape.mp4"])
    with pytest.raises(VideoMCPError, match="Invalid template id"):
        editor.apply_template("e", "../../etc/passwd", ["landscape.mp4"])
    with pytest.raises(VideoMCPError, match="at least one video"):
        editor.apply_template("e", "clean_cuts", ["music.m4a"])
    with pytest.raises(VideoMCPError, match="non-empty list"):
        editor.apply_template("e", "clean_cuts", [])
    with pytest.raises(VideoMCPError, match=r"\.\."):
        editor.apply_template("e", "clean_cuts", ["../../x.mp4"])
