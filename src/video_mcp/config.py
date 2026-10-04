"""Runtime configuration from environment variables."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


@dataclass
class Config:
    workspace_dir: Path
    font_file: str = DEFAULT_FONT
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str | None = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        workspace = env.get("WORKSPACE_DIR") or "~/video-mcp/workspace"
        port_raw = env.get("PORT") or "8765"
        try:
            port = int(port_raw)
        except ValueError:
            raise SystemExit(f"PORT must be an integer, got {port_raw!r}")
        return cls(
            workspace_dir=Path(workspace).expanduser(),
            font_file=env.get("FONT_FILE") or DEFAULT_FONT,
            ffmpeg=env.get("FFMPEG") or "ffmpeg",
            ffprobe=env.get("FFPROBE") or "ffprobe",
            host=env.get("HOST") or "127.0.0.1",
            port=port,
            auth_token=env.get("AUTH_TOKEN") or None,
        )


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False
