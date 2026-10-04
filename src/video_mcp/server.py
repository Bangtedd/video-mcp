"""MCP server: exposes the Editor as FastMCP tools over streamable HTTP or stdio."""

import argparse
import functools
import hmac
import logging
import sys
from typing import Any, Literal

import anyio
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError

from .config import Config, is_loopback
from .editor import Editor
from .errors import VideoMCPError

log = logging.getLogger("video_mcp")

INSTRUCTIONS = """\
You are the video editor. Source clips and music live in the workspace inbox
(list_media). Edits only change a JSON timeline; nothing is encoded until
render(). Typical flow: list_media -> get_frame to look at footage ->
create_project -> add_clip (trim with start/end) -> set_music -> add_text ->
render('preview') -> get_job until done -> get_frame on the render to check ->
render('final'). Times for clips are seconds in the source file; times for
texts are seconds on the output timeline (see get_project's output_duration).
Finishing: set_transition, set_look, set_gradient and set_fades. Shortcut:
list_templates -> apply_template(project, template_id, files, texts) builds a
whole project (suggest_segments picks the liveliest part of each clip); it can
then be adjusted with the other tools.
"""

mcp = FastMCP("video-mcp", instructions=INSTRUCTIONS)
_editor: Editor | None = None


def editor() -> Editor:
    global _editor
    if _editor is None:
        _editor = Editor(Config.from_env())
    return _editor


def set_editor(ed: Editor) -> None:
    global _editor
    _editor = ed


async def _call(fn, *args, **kwargs):
    """Run a blocking editor call off the event loop; turn errors into tool errors."""
    try:
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    except VideoMCPError as exc:
        raise ToolError(str(exc)) from None


Preset = Literal["vertical", "square", "landscape"]
Fit = Literal["crop", "pad"]
Position = Literal["top", "center", "bottom"]
Size = Literal["small", "medium", "large"]
Transition = Literal["cut", "fade", "slide_left", "slide_up", "zoom"]
Look = Literal["none", "warm", "cool", "vivid", "film", "bw", "moody"]
Gradient = Literal["none", "bottom", "top", "both"]


@mcp.tool()
async def list_media() -> dict[str, Any]:
    """List media files in the inbox with duration, resolution, fps and has_audio."""
    return await _call(editor().list_media)


@mcp.tool()
async def probe_media(file: str) -> dict[str, Any]:
    """Full details for one file (inbox name like 'clip.mp4', or 'renders/x.mp4')."""
    return await _call(editor().probe_media, file)


@mcp.tool()
async def get_frame(file: str, time: float) -> Image:
    """Return a JPEG frame (long side <= 768 px) at `time` seconds so you can see the
    footage. Works on inbox files and on renders ('renders/<name>.mp4')."""
    data = await _call(editor().get_frame, file, time)
    return Image(data=data, format="jpeg")


@mcp.tool()
async def create_project(name: str, preset: Preset = "vertical", fit: Fit = "crop") -> dict[str, Any]:
    """Create an empty project. preset: vertical 1080x1920, square 1080x1080,
    landscape 1920x1080 (all 30 fps). fit: crop (fill frame) or pad (black bars).
    Name: letters, digits, '-' and '_' only."""
    return await _call(editor().create_project, name, preset, fit)


@mcp.tool()
async def list_projects() -> dict[str, Any]:
    """List projects with their total output durations."""
    return await _call(editor().list_projects)


@mcp.tool()
async def get_project(name: str) -> dict[str, Any]:
    """Full timeline plus computed output duration and each clip's timeline position."""
    return await _call(editor().get_project, name)


@mcp.tool()
async def add_clip(
    project: str,
    file: str,
    start: float | None = None,
    end: float | None = None,
    position: int | None = None,
) -> dict[str, Any]:
    """Add a clip. start/end are seconds in the source (default: whole file).
    position is a 0-based index in the clip list (default: append at the end)."""
    return await _call(editor().add_clip, project, file, start, end, position)


@mcp.tool()
async def update_clip(
    project: str,
    clip_id: str,
    start: float | None = None,
    end: float | None = None,
    speed: float | None = None,
    volume: float | None = None,
) -> dict[str, Any]:
    """Change a clip's trim (start/end in source seconds), speed (0.25-4.0) or
    volume (0.0-2.0). Omitted fields are left unchanged."""
    return await _call(editor().update_clip, project, clip_id, start, end, speed, volume)


@mcp.tool()
async def move_clip(project: str, clip_id: str, position: int) -> dict[str, Any]:
    """Move a clip to a new 0-based position in the clip list."""
    return await _call(editor().move_clip, project, clip_id, position)


@mcp.tool()
async def remove_clip(project: str, clip_id: str) -> dict[str, Any]:
    """Remove a clip from the timeline (the source file is untouched)."""
    return await _call(editor().remove_clip, project, clip_id)


@mcp.tool()
async def set_music(
    project: str,
    file: str | None,
    volume: float = 0.3,
    offset: float = 0.0,
    fade_in: float = 0.0,
    fade_out: float = 0.0,
) -> dict[str, Any]:
    """Set background music mixed under the clip audio. offset = seconds into the
    music file to start from. Music is trimmed to the video length. Pass
    file=null to remove the music."""
    return await _call(editor().set_music, project, file, volume, offset, fade_in, fade_out)


@mcp.tool()
async def add_text(
    project: str,
    text: str,
    start: float,
    end: float,
    position: Position = "bottom",
    size: Size = "medium",
    color: str = "white",
    box: bool = False,
    fade: bool = False,
) -> dict[str, Any]:
    """Add a text overlay shown from start to end (seconds on the output timeline).
    Newlines are kept; long lines wrap automatically. color: name or '#rrggbb'.
    box: semi-transparent dark background behind the text. fade: 0.3 s fade in/out."""
    return await _call(
        editor().add_text, project, text, start, end, position, size, color, box, fade
    )


@mcp.tool()
async def update_text(
    project: str,
    text_id: str,
    text: str | None = None,
    start: float | None = None,
    end: float | None = None,
    position: Position | None = None,
    size: Size | None = None,
    color: str | None = None,
    box: bool | None = None,
    fade: bool | None = None,
) -> dict[str, Any]:
    """Change any field of a text overlay. Omitted fields are left unchanged."""
    return await _call(
        editor().update_text, project, text_id, text, start, end, position, size, color, box, fade
    )


@mcp.tool()
async def remove_text(project: str, text_id: str) -> dict[str, Any]:
    """Remove a text overlay."""
    return await _call(editor().remove_text, project, text_id)


@mcp.tool()
async def set_transition(project: str, type: Transition = "cut", duration: float = 0.5) -> dict[str, Any]:
    """Transition used between every pair of clips: cut, fade, slide_left, slide_up
    or zoom; duration 0.2-1.0 s. Transitions overlap clips, so the video gets
    shorter (get_project reports the new output_duration). A join where either
    clip is shorter than twice the duration falls back to a cut."""
    return await _call(editor().set_transition, project, type, duration)


@mcp.tool()
async def set_look(project: str, look: Look) -> dict[str, Any]:
    """Colour look for the whole video: none, warm, cool, vivid, film (faded matte),
    bw (black and white) or moody (darker, contrasty, slight vignette)."""
    return await _call(editor().set_look, project, look)


@mcp.tool()
async def set_gradient(project: str, gradient: Gradient) -> dict[str, Any]:
    """Dark-to-transparent gradient over 35% of the frame at the bottom, top, both
    or none, drawn under the text so captions stay readable."""
    return await _call(editor().set_gradient, project, gradient)


@mcp.tool()
async def set_fades(project: str, fade_in: float = 0.0, fade_out: float = 0.0) -> dict[str, Any]:
    """Fade the whole video (picture and sound) in from black at the start and out
    at the end. Each 0-2 seconds; 0 disables."""
    return await _call(editor().set_fades, project, fade_in, fade_out)


@mcp.tool()
async def suggest_segments(file: str, length: float, count: int = 1) -> dict[str, Any]:
    """Best `count` non-overlapping windows of `length` seconds in a clip, scored by
    motion (skipping the first and last 0.5 s). Use the start/end with add_clip.
    method is 'motion', or 'even' when the file is short, static or unanalysable."""
    return await _call(editor().suggest_segments, file, length, count)


@mcp.tool()
async def list_templates() -> dict[str, Any]:
    """Templates (from templates/*.json): id, name, description, nominal duration
    and the text fields each one takes."""
    return await _call(editor().list_templates)


@mcp.tool()
async def apply_template(
    project: str,
    template_id: str,
    files: list[str],
    texts: dict[str, str] | None = None,
    music: str | None = None,
) -> dict[str, Any]:
    """Build a project from a template. files: clips in order (fewer than the
    template's slots cycle through, more repeat the slot pattern; an audio-only
    file is used as music). texts: {key: text} for the template's text fields;
    empty ones are skipped. Creates the project, or replaces its timeline if it
    exists. The result is a normal project you can still edit."""
    return await _call(editor().apply_template, project, template_id, files, texts, music)


@mcp.tool()
async def render(project: str, quality: Literal["preview", "final"] = "preview") -> dict[str, Any]:
    """Start a background render and return its job_id immediately. 'preview' is half
    resolution and fast; 'final' is full quality. One render runs at a time; others
    queue. Poll get_job(job_id)."""
    return await _call(editor().render, project, quality)


@mcp.tool()
async def get_job(job_id: str) -> dict[str, Any]:
    """Render job status: queued, running, done or failed; progress 0-100; output
    path (pass it to get_frame / probe_media); on failure, error and FFmpeg stderr."""
    return await _call(editor().get_job, job_id)


# ------------------------------------------------------------------ HTTP


class BearerAuth:
    """ASGI middleware requiring `Authorization: Bearer <token>` on HTTP requests."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope.get("headers") or []).get(b"authorization", b"")
            scheme, _, supplied = auth.partition(b" ")
            if scheme.lower() != b"bearer" or not hmac.compare_digest(supplied.strip(), self.token):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b'Bearer realm="video-mcp"'),
                        ],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"error": "missing or invalid bearer token"}',
                    }
                )
                return
        await self.app(scope, receive, send)


def build_http_app(cfg: Config):
    from mcp.server.transport_security import TransportSecuritySettings

    mcp.settings.host = cfg.host
    mcp.settings.port = cfg.port
    if is_loopback(cfg.host):
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        )
    else:
        # Exposed on the network: access is controlled by the bearer token instead.
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
    app = mcp.streamable_http_app()
    if cfg.auth_token:
        app = BearerAuth(app, cfg.auth_token)
    return app


def check_bind(cfg: Config) -> None:
    if not is_loopback(cfg.host) and not cfg.auth_token:
        raise SystemExit(
            f"Refusing to listen on non-loopback HOST={cfg.host} without AUTH_TOKEN. "
            "Set AUTH_TOKEN to a long random string (clients must send it as a bearer token), "
            "or use HOST=127.0.0.1."
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="video-mcp", description=__doc__)
    parser.add_argument("--stdio", action="store_true", help="use stdio transport instead of HTTP")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    cfg = Config.from_env()
    set_editor(Editor(cfg))
    log.info("workspace: %s", editor().ws.root)

    if args.stdio:
        mcp.run("stdio")
        return

    check_bind(cfg)
    import uvicorn

    log.info("listening on http://%s:%d/mcp", cfg.host, cfg.port)
    uvicorn.run(build_http_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
