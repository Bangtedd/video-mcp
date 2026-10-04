"""Runtime configuration from environment variables."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
# templates/ at the repo root (the package lives in <repo>/src/video_mcp/).
DEFAULT_TEMPLATES = Path(__file__).resolve().parents[2] / "templates"


def _port(env: dict, name: str, default: str) -> int:
    raw = env.get(name) or default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}")


@dataclass
class Config:
    workspace_dir: Path
    font_file: str = DEFAULT_FONT
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str | None = None
    templates_dir: Path = DEFAULT_TEMPLATES
    web_host: str = "0.0.0.0"
    web_port: int = 8780
    web_password: str | None = None
    web_max_upload_mb: int = 500
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        workspace = env.get("WORKSPACE_DIR") or "~/video-mcp/workspace"
        return cls(
            workspace_dir=Path(workspace).expanduser(),
            font_file=env.get("FONT_FILE") or DEFAULT_FONT,
            ffmpeg=env.get("FFMPEG") or "ffmpeg",
            ffprobe=env.get("FFPROBE") or "ffprobe",
            host=env.get("HOST") or "127.0.0.1",
            port=_port(env, "PORT", "8765"),
            auth_token=env.get("AUTH_TOKEN") or None,
            templates_dir=Path(env.get("TEMPLATES_DIR") or DEFAULT_TEMPLATES).expanduser(),
            web_host=env.get("WEB_HOST") or "0.0.0.0",
            web_port=_port(env, "WEB_PORT", "8780"),
            web_password=env.get("WEB_PASSWORD") or None,
            web_max_upload_mb=_port(env, "WEB_MAX_UPLOAD_MB", "500"),
        )


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False
