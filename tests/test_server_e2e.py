"""Start the real server and drive it over MCP (streamable HTTP and stdio)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

try:  # newer SDKs; streamablehttp_client is deprecated there
    from mcp.client.streamable_http import streamable_http_client
except ImportError:  # pragma: no cover
    streamable_http_client = None

from conftest import decode_rgb, probe, region_diff

TOOLS = {
    "list_media", "probe_media", "get_frame", "create_project", "list_projects",
    "get_project", "add_clip", "update_clip", "move_clip", "remove_clip", "set_music",
    "add_text", "update_text", "remove_text", "render", "get_job",
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(workspace, host="127.0.0.1", token=None):
    port = free_port()
    env = {**os.environ, "WORKSPACE_DIR": str(workspace), "HOST": host, "PORT": str(port)}
    env.pop("AUTH_TOKEN", None)
    if token:
        env["AUTH_TOKEN"] = token
    # Prefer the console script installed next to this interpreter (e.g. in .venv/bin).
    exe = Path(sys.executable).parent / "video-mcp"
    cmd = [str(exe)] if exe.exists() else [sys.executable, "-m", "video_mcp"]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError("server exited:\n" + proc.stdout.read().decode())
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return proc, f"http://127.0.0.1:{port}/mcp"
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise AssertionError("server did not start")


@pytest.fixture
def server(workspace):
    proc, url = start_server(workspace)
    yield url
    proc.terminate()
    proc.wait(timeout=10)


class Client:
    def __init__(self, session: ClientSession):
        self.session = session

    async def call(self, _tool, **args):
        res = await self.session.call_tool(_tool, args)
        if res.isError:
            raise RuntimeError(res.content[0].text)
        if res.structuredContent is not None:
            sc = res.structuredContent
            return sc.get("result", sc) if set(sc) == {"result"} else sc
        return res

    async def error(self, _tool, **args) -> str:
        res = await self.session.call_tool(_tool, args)
        assert res.isError, f"{_tool} should have failed"
        return res.content[0].text


async def with_client(url, fn, headers=None):
    if streamable_http_client is not None:
        async with httpx.AsyncClient(headers=headers, timeout=60) as http:
            async with streamable_http_client(url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await fn(Client(session))
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(Client(session))


async def wait_done(c: Client, job_id: str, timeout=300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = await c.call("get_job", job_id=job_id)
        if job["status"] in ("done", "failed"):
            return job
        await asyncio.sleep(0.2)
    raise AssertionError("render timed out")


def frame_rgb(res):
    assert res.content[0].type == "image"
    assert res.content[0].mimeType == "image/jpeg"
    return decode_rgb(base64.b64decode(res.content[0].data))


def test_full_edit_over_http(server, workspace):
    """Done means #2 and #3: the documented tool sequence produces a playable
    1080x1920 MP4 of the right duration, and get_frame shows the text overlay."""

    async def scenario(c: Client):
        tools = {t.name for t in (await c.session.list_tools()).tools}
        assert TOOLS <= tools

        media = await c.call("list_media")
        assert {f["file"] for f in media["files"]} >= {"landscape.mp4", "portrait.mp4", "silent.mp4"}
        frame = await c.session.call_tool("get_frame", {"file": "landscape.mp4", "time": 1.0})
        w, h, _ = frame_rgb(frame)
        assert max(w, h) <= 768

        await c.call("create_project", name="trip", preset="vertical", fit="crop")
        a = await c.call("add_clip", project="trip", file="landscape.mp4")
        await c.call("add_clip", project="trip", file="silent.mp4")
        await c.call("add_clip", project="trip", file="portrait.mp4")
        # Trim the first clip to 1.0..3.0 s.
        await c.call("update_clip", project="trip", clip_id=a["clip"]["id"], start=1.0, end=3.0)
        await c.call("set_music", project="trip", file="music.m4a", volume=0.3, fade_in=1, fade_out=2)
        await c.call("add_text", project="trip", text="DAY ONE: it's 100%", start=0, end=2.5,
                     position="top", size="large", color="white", box=True)
        await c.call("add_text", project="trip", text="see you\nnext time", start=6, end=9,
                     position="bottom", size="medium", color="#ffcc00")
        proj = await c.call("get_project", name="trip")
        assert proj["output_duration"] == pytest.approx(2 + 3 + 4)

        assert "end" in await c.error("add_text", project="trip", text="x", start=8, end=12)
        assert "outside" in await c.error("probe_media", file="/etc/passwd")
        assert ".." in await c.error("get_frame", file="../../etc/passwd", time=0)

        job = await c.call("render", project="trip", quality="final")
        assert job["status"] in ("queued", "running")
        done = await wait_done(c, job["job_id"])
        assert done["status"] == "done", done

        info = probe(workspace / done["output"])
        assert (info["width"], info["height"]) == (1080, 1920)
        assert info["duration"] == pytest.approx(9, abs=0.2)
        assert info["has_audio"]

        with_text = frame_rgb(await c.session.call_tool("get_frame", {"file": done["output"], "time": 1.0}))
        projects = await c.call("list_projects")
        assert projects["projects"][0]["name"] == "trip"

        # Reference render of the same timeline without overlays.
        for t in proj["texts"]:
            await c.call("remove_text", project="trip", text_id=t["id"])
        ref = await wait_done(c, (await c.call("render", project="trip", quality="final"))["job_id"])
        assert ref["status"] == "done"
        without = frame_rgb(await c.session.call_tool("get_frame", {"file": ref["output"], "time": 1.0}))
        return with_text, without

    (w, h, rgb_text), (w2, h2, rgb_ref) = asyncio.run(with_client(server, scenario))
    assert (w, h) == (w2, h2) == (432, 768)
    top = region_diff(rgb_text, rgb_ref, w, int(h * 0.08), int(h * 0.2))
    middle = region_diff(rgb_text, rgb_ref, w, int(h * 0.4), int(h * 0.6))
    assert top > 15, f"text overlay not visible (diff {top:.1f})"
    assert middle < 3, f"unexpected change away from the overlay (diff {middle:.1f})"


def test_tool_errors_are_clear(server):
    async def scenario(c: Client):
        msg = await c.error("create_project", name="bad/name")
        assert "letters, digits" in msg
        msg = await c.error("get_project", name="nope")
        assert "create_project" in msg
        msg = await c.error("get_job", job_id="missing")
        assert "No job" in msg
        await c.call("create_project", name="ok")
        msg = await c.error("add_clip", project="ok", file="landscape.mp4", start=3, end=2)
        assert "greater than start" in msg
        msg = await c.error("render", project="ok")
        assert "no clips" in msg

    asyncio.run(with_client(server, scenario))


def test_bearer_token_required_on_public_host(workspace):
    proc, url = start_server(workspace, host="0.0.0.0", token="s3cret-token")
    try:
        req = urllib.request.Request(
            url, data=b"{}", method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        )
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code == 401
        req.add_header("Authorization", "Bearer wrong")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code == 401

        async def ok(c: Client):
            return await c.call("list_projects")

        res = asyncio.run(with_client(url, ok, headers={"Authorization": "Bearer s3cret-token"}))
        assert res["count"] == 0
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_stdio_transport(workspace):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "video_mcp", "--stdio"],
        env={**os.environ, "WORKSPACE_DIR": str(workspace)},
    )

    async def scenario():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                assert TOOLS <= tools
                res = await session.call_tool("probe_media", {"file": "portrait.mp4"})
                assert not res.isError
                return res.structuredContent

    info = asyncio.run(scenario())
    assert (info["width"], info["height"]) == (360, 640)
