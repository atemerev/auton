"""Workspace management for agent file artifacts.

Workspace is per-user: all agents of a user share one workspace directory
(and one container). Falls back to per-agent workspace for agents without
a user_id.
"""

import logging
import shutil
from pathlib import Path

from auton.containers import WORKSPACES_ROOT

logger = logging.getLogger(__name__)


def get_workspace_path(agent_id: str, user_id: str | None = None) -> Path:
    """Return the host-side workspace path.

    If user_id is set, workspace is shared across all the user's agents.
    Otherwise falls back to per-agent workspace (legacy / no-auth mode).
    """
    key = user_id or agent_id
    safe = key.replace("/", "_").replace("..", "_")
    return WORKSPACES_ROOT / safe


def ensure_workspace(agent_id: str, user_id: str | None = None) -> Path:
    """Create workspace directory if needed. Returns the path."""
    ws = get_workspace_path(agent_id, user_id)
    ws.mkdir(parents=True, exist_ok=True)
    logger.info(f"Workspace ready: {ws}")
    return ws


def cleanup_workspace(agent_id: str, user_id: str | None = None) -> None:
    """Remove a workspace directory."""
    ws = get_workspace_path(agent_id, user_id)
    if ws.exists():
        shutil.rmtree(ws)
        logger.info(f"Workspace removed: {ws}")


def list_workspace_files(agent_id: str, user_id: str | None = None) -> list[dict]:
    """List files in a workspace with basic metadata."""
    ws = get_workspace_path(agent_id, user_id)
    if not ws.exists():
        return []
    files = []
    for item in sorted(ws.rglob("*")):
        if item.is_file():
            rel = item.relative_to(ws)
            stat = item.stat()
            files.append({
                "path": str(rel),
                "size": stat.st_size,
                "modified": stat.st_mtime,
            })
    return files
