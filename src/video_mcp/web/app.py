"""video-mcp-web: phone-friendly web app. Upload clips, pick a template, type a
caption, download the video. Shares the workspace with the MCP server."""

from __future__ import annotations

import asyncio
import functools
import hmac
import html
import json
import logging
import re
import secrets
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import ffmpeg, segments
from ..config import Config
from ..editor import Editor
from ..errors import VideoMCPError
from ..templates import check_template_id
from . import auth
from .store import (
    ALLOWED_EXTS,
    AUDIO_EXTS,
    MAX_CLIPS,
    NotFound,
    Store,
    check_file_id,
    check_session_id,
    project_name,
)

log = logging.getLogger("video_mcp.web")

STATIC = Path(__file__).parent / "static"
MAX_JSON = 64 * 1024
CHUNK_LOG = 8 * 1024 * 1024
JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
VIDEO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,200}(\.[A-Za-z0-9_-]+)*\.mp4$")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

SECURITY_HEADERS = [
    (b"content-security-policy",
     b"default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
     b"style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; "
     b"frame-ancestors 'none'; form-action 'self'"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
]


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _cookies(scope) -> dict[str, str]:
    raw = b"; ".join(v for k, v in scope.get("headers") or [] if k == b"cookie").decode("latin-1")
    out = {}
    for part in raw.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep:
            out[name] = value
    return out


class Guard:
    """ASGI middleware: login required everywhere except the login page and
    static assets; CSRF token header required on every state-changing request;
    security headers on every response."""

    def __init__(self, app, signer: auth.Signer):
        self.app = app
        self.signer = signer

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": list(message.get("headers") or []) + SECURITY_HEADERS}
            await send(message)

        path = scope["path"]
        if path in ("/login", "/healthz", "/favicon.ico") or path.startswith("/static/"):
            return await self.app(scope, receive, send_with_headers)

        session = self.signer.read_session(_cookies(scope).get(auth.COOKIE))
        if session is None:
            if path.startswith("/api/") or scope["method"] not in SAFE_METHODS:
                resp = JSONResponse({"error": "Log in first."}, status_code=401)
            else:
                resp = RedirectResponse("/login", status_code=303)
            return await resp(scope, receive, send_with_headers)

        if scope["method"] not in SAFE_METHODS:
            supplied = dict(scope.get("headers") or []).get(b"x-csrf-token", b"").decode("latin-1")
            if not supplied or not hmac.compare_digest(supplied, session["csrf"]):
                resp = JSONResponse({"error": "Missing or invalid CSRF token."}, status_code=403)
                return await resp(scope, receive, send_with_headers)

        scope.setdefault("state", {})["session"] = session
        return await self.app(scope, receive, send_with_headers)


def api(fn):
    """Turn VideoMCPError / HTTPError into JSON errors."""

    @functools.wraps(fn)
    async def wrapper(self, request: Request):
        try:
            return await fn(self, request)
        except NotFound as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except HTTPError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)
        except VideoMCPError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    return wrapper


async def run(fn, *args, **kwargs):
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


async def read_json(request: Request) -> dict:
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_JSON:
        raise HTTPError(413, "Request too large.")
    body = await request.body()
    if len(body) > MAX_JSON:
        raise HTTPError(413, "Request too large.")
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise HTTPError(400, "Body must be JSON.")
    if not isinstance(data, dict):
        raise HTTPError(400, "Body must be a JSON object.")
    return data


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Video maker - log in</title>
<link rel="icon" href="/static/icon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/static/app.css"></head>
<body class="login"><main class="card login-card">
<h1>Video maker</h1>
<form method="post" action="/login">
<input type="hidden" name="csrf" value="{csrf}">
<label for="pw">Password</label>
<input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>
{error}
<button class="primary" type="submit">Log in</button>
</form></main></body></html>
"""


class WebApp:
    def __init__(self, cfg: Config, editor: Editor | None = None):
        if not cfg.web_password:
            raise SystemExit(
                "WEB_PASSWORD is not set. Set it (e.g. in ~/video-mcp/.env) before starting "
                "video-mcp-web; the web app always requires a login."
            )
        self.cfg = cfg
        self.editor = editor or Editor(cfg)
        self.ws = self.editor.ws
        self.store = Store(self.ws, cfg.ffmpeg)
        self.signer = auth.Signer(auth.load_secret(self.ws.root), cfg.web_password)
        self.max_upload = cfg.web_max_upload_mb * 1024 * 1024
        self.job_meta: dict[str, dict] = {}
        self._meta_lock = threading.Lock()

    # --------------------------------------------------------------- pages

    def login_page(self, request: Request, error: str = "", status: int = 200) -> Response:
        # Reuse the browser's existing login token so a second tab (or a stray
        # request redirected here) doesn't invalidate a form already on screen.
        token = request.cookies.get(auth.LOGIN_COOKIE, "")
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            token = secrets.token_hex(16)
        body = LOGIN_PAGE.format(
            csrf=token,
            error=f'<p class="error" role="alert">{html.escape(error)}</p>' if error else "",
        )
        resp = HTMLResponse(body, status_code=status)
        resp.set_cookie(auth.LOGIN_COOKIE, token, httponly=True, samesite="strict", path="/login")
        return resp

    async def login(self, request: Request) -> Response:
        if request.method == "GET":
            if self.signer.read_session(request.cookies.get(auth.COOKIE)):
                return RedirectResponse("/", status_code=303)
            return self.login_page(request)
        body = await request.body()
        if len(body) > MAX_JSON:
            return self.login_page(request, "Request too large.", 413)
        form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
        token = (form.get("csrf") or [""])[0]
        expected = request.cookies.get(auth.LOGIN_COOKIE, "")
        if not token or not expected or not hmac.compare_digest(token, expected):
            return self.login_page(request, "Your login form expired. Try again.", 403)
        password = (form.get("password") or [""])[0]
        if not self.signer.check_password(password):
            await asyncio.sleep(1)
            return self.login_page(request, "Wrong password.", 401)
        value, _ = self.signer.make_session()
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(auth.COOKIE, value, max_age=auth.MAX_AGE, httponly=True,
                        samesite="lax", path="/")
        resp.delete_cookie(auth.LOGIN_COOKIE, path="/login")
        return resp

    async def logout(self, request: Request) -> Response:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(auth.COOKIE, path="/")
        return resp

    async def index(self, request: Request) -> Response:
        return FileResponse(STATIC / "index.html", headers={"cache-control": "no-store"})

    async def healthz(self, request: Request) -> Response:
        return JSONResponse({"ok": True})

    async def favicon(self, request: Request) -> Response:
        return FileResponse(STATIC / "icon.svg", media_type="image/svg+xml",
                            headers={"cache-control": "public, max-age=86400"})

    # ------------------------------------------------------------ sessions

    def session_view(self, meta: dict) -> dict:
        sid = meta["id"]

        def item(x):
            return {k: x.get(k) for k in ("id", "name", "kind", "duration", "width", "height",
                                          "has_audio", "size")} | (
                {"thumb": f"/api/sessions/{sid}/uploads/{x['id']}/thumb"}
                if x["kind"] == "clip" else {})

        return {
            "id": sid,
            "clips": [item(c) for c in meta["clips"]],
            "music": item(meta["music"]) if meta["music"] else None,
            "template": meta.get("template"),
            "texts": meta.get("texts") or {},
            "has_project": self.ws.project_path(project_name(sid)).exists(),
        }

    @api
    async def state(self, request: Request) -> Response:
        templates = await run(self.editor.list_templates)
        return JSONResponse({
            "csrf": request.scope["state"]["session"]["csrf"],
            "templates": templates["templates"],
            "max_upload_bytes": self.max_upload,
            "max_clips": MAX_CLIPS,
            "accept": sorted(ALLOWED_EXTS),
        })

    @api
    async def create_session(self, request: Request) -> Response:
        meta = await run(self.store.create)
        return JSONResponse(self.session_view(meta), status_code=201)

    @api
    async def get_session(self, request: Request) -> Response:
        sid = check_session_id(request.path_params["sid"])
        meta = await run(self.store.load, sid)
        return JSONResponse(self.session_view(meta))

    @api
    async def delete_session(self, request: Request) -> Response:
        sid = check_session_id(request.path_params["sid"])
        await run(self.store.load, sid, False)
        await run(self.store.delete, sid)
        return JSONResponse({"deleted": sid})

    # -------------------------------------------------------------- uploads

    @api
    async def upload(self, request: Request) -> Response:
        sid = check_session_id(request.path_params["sid"])
        meta = await run(self.store.load, sid)
        name = urllib.parse.unquote(request.headers.get("x-filename", ""))[:200]
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_EXTS:
            raise HTTPError(
                415, f"{name or 'That file'}: unsupported type. Use "
                + ", ".join(sorted(ALLOWED_EXTS)) + "."
            )
        length = request.headers.get("content-length")
        too_big = f"{name or 'That file'}: larger than {self.cfg.web_max_upload_mb} MB."
        if length and length.isdigit() and int(length) > self.max_upload:
            raise HTTPError(413, too_big)
        is_audio_ext = ext in AUDIO_EXTS
        if not is_audio_ext and len(meta["clips"]) >= MAX_CLIPS:
            raise HTTPError(400, f"At most {MAX_CLIPS} clips per video.")

        fid, dest = self.store.new_upload_path(sid, ext)
        part = dest.with_name(dest.name + ".part")
        received = 0
        try:
            with open(part, "wb") as f:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > self.max_upload:
                        raise HTTPError(413, too_big)
                    f.write(chunk)
            if received == 0:
                raise HTTPError(400, f"{name}: the file is empty.")
            try:
                info = await run(ffmpeg.probe, self.cfg.ffprobe, part)
            except VideoMCPError:
                info = None
            ok = info is not None and (
                info["has_video"] or info["has_audio"]
            ) and info["duration"] > 0
            if not ok:
                raise HTTPError(400, f"{name}: not a playable video or audio file.")
            part.rename(dest)
        except BaseException:
            part.unlink(missing_ok=True)
            raise

        kind = "clip" if info["has_video"] else "music"
        entry = {
            "id": fid,
            "file": f"{sid}/{dest.name}",
            "name": Path(name).name[:120],
            "kind": kind,
            "duration": info["duration"],
            "width": info["width"],
            "height": info["height"],
            "has_audio": info["has_audio"],
            "size": received,
        }
        old_music: list[dict] = []

        def add(m):
            if kind == "clip":
                if len(m["clips"]) >= MAX_CLIPS:
                    raise HTTPError(400, f"At most {MAX_CLIPS} clips per video.")
                m["clips"].append(entry)
            else:
                if m["music"]:
                    old_music.append(m["music"])
                m["music"] = entry

        try:
            await run(self.store.update, sid, add)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        for old in old_music:
            (self.ws.inbox / old["file"]).unlink(missing_ok=True)
        if kind == "clip":
            try:
                await run(self.store.clip_thumb, sid, entry)
            except VideoMCPError:
                pass
            # Warm the motion-analysis cache so "Make video" is quick later.
            threading.Thread(
                target=self._analyze, args=(dest,), name="analyze", daemon=True
            ).start()
        log.info("upload %s/%s: %s, %d bytes", sid[:8], fid, kind, received)
        return JSONResponse(self.session_view(await run(self.store.load, sid)), status_code=201)

    def _analyze(self, path: Path) -> None:
        try:
            segments.cached_analysis(self.cfg.ffmpeg, self.ws.cache, path)
        except (RuntimeError, OSError):
            pass

    @api
    async def delete_upload(self, request: Request) -> Response:
        sid = check_session_id(request.path_params["sid"])
        fid = check_file_id(request.path_params["fid"])
        meta = await run(self.store.remove_file, sid, fid)
        return JSONResponse(self.session_view(meta))

    @api
    async def reorder(self, request: Request) -> Response:
        sid = check_session_id(request.path_params["sid"])
        data = await read_json(request)
        meta = await run(self.store.reorder, sid, data.get("order"))
        return JSONResponse(self.session_view(meta))

    @api
    async def clip_thumb(self, request: Request) -> Response:
        sid = check_session_id(request.path_params["sid"])
        fid = check_file_id(request.path_params["fid"])
        meta = await run(self.store.load, sid, False)
        item = self.store.find(meta, fid)
        if item["kind"] != "clip":
            raise NotFound("No thumbnail for music.")
        path = await run(self.store.clip_thumb, sid, item)
        return FileResponse(path, media_type="image/jpeg",
                            headers={"cache-control": "private, max-age=86400"})

    @api
    async def template_thumb(self, request: Request) -> Response:
        sid = check_session_id(request.path_params["sid"])
        tid = check_template_id(request.path_params["tid"])
        meta = await run(self.store.load, sid, False)
        if not meta["clips"]:
            raise NotFound("Upload a clip first.")
        template = await run(self.editor.get_template, tid)
        path = await run(self.store.template_thumb, sid, meta["clips"][0], template)
        return FileResponse(path, media_type="image/jpeg",
                            headers={"cache-control": "private, max-age=86400"})

    # --------------------------------------------------------------- render

    def _submit(self, sid: str, quality: str) -> dict:
        project = project_name(sid)
        job = self.editor.render(project, quality)
        tpl = self.editor.get_project(project).get("template")
        name = Path(job["output"]).name
        meta = {"template": tpl, "quality": quality, "created": time.time()}
        try:
            meta["template_name"] = self.editor.get_template(tpl)["name"] if tpl else None
        except VideoMCPError:
            meta["template_name"] = tpl
        sidecar = self.ws.renders / (Path(name).stem + ".json")
        sidecar.write_text(json.dumps(meta), encoding="utf-8")
        with self._meta_lock:
            self.job_meta[job["job_id"]] = {"session": sid}
        return self.job_view(self.editor.get_job(job["job_id"]))

    @staticmethod
    def job_view(job: dict) -> dict:
        out = {k: job.get(k) for k in ("job_id", "status", "progress", "error",
                                       "expected_duration", "quality")}
        if job.get("output"):
            name = Path(job["output"]).name
            out["video"] = {"name": name, "url": f"/videos/{name}",
                            "download_url": f"/videos/{name}?download=1",
                            "poster": f"/api/videos/{name}/poster"}
        return out

    @api
    async def make(self, request: Request) -> Response:
        sid = check_session_id(request.path_params["sid"])
        data = await read_json(request)
        tid = check_template_id(data.get("template"))
        texts = data.get("texts") or {}
        if not isinstance(texts, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in texts.items()
        ):
            raise HTTPError(400, "texts must map field keys to strings.")
        meta = await run(self.store.load, sid)
        if not meta["clips"]:
            raise HTTPError(400, "Upload at least one video clip first.")
        files = [c["file"] for c in meta["clips"]]
        music = meta["music"]["file"] if meta["music"] else None
        result = await run(
            self.editor.apply_template, project_name(sid), tid, files, texts, music
        )

        def remember(m):
            m["template"] = tid
            m["texts"] = texts

        await run(self.store.update, sid, remember)
        job = await run(self._submit, sid, "preview")
        return JSONResponse({
            "job": job,
            "duration": result["project"]["output_duration"],
            "skipped_texts": result["skipped_texts"],
        }, status_code=202)

    @api
    async def final(self, request: Request) -> Response:
        sid = check_session_id(request.path_params["sid"])
        await run(self.store.load, sid)
        if not self.ws.project_path(project_name(sid)).exists():
            raise HTTPError(400, "Make a preview first.")
        job = await run(self._submit, sid, "final")
        return JSONResponse({"job": job}, status_code=202)

    @api
    async def job(self, request: Request) -> Response:
        job_id = request.path_params["job_id"]
        if not JOB_ID_RE.match(job_id):
            raise NotFound("Unknown job.")
        try:
            job = self.editor.get_job(job_id)
        except VideoMCPError:
            raise NotFound("Unknown job. Jobs are forgotten when the app restarts.")
        return JSONResponse(self.job_view(job))

    # --------------------------------------------------------------- videos

    def video_path(self, name: str) -> Path:
        if not isinstance(name, str) or not VIDEO_NAME_RE.match(name) or ".." in name:
            raise NotFound("Unknown video.")
        path = self.ws._contained(self.ws.renders / name, name)
        if path.parent != self.ws.renders or not path.is_file():
            raise NotFound("Unknown video.")
        return path

    def _list_videos(self) -> list[dict]:
        out = []
        for path in self.ws.renders.glob("*.mp4"):
            if path.is_symlink() or not VIDEO_NAME_RE.match(path.name):
                continue
            st = path.stat()
            meta = {}
            sidecar = path.with_suffix(".json")
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            quality = meta.get("quality") or (
                "final" if "-final-" in path.name else "preview" if "-preview-" in path.name else ""
            )
            try:
                duration = self.editor._probe(path)["duration"]
            except VideoMCPError:
                duration = None
            out.append({
                "name": path.name,
                "url": f"/videos/{path.name}",
                "download_url": f"/videos/{path.name}?download=1",
                "poster": f"/api/videos/{path.name}/poster",
                "size": st.st_size,
                "created": meta.get("created") or st.st_mtime,
                "quality": quality,
                "template": meta.get("template_name") or meta.get("template"),
                "duration": duration,
            })
        out.sort(key=lambda v: v["created"], reverse=True)
        return out

    @api
    async def videos(self, request: Request) -> Response:
        return JSONResponse({"videos": await run(self._list_videos)})

    def _poster_path(self, path: Path) -> Path:
        return self.ws.cache / "posters" / (path.stem + ".jpg")

    def _make_poster(self, path: Path) -> Path:
        out = self._poster_path(path)
        if out.is_file() and out.stat().st_mtime >= path.stat().st_mtime:
            return out
        out.parent.mkdir(parents=True, exist_ok=True)
        info = self.editor._probe(path)
        tmp = out.with_name(out.stem + ".tmp.jpg")
        ffmpeg.extract_frame(self.cfg.ffmpeg, path, min(1.5, info["duration"] / 2), tmp)
        tmp.replace(out)
        return out

    @api
    async def poster(self, request: Request) -> Response:
        path = self.video_path(request.path_params["name"])
        out = await run(self._make_poster, path)
        return FileResponse(out, media_type="image/jpeg",
                            headers={"cache-control": "private, max-age=86400"})

    @api
    async def delete_video(self, request: Request) -> Response:
        path = self.video_path(request.path_params["name"])
        path.unlink(missing_ok=True)
        path.with_suffix(".json").unlink(missing_ok=True)
        self._poster_path(path).unlink(missing_ok=True)
        return JSONResponse({"deleted": path.name})

    @api
    async def video_file(self, request: Request) -> Response:
        path = self.video_path(request.path_params["name"])
        headers = {"cache-control": "private, max-age=3600"}
        if request.query_params.get("download"):
            return FileResponse(path, media_type="video/mp4", filename=path.name, headers=headers)
        return FileResponse(path, media_type="video/mp4", headers=headers)

    # ------------------------------------------------------------------ app

    def build(self) -> Guard:
        routes = [
            Route("/", self.index),
            Route("/healthz", self.healthz),
            Route("/favicon.ico", self.favicon),
            Route("/login", self.login, methods=["GET", "POST"]),
            Route("/logout", self.logout, methods=["POST"]),
            Route("/api/state", self.state),
            Route("/api/sessions", self.create_session, methods=["POST"]),
            Route("/api/sessions/{sid}", self.get_session),
            Route("/api/sessions/{sid}", self.delete_session, methods=["DELETE"]),
            Route("/api/sessions/{sid}/uploads", self.upload, methods=["POST"]),
            Route("/api/sessions/{sid}/uploads/{fid}", self.delete_upload, methods=["DELETE"]),
            Route("/api/sessions/{sid}/uploads/{fid}/thumb", self.clip_thumb),
            Route("/api/sessions/{sid}/order", self.reorder, methods=["POST"]),
            Route("/api/sessions/{sid}/templates/{tid}/thumb", self.template_thumb),
            Route("/api/sessions/{sid}/make", self.make, methods=["POST"]),
            Route("/api/sessions/{sid}/final", self.final, methods=["POST"]),
            Route("/api/jobs/{job_id}", self.job),
            Route("/api/videos", self.videos),
            Route("/api/videos/{name}", self.delete_video, methods=["DELETE"]),
            Route("/api/videos/{name}/poster", self.poster),
            Route("/videos/{name}", self.video_file),
            Mount("/static", StaticFiles(directory=STATIC), name="static"),
        ]
        return Guard(Starlette(routes=routes), self.signer)

    def startup(self) -> None:
        removed = self.store.cleanup()
        if removed:
            log.info("removed %d upload session(s) idle for more than 7 days", len(removed))
        # Sidecars whose render never finished.
        for sidecar in self.ws.renders.glob("*.json"):
            if not sidecar.with_suffix(".mp4").exists() and time.time() - sidecar.stat().st_mtime > 86400:
                sidecar.unlink(missing_ok=True)


def build_app(cfg: Config, editor: Editor | None = None):
    web = WebApp(cfg, editor)
    web.startup()
    return web.build()


def main(argv: list[str] | None = None) -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="video-mcp-web", description=__doc__)
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    cfg = Config.from_env()
    app = build_app(cfg)
    log.info("workspace: %s", cfg.workspace_dir)
    log.info("web app on http://%s:%d/ (home network only; do not port-forward)",
             cfg.web_host, cfg.web_port)
    uvicorn.run(app, host=cfg.web_host, port=cfg.web_port, log_level="info")


if __name__ == "__main__":
    main()
