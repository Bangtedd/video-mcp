"""Fast tests of pure helpers and validation (still using real ffprobe for media)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from video_mcp.config import Config, is_loopback
from video_mcp.errors import VideoMCPError
from video_mcp.render import atempo_chain, escape_option, wrap_text
from video_mcp.workspace import Workspace


@pytest.mark.parametrize("speed", [0.25, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0])
def test_atempo_chain_stays_in_range(speed):
    chain = atempo_chain(speed)
    product = 1.0
    for f in chain:
        v = float(f.split("=")[1])
        assert 0.5 <= v <= 2.0
        product *= v
    assert product == pytest.approx(speed, rel=1e-5)


def test_wrap_text_keeps_newlines_and_limits_width():
    out = wrap_text("one two three four five six\nseven", 9)
    assert all(len(line) <= 9 for line in out.split("\n"))
    assert out.split("\n")[-1] == "seven"
    assert wrap_text("abcdefghijkl", 5) == "abcde\nfghij\nkl"


def test_escape_option_escapes_both_levels():
    assert escape_option("a:b") == r"a\\:b"
    assert escape_option("x,y") == r"x\,y"
    assert escape_option("it's") == r"it\\\'s"


def test_loopback_detection():
    assert is_loopback("127.0.0.1")
    assert is_loopback("localhost")
    assert is_loopback("::1")
    assert not is_loopback("0.0.0.0")
    assert not is_loopback("192.168.1.5")


# ------------------------------------------------------------ path safety


def test_path_traversal_rejected(workspace, tmp_path):
    ws = Workspace(workspace)
    for bad in ["../secret.mp4", "inbox/../../secret.mp4", "..", "a/../../b"]:
        with pytest.raises(VideoMCPError, match=r"\.\."):
            ws.resolve_file(bad)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    with pytest.raises(VideoMCPError, match="outside the workspace"):
        ws.resolve_file(str(outside))
    with pytest.raises(VideoMCPError, match="outside the workspace"):
        ws.resolve_file("/etc/passwd")


def test_symlink_escape_rejected(workspace, tmp_path):
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"x")
    (workspace / "inbox" / "sneaky.mp4").symlink_to(outside)
    (workspace / "inbox" / "dirlink").symlink_to(tmp_path)
    ws = Workspace(workspace)
    with pytest.raises(VideoMCPError, match="outside the workspace"):
        ws.resolve_file("sneaky.mp4")
    with pytest.raises(VideoMCPError, match="outside the workspace"):
        ws.resolve_file("dirlink/elsewhere.mp4")


def test_symlink_inside_workspace_allowed(workspace):
    (workspace / "inbox" / "alias.mp4").symlink_to(workspace / "inbox" / "landscape.mp4")
    assert Workspace(workspace).resolve_file("alias.mp4").name == "landscape.mp4"


def test_absolute_path_inside_workspace_allowed(workspace):
    p = Workspace(workspace).resolve_file(str(workspace / "inbox" / "landscape.mp4"))
    assert p.name == "landscape.mp4"


def test_tools_reject_traversal(editor):
    editor.create_project("p")
    for call in [
        lambda: editor.probe_media("../../etc/passwd"),
        lambda: editor.get_frame("/etc/passwd", 0),
        lambda: editor.add_clip("p", "../inbox/landscape.mp4"),
        lambda: editor.set_music("p", "/etc/hostname"),
    ]:
        with pytest.raises(VideoMCPError):
            call()


@pytest.mark.parametrize("name", ["../x", "a b", "x/y", "", "a.json", "ünï", "a" * 65])
def test_bad_project_names(editor, name):
    with pytest.raises(VideoMCPError, match="Invalid project name"):
        editor.create_project(name)


# ------------------------------------------------------------ validation


def test_media_listing_and_probe(editor):
    files = {f["file"]: f for f in editor.list_media()["files"]}
    assert files["landscape.mp4"]["width"] == 640 and files["landscape.mp4"]["has_audio"]
    assert files["portrait.mp4"]["height"] == 640 and files["portrait.mp4"]["fps"] == 25
    assert files["silent.mp4"]["has_audio"] is False
    assert files["music.m4a"]["has_video"] is False
    rot = editor.probe_media("rotated.mp4")
    assert abs(rot["rotation"]) == 90
    assert (rot["width"], rot["height"]) == (360, 640)
    assert rot["orientation"] == "portrait"


def test_clip_validation(editor):
    editor.create_project("v")
    with pytest.raises(VideoMCPError, match="greater than start"):
        editor.add_clip("v", "landscape.mp4", 2, 2)
    with pytest.raises(VideoMCPError, match="greater than start"):
        editor.add_clip("v", "landscape.mp4", 3, 1)
    with pytest.raises(VideoMCPError, match="past the end"):
        editor.add_clip("v", "landscape.mp4", 0, 10)
    with pytest.raises(VideoMCPError, match="no video stream"):
        editor.add_clip("v", "music.m4a")
    with pytest.raises(VideoMCPError, match="not found"):
        editor.add_clip("v", "nope.mp4")
    with pytest.raises(VideoMCPError, match="does not exist"):
        editor.add_clip("missing", "landscape.mp4")
    clip = editor.add_clip("v", "landscape.mp4")["clip"]
    assert (clip["start"], clip["end"]) == (0, 4.0)
    for kwargs, msg in [
        ({"speed": 0.1}, "speed"),
        ({"speed": 5}, "speed"),
        ({"volume": -1}, "volume"),
        ({"volume": 2.5}, "volume"),
        ({"end": 4.5}, "past the end"),
        ({"start": 4.0}, "greater than start"),
    ]:
        with pytest.raises(VideoMCPError, match=msg):
            editor.update_clip("v", clip["id"], **kwargs)
    with pytest.raises(VideoMCPError, match="No clip"):
        editor.update_clip("v", "c99", speed=2)
    with pytest.raises(VideoMCPError, match="position"):
        editor.add_clip("v", "landscape.mp4", position=5)


def test_clip_ordering(editor):
    editor.create_project("o")
    a = editor.add_clip("o", "landscape.mp4")["clip"]["id"]
    b = editor.add_clip("o", "portrait.mp4")["clip"]["id"]
    c = editor.add_clip("o", "silent.mp4", position=0)["clip"]["id"]
    assert [x["id"] for x in editor.get_project("o")["clips"]] == [c, a, b]
    assert editor.move_clip("o", c, 2)["order"] == [a, b, c]
    editor.remove_clip("o", a)
    assert [x["id"] for x in editor.get_project("o")["clips"]] == [b, c]


def test_duration_math(editor):
    editor.create_project("d", "square", "pad")
    c = editor.add_clip("d", "landscape.mp4", 1, 3)["clip"]["id"]
    editor.update_clip("d", c, speed=0.5)
    editor.add_clip("d", "silent.mp4")
    p = editor.get_project("d")
    assert p["output_duration"] == pytest.approx(4 + 3)
    assert p["clips"][1]["timeline_start"] == pytest.approx(4)
    assert (p["width"], p["height"]) == (1080, 1080)
    assert editor.list_projects()["projects"][0]["output_duration"] == pytest.approx(7)


def test_text_and_music_validation(editor):
    editor.create_project("t")
    with pytest.raises(VideoMCPError, match="Add clips"):
        editor.add_text("t", "hi", 0, 1)
    editor.add_clip("t", "landscape.mp4")  # 4 s
    with pytest.raises(VideoMCPError, match="past the end of the timeline"):
        editor.add_text("t", "hi", 3, 5)
    with pytest.raises(VideoMCPError, match="greater than start"):
        editor.add_text("t", "hi", 2, 1)
    with pytest.raises(VideoMCPError, match="Unknown color"):
        editor.add_text("t", "hi", 0, 1, color="notacolor:x")
    with pytest.raises(VideoMCPError, match="position"):
        editor.add_text("t", "hi", 0, 1, position="left")
    with pytest.raises(VideoMCPError, match="empty"):
        editor.add_text("t", "  \n ", 0, 1)
    tid = editor.add_text("t", "hi", 0, 4)["text"]["id"]
    with pytest.raises(VideoMCPError, match="past the end of the timeline"):
        editor.update_text("t", tid, end=4.5)

    with pytest.raises(VideoMCPError, match="offset"):
        editor.set_music("t", "music.m4a", offset=25)
    with pytest.raises(VideoMCPError, match="volume"):
        editor.set_music("t", "music.m4a", volume=3)
    with pytest.raises(VideoMCPError, match="no audio"):
        editor.set_music("t", "silent.mp4")
    assert editor.set_music("t", "music.m4a")["music"]["volume"] == 0.3
    assert editor.set_music("t", None)["music"] is None


def test_shrinking_timeline_blocks_render(editor):
    editor.create_project("s")
    c = editor.add_clip("s", "landscape.mp4")["clip"]["id"]
    editor.add_text("s", "late", 3, 4)
    editor.update_clip("s", c, end=2)
    assert editor.get_project("s")["problems"]
    with pytest.raises(VideoMCPError, match="Cannot render"):
        editor.render("s")
    editor.create_project("empty")
    with pytest.raises(VideoMCPError, match="no clips"):
        editor.render("empty")


def test_get_frame_validation(editor):
    with pytest.raises(VideoMCPError, match="past the end"):
        editor.get_frame("landscape.mp4", 4.0)
    with pytest.raises(VideoMCPError, match="no video"):
        editor.get_frame("music.m4a", 1)


# ------------------------------------------------------------ startup safety


def _run_server(env_extra: dict, workspace) -> subprocess.CompletedProcess:
    env = {**os.environ, "WORKSPACE_DIR": str(workspace), **env_extra}
    return subprocess.run(
        [sys.executable, "-m", "video_mcp"], env=env, capture_output=True, text=True, timeout=30
    )


def test_refuses_public_bind_without_token(workspace):
    proc = _run_server({"HOST": "0.0.0.0", "PORT": "0", "AUTH_TOKEN": ""}, workspace)
    assert proc.returncode != 0
    assert "AUTH_TOKEN" in proc.stderr


def test_config_from_env(tmp_path):
    cfg = Config.from_env({"WORKSPACE_DIR": str(tmp_path), "PORT": "9000", "HOST": "0.0.0.0"})
    assert cfg.port == 9000 and cfg.host == "0.0.0.0" and cfg.auth_token is None
    default = Config.from_env({})
    assert default.host == "127.0.0.1" and default.port == 8765
    assert str(default.workspace_dir).endswith("video-mcp/workspace")
