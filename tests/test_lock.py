"""Renders are serialised across processes with a lock file in the workspace."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time

import pytest

from conftest import wait_job

HOLD_LOCK = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    from video_mcp.jobs import render_lock
    with render_lock(Path(sys.argv[1])):
        print("locked", flush=True)
        time.sleep(float(sys.argv[2]))
        print(time.time(), flush=True)
""")

RENDER_ELSEWHERE = textwrap.dedent("""
    import json, sys, time
    from video_mcp.config import Config
    from video_mcp.editor import Editor
    ed = Editor(Config(workspace_dir=sys.argv[1]))
    job = ed.render(sys.argv[2], "preview")
    while ed.get_job(job["job_id"])["status"] not in ("done", "failed"):
        time.sleep(0.05)
    print(json.dumps(ed.get_job(job["job_id"])), flush=True)
""")


def test_render_waits_for_lock_held_by_another_process(editor):
    editor.create_project("locked", "square")
    editor.add_clip("locked", "landscape.mp4", 0, 1)
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD_LOCK, str(editor.ws.render_lock), "2.0"],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        job = editor.render("locked", "preview")
        time.sleep(1.0)
        assert editor.get_job(job["job_id"])["status"] == "queued"
        released = float(holder.stdout.readline())
        result = wait_job(editor, job["job_id"])
    finally:
        holder.wait(timeout=30)
    assert result["status"] == "done", result
    assert result["started_at"] >= released


def test_two_processes_never_render_at_once(editor):
    editor.create_project("p1", "vertical")
    editor.add_clip("p1", "landscape.mp4")
    editor.add_clip("p1", "portrait.mp4")
    editor.create_project("p2", "vertical")
    editor.add_clip("p2", "portrait.mp4")
    editor.add_clip("p2", "landscape.mp4")
    other = subprocess.Popen(
        [sys.executable, "-c", RENDER_ELSEWHERE, str(editor.ws.root), "p2"],
        stdout=subprocess.PIPE, text=True,
    )
    time.sleep(0.3)
    mine = editor.get_job(editor.render("p1", "final")["job_id"])
    mine = wait_job(editor, mine["job_id"])
    theirs = json.loads(other.communicate(timeout=300)[0].strip().splitlines()[-1])
    assert mine["status"] == theirs["status"] == "done"
    spans = sorted([(mine["started_at"], mine["finished_at"]), (theirs["started_at"], theirs["finished_at"])])
    assert spans[0][1] <= spans[1][0] + 0.05, f"renders overlapped: {spans}"


def test_lock_file_lives_in_workspace(editor):
    assert editor.ws.render_lock.parent == editor.ws.root
    editor.create_project("lk", "square")
    editor.add_clip("lk", "landscape.mp4", 0, 0.5)
    assert wait_job(editor, editor.render("lk")["job_id"])["status"] == "done"
    assert editor.ws.render_lock.exists()
    with pytest.raises(Exception):
        editor.ws.resolve_file("/etc/passwd")
