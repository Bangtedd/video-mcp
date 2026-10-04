"""The web app, driven over HTTP as a separate process (like on the Pi)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from conftest import frame_at, probe, region_diff, region_mean, wait_job
from test_server_e2e import free_port
from video_mcp.config import Config
from video_mcp.editor import Editor

PASSWORD = "correct horse battery"


def start_web(workspace: Path, password: str | None = PASSWORD, **env_extra):
    port = free_port()
    env = {**os.environ, "WORKSPACE_DIR": str(workspace), "WEB_HOST": "127.0.0.1",
           "WEB_PORT": str(port), **env_extra}
    env.pop("WEB_PASSWORD", None)
    if password:
        env["WEB_PASSWORD"] = password
    exe = Path(sys.executable).parent / "video-mcp-web"
    cmd = [str(exe)] if exe.exists() else [sys.executable, "-m", "video_mcp.web.app"]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            return proc, None
        try:
            httpx.get(base + "/healthz", timeout=0.5)
            return proc, base
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.kill()
    raise AssertionError("web app did not start")


@pytest.fixture
def web(workspace, extra):
    proc, base = start_web(workspace)
    assert base, proc.stdout.read().decode()
    yield base
    proc.terminate()
    proc.wait(timeout=10)


def login(base: str, password: str = PASSWORD) -> httpx.Client:
    c = httpx.Client(base_url=base, timeout=120, follow_redirects=False)
    page = c.get("/login")
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
    r = c.post("/login", data={"password": password, "csrf": token})
    assert r.status_code == 303, r.text
    c.headers["X-CSRF-Token"] = c.get("/api/state").json()["csrf"]
    return c


def upload(c: httpx.Client, sid: str, path: Path, name: str | None = None) -> httpx.Response:
    return c.post(
        f"/api/sessions/{sid}/uploads",
        content=path.read_bytes(),
        headers={"X-Filename": name or path.name, "Content-Type": "application/octet-stream"},
    )


def poll(c: httpx.Client, job: dict, timeout: float = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = c.get(f"/api/jobs/{job['job_id']}").json()
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(0.2)
    raise AssertionError("render timed out")


# -------------------------------------------------------------------- auth


def test_refuses_to_start_without_password(workspace):
    proc, base = start_web(workspace, password=None)
    assert base is None
    assert proc.returncode != 0
    assert "WEB_PASSWORD" in proc.stdout.read().decode()


def test_pages_require_login(web):
    c = httpx.Client(base_url=web, timeout=30)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    for path in ["/api/state", "/api/videos", "/api/jobs/0123456789ab"]:
        assert c.get(path).status_code == 401, path
    assert c.get("/videos/x.mp4", follow_redirects=False).status_code == 303
    assert c.post("/api/sessions").status_code == 401
    # Login page and static assets are public; everything carries security headers.
    r = c.get("/login")
    assert r.status_code == 200 and 'type="password"' in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert c.get("/static/app.js").status_code == 200


def test_wrong_password_rejected_slowly(web):
    c = httpx.Client(base_url=web, timeout=30)
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/login").text).group(1)
    t0 = time.monotonic()
    r = c.post("/login", data={"password": "wrong", "csrf": token})
    assert time.monotonic() - t0 >= 1.0
    assert r.status_code == 401 and "Wrong password" in r.text
    assert "vmw_session" not in r.cookies
    assert c.get("/api/state").status_code == 401
    # The login form itself is CSRF-protected.
    r = httpx.post(web + "/login", data={"password": PASSWORD, "csrf": "forged"})
    assert r.status_code == 403
    # A tampered session cookie is ignored.
    good = login(web)
    value = good.cookies["vmw_session"]
    forged = httpx.Client(base_url=web, cookies={"vmw_session": value[:-2] + "AA"})
    assert forged.get("/api/state").status_code == 401
    # Cookie flags.
    page = httpx.get(web + "/login")
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
    r = httpx.post(web + "/login", data={"password": PASSWORD, "csrf": token}, cookies=page.cookies)
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=lax" in set_cookie


def test_csrf_enforced(web, extra_media_dir):
    c = login(web)
    token = c.headers.pop("X-CSRF-Token")
    assert c.post("/api/sessions").status_code == 403
    assert c.post("/api/sessions", headers={"X-CSRF-Token": "0" * 32}).status_code == 403
    r = c.post("/api/sessions", headers={"X-CSRF-Token": token})
    assert r.status_code == 201
    sid = r.json()["id"]
    assert upload(c, sid, extra_media_dir / "red.mp4").status_code == 403
    assert c.delete(f"/api/sessions/{sid}").status_code == 403
    assert c.delete("/api/videos/x.mp4").status_code == 403
    assert c.post("/logout").status_code == 403
    assert c.get(f"/api/sessions/{sid}").status_code == 200  # reads need no token


# ------------------------------------------------------------------ uploads


def test_upload_validation(web, workspace, extra_media_dir):
    c = login(web)
    sid = c.post("/api/sessions").json()["id"]
    sdir = workspace / "inbox" / sid

    r = c.post(f"/api/sessions/{sid}/uploads", content=b"not a video at all" * 500,
               headers={"X-Filename": "holiday.mp4"})
    assert r.status_code == 400 and "not a playable" in r.json()["error"]
    r = c.post(f"/api/sessions/{sid}/uploads", content=b"MZ\x90\x00" * 100,
               headers={"X-Filename": "tool.exe"})
    assert r.status_code == 415
    r = c.post(f"/api/sessions/{sid}/uploads", content=b"", headers={"X-Filename": "empty.mov"})
    assert r.status_code == 400
    assert sorted(p.name for p in sdir.iterdir()) == [".session.json", ".thumbs"]

    # Generated names on disk, never the client's (even a hostile one).
    r = upload(c, sid, extra_media_dir / "red.mp4", name="../../../etc/evil name.mp4")
    assert r.status_code == 201, r.text
    clip = r.json()["clips"][0]
    assert clip["name"] == "evil name.mp4" and clip["kind"] == "clip"
    on_disk = [p.name for p in sdir.iterdir() if not p.name.startswith(".")]
    assert len(on_disk) == 1 and re.fullmatch(r"[0-9a-f]{16}\.mp4", on_disk[0])
    assert not (workspace.parent / "etc").exists()

    thumb = c.get(clip["thumb"])
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"

    # Music: one file, replaced by the next upload.
    r = upload(c, sid, extra_media_dir / "song.mp3")
    assert r.json()["music"]["kind"] == "music"
    first_music = r.json()["music"]["id"]
    r = upload(c, sid, workspace / "inbox" / "music.m4a")
    assert r.json()["music"]["id"] != first_music
    assert len([p for p in sdir.iterdir() if p.suffix in (".mp3", ".m4a")]) == 1

    # Reorder and remove.
    b = upload(c, sid, extra_media_dir / "blue.mov").json()["clips"][1]["id"]
    order = [b, clip["id"]]
    assert [x["id"] for x in c.post(f"/api/sessions/{sid}/order", json={"order": order}).json()["clips"]] == order
    assert c.post(f"/api/sessions/{sid}/order", json={"order": [b]}).status_code == 400
    r = c.delete(f"/api/sessions/{sid}/uploads/{clip['id']}")
    assert [x["id"] for x in r.json()["clips"]] == [b]
    assert len([p for p in sdir.iterdir() if p.suffix == ".mp4"]) == 0


def test_upload_size_limit(workspace, extra):
    proc, base = start_web(workspace, WEB_MAX_UPLOAD_MB="1")
    try:
        c = login(base)
        sid = c.post("/api/sessions").json()["id"]
        big = b"\0" * (2 * 1024 * 1024)
        r = c.post(f"/api/sessions/{sid}/uploads", content=big, headers={"X-Filename": "big.mp4"})
        assert r.status_code == 413
        # Streamed without a Content-Length: cut off while streaming to disk.
        r = c.post(f"/api/sessions/{sid}/uploads", content=iter([big[:600000]] * 4),
                   headers={"X-Filename": "big.mp4"})
        assert r.status_code == 413
        files = [p.name for p in (workspace / "inbox" / sid).iterdir()]
        assert sorted(files) == [".session.json", ".thumbs"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_path_traversal_in_ids_rejected(web, workspace):
    c = login(web)
    sid = c.post("/api/sessions").json()["id"]
    (workspace / "projects" / "secret.json").write_text("{}")
    (workspace / "renders" / "keep.mp4").write_bytes(b"x")
    bad = [
        ("GET", "/api/sessions/..%2F..%2Fprojects"),
        ("GET", "/api/sessions/../projects"),
        ("GET", f"/api/sessions/{sid}/uploads/..%2F.session.json/thumb"),
        ("GET", f"/api/sessions/{sid}/uploads/%2e%2e/thumb"),
        ("GET", f"/api/sessions/{sid}/templates/..%2F..%2Fpyproject/thumb"),
        ("GET", f"/api/sessions/{sid}/templates/../../etc/passwd/thumb"),
        ("GET", "/api/jobs/..%2F..%2Fx"),
        ("GET", "/videos/..%2Fprojects%2Fsecret.json"),
        ("GET", "/videos/%2e%2e%2f.web_secret"),
        ("GET", "/videos/....mp4"),
        ("GET", "/videos/.render.lock"),
        ("DELETE", "/api/videos/..%2Fprojects%2Fsecret.json"),
        ("DELETE", "/api/videos/..%2Frenders%2Fkeep.mp4"),
        ("DELETE", f"/api/sessions/{'0' * 31}g"),
        ("DELETE", "/api/sessions/..%2Fprojects"),
        ("DELETE", f"/api/sessions/{sid}/uploads/..%2F..%2Fprojects%2Fsecret.json"),
        ("POST", f"/api/sessions/{'a' * 32}/make"),
    ]
    for method, path in bad:
        r = c.request(method, path, json={"template": "clean_cuts"} if method == "POST" else None)
        assert r.status_code in (400, 404, 405), (method, path, r.status_code)
    r = c.post(f"/api/sessions/{sid}/make", json={"template": "../../etc/passwd"})
    assert r.status_code in (400, 404)
    assert (workspace / "projects" / "secret.json").exists()
    assert (workspace / "renders" / "keep.mp4").exists()
    assert (workspace / ".web_secret").exists()


def test_stale_sessions_removed_on_startup(workspace):
    old, fresh = "a" * 32, "b" * 32
    for sid, age in ((old, 8 * 86400), (fresh, 3 * 86400)):
        d = workspace / "inbox" / sid
        d.mkdir(parents=True)
        (d / "0123456789abcdef.mp4").write_bytes(b"x")
        (d / ".session.json").write_text(json.dumps(
            {"id": sid, "last_activity": time.time() - age, "clips": [], "music": None}))
        (workspace / "projects").mkdir(exist_ok=True)
        (workspace / "projects" / f"web-{sid}.json").write_text("{}")
    (workspace / "renders").mkdir(exist_ok=True)
    (workspace / "renders" / f"web-{old}-final-x.mp4").write_bytes(b"x")
    proc, base = start_web(workspace)
    try:
        assert base
        assert not (workspace / "inbox" / old).exists()
        assert not (workspace / "projects" / f"web-{old}.json").exists()
        assert (workspace / "inbox" / fresh).exists()
        assert (workspace / "renders" / f"web-{old}-final-x.mp4").exists()  # renders are kept
        assert (workspace / "inbox" / "landscape.mp4").exists()            # other inbox files untouched
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# --------------------------------------------------------------- end to end


def test_upload_template_render_download(web, workspace, extra_media_dir, tmp_path, job_manager):
    """Done means #2: three mixed clips and a music file, warm_promo with two
    text fields, final render downloaded and checked frame by frame."""
    c = login(web)
    state = c.get("/api/state").json()
    assert {t["id"] for t in state["templates"]} >= {"warm_promo"}
    sid = c.post("/api/sessions").json()["id"]
    for name in ["red.mp4", "blue.mov", "green.mp4", "song.mp3"]:
        r = upload(c, sid, extra_media_dir / name)
        assert r.status_code == 201, r.text
    sess = r.json()
    assert [x["name"] for x in sess["clips"]] == ["red.mp4", "blue.mov", "green.mp4"]
    assert [x["width"] > x["height"] for x in sess["clips"]] == [True, False, False]
    assert sess["clips"][2]["has_audio"] is False
    assert sess["music"]["name"] == "song.mp3"

    # Template cards show the first clip with each template's look applied.
    warm = c.get(f"/api/sessions/{sid}/templates/warm_promo/thumb")
    bw = c.get(f"/api/sessions/{sid}/templates/bw_mood/thumb")
    assert warm.status_code == bw.status_code == 200
    assert warm.content != bw.content

    texts = {"title": "FRESH BREAD", "cta": "Visit us today"}
    r = c.post(f"/api/sessions/{sid}/make", json={"template": "warm_promo", "texts": texts})
    assert r.status_code == 202, r.text
    made = r.json()
    preview = poll(c, made["job"])
    assert preview["status"] == "done", preview
    assert preview["quality"] == "preview"
    total = made["duration"]
    # 4 slots (3, 2.5, 2.5, 3 s) over 3 clips, minus three 0.5 s fades.
    assert total == pytest.approx(11 - 1.5, abs=0.04)

    r = c.post(f"/api/sessions/{sid}/final")
    assert r.status_code == 202
    final = poll(c, r.json()["job"])
    assert final["status"] == "done", final

    # Download (attachment) and range requests.
    url = final["video"]["download_url"]
    r = c.get(url)
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers.get("accept-ranges") == "bytes"
    out = tmp_path / "download.mp4"
    out.write_bytes(r.content)
    size = len(r.content)
    part = c.get(final["video"]["url"], headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert part.headers["content-range"] == f"bytes 0-99/{size}"
    assert part.content == r.content[:100]
    tail = c.get(final["video"]["url"], headers={"Range": "bytes=-64"})
    assert tail.status_code == 206 and tail.content == r.content[-64:]
    mid = c.get(final["video"]["url"], headers={"Range": f"bytes={size // 2}-{size // 2 + 9}"})
    assert mid.status_code == 206 and mid.content == r.content[size // 2:size // 2 + 10]
    assert c.get(final["video"]["url"], headers={"Range": f"bytes={size + 10}-"}).status_code == 416

    info = probe(out)
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["video"]["codec_name"] == "h264" and info["audio"]["codec_name"] == "aac"
    assert info["duration"] == pytest.approx(total, abs=0.2)
    assert out.read_bytes().find(b"moov") < out.read_bytes().find(b"mdat")

    # Reference renders of the same project, made in this (separate) process:
    # ref_plain = no texts and no gradient; ref_nolook = that plus look none.
    ed = Editor(Config(workspace_dir=workspace), jobs=job_manager)
    project = json.loads((workspace / "projects" / f"web-{sid}.json").read_text())
    assert len(project["clips"]) == 4
    assert project["look"] == "warm" and project["gradient"] == "bottom"
    assert project["transition"] == {"type": "fade", "duration": 0.5}
    assert {t["key"] for t in project["texts"]} == {"title", "cta"}

    def reference(name, **changes):
        p = dict(project, name=name, **changes)
        (workspace / "projects" / f"{name}.json").write_text(json.dumps(p))
        res = wait_job(ed, ed.render(name, "final")["job_id"])
        assert res["status"] == "done", res
        return workspace / res["output"]

    ref_plain = reference("ref_plain", texts=[], gradient="none")
    ref_nolook = reference("ref_nolook", texts=[], gradient="none", look="none")

    W, H = 216, 384
    f = lambda path, t: frame_at(path, t, W, H)  # noqa: E731
    rows = lambda a, b: (int(H * a), int(H * b))  # noqa: E731
    middle = rows(0.40, 0.60)

    # Clip order: red (0-3 s), fade into blue (2.5-3.0), blue, fade into green, ...
    red, blend, blue = f(out, 1.5), f(out, 2.75), f(out, 4.0)
    assert region_mean(red, W, *middle, 0) > 150 and region_mean(red, W, *middle, 2) < 90
    assert region_mean(blue, W, *middle, 2) > 150 and region_mean(blue, W, *middle, 0) < 100
    # Transition: mid-dissolve the picture is a mix of both clips.
    assert 50 < region_mean(blend, W, *middle, 0) < 200
    assert 50 < region_mean(blend, W, *middle, 2) < 200

    # Look: warm grading changes the picture against the ungraded reference.
    t_look = 4.0
    graded, ungraded = f(ref_plain, t_look), f(ref_nolook, t_look)
    assert region_diff(graded, ungraded, W, *middle) > 5
    assert region_mean(graded, W, *middle, 0) > region_mean(ungraded, W, *middle, 0) + 5  # warmer

    # Gradient: the bottom 35% is darker, the middle is untouched (no text at 4 s).
    shaded = f(out, t_look)
    assert region_mean(graded, W, *rows(0.75, 0.97)) - region_mean(shaded, W, *rows(0.75, 0.97)) > 15
    assert region_diff(shaded, graded, W, *middle) < 4

    # Text: the title at the top early on, the call to action at the bottom at the end.
    title, title_ref = f(out, 1.5), f(ref_plain, 1.5)
    assert region_diff(title, title_ref, W, *rows(0.08, 0.16)) > 15
    t_cta = total - 1.0
    cta, cta_ref = f(out, t_cta), f(ref_plain, t_cta)
    assert region_diff(cta, cta_ref, W, *rows(0.80, 0.92)) > 15

    poster = c.get(final["video"]["poster"])
    assert poster.status_code == 200 and poster.content[:2] == b"\xff\xd8"

    # My videos: both renders listed; delete one.
    vids = c.get("/api/videos").json()["videos"]
    mine = [v for v in vids if v["name"].startswith(f"web-{sid}")]
    assert {v["quality"] for v in mine} == {"preview", "final"}
    assert all(v["template"] == "Warm promo" for v in mine)
    prev = next(v for v in mine if v["quality"] == "preview")
    assert c.delete(f"/api/videos/{prev['name']}").status_code == 200
    assert c.get(prev["url"]).status_code == 404
    assert prev["name"] not in {v["name"] for v in c.get("/api/videos").json()["videos"]}

    # "Try another template" keeps the uploads.
    r = c.post(f"/api/sessions/{sid}/make", json={"template": "bw_mood", "texts": {"title": "Again"}})
    assert r.status_code == 202
    assert poll(c, r.json()["job"])["status"] == "done"
    assert len(c.get(f"/api/sessions/{sid}").json()["clips"]) == 3


def test_make_needs_clips_and_known_template(web):
    c = login(web)
    sid = c.post("/api/sessions").json()["id"]
    r = c.post(f"/api/sessions/{sid}/make", json={"template": "warm_promo"})
    assert r.status_code == 400 and "clip" in r.json()["error"]
    assert c.post(f"/api/sessions/{sid}/final").status_code == 400
    assert c.post(f"/api/sessions/{sid}/make", content=b"not json").status_code == 400
    assert c.get("/api/jobs/0123456789ab").status_code == 404
