"""Workspace layout and safe path resolution.

Every file argument coming from a tool call goes through `Workspace.resolve_*`,
which guarantees the resulting path stays inside WORKSPACE_DIR (after following
symlinks).
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from .errors import VideoMCPError

SUBDIRS = ("inbox", "projects", "renders", "cache")
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class Workspace:
    def __init__(self, root: Path):
        root = Path(root).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()
        for sub in SUBDIRS:
            (self.root / sub).mkdir(exist_ok=True)

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def projects(self) -> Path:
        return self.root / "projects"

    @property
    def renders(self) -> Path:
        return self.root / "renders"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def render_lock(self) -> Path:
        """Lock file serialising FFmpeg renders across processes (MCP server, web app)."""
        return self.root / ".render.lock"

    def relative(self, path: Path) -> str:
        """Workspace-relative POSIX path, as shown to the client."""
        return Path(path).resolve().relative_to(self.root).as_posix()

    def _contained(self, candidate: Path, original: str) -> Path:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise VideoMCPError(
                f"File {original!r} resolves outside the workspace. "
                "Use a path inside the workspace, e.g. 'clip.mp4' (from inbox/) "
                "or 'renders/<name>.mp4'."
            )
        return resolved

    def resolve_file(self, name: str) -> Path:
        """Resolve a user-supplied file name to an existing file in the workspace.

        Plain names are looked up in inbox/ first, then relative to the
        workspace root (so 'renders/x.mp4' works). Absolute paths are accepted
        only if they point inside the workspace.
        """
        if not isinstance(name, str) or not name.strip():
            raise VideoMCPError("File name is empty. Pass a file name from list_media().")
        if "\x00" in name:
            raise VideoMCPError("File name contains a NUL byte.")
        parts = PurePosixPath(name.replace("\\", "/")).parts
        if ".." in parts:
            raise VideoMCPError(
                f"File {name!r} contains '..'. Use a plain file name from list_media()."
            )
        if os.path.isabs(name):
            path = self._contained(Path(name), name)
            if not path.is_file():
                raise VideoMCPError(f"File {name!r} does not exist.")
            return path

        candidates = [self.inbox / name, self.root / name]
        for cand in candidates:
            if cand.exists() or cand.is_symlink():
                path = self._contained(cand, name)
                if path.is_file():
                    return path
        raise VideoMCPError(
            f"File {name!r} not found in inbox/ or the workspace. "
            "Call list_media() to see available files."
        )

    def project_path(self, name: str) -> Path:
        check_project_name(name)
        return self.projects / f"{name}.json"


def check_project_name(name: str) -> None:
    if not isinstance(name, str) or not PROJECT_NAME_RE.match(name):
        raise VideoMCPError(
            f"Invalid project name {name!r}. Use 1-64 letters, digits, '-' or '_' only."
        )
