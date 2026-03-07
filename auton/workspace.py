"""Workspace management for agent file artifacts.

Each agent gets an isolated workspace directory at ./workspaces/{agent-id}/.
Files are the primary artifact format — easy to inspect, share, and version.
"""

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

WORKSPACES_ROOT = Path("./workspaces")


def get_workspace_path(agent_id: str) -> Path:
    """Return the workspace path for an agent. Does not create it."""
    # Sanitize agent_id to prevent path traversal
    safe_id = agent_id.replace("/", "_").replace("..", "_")
    return WORKSPACES_ROOT / safe_id


def ensure_workspace(agent_id: str) -> Path:
    """Create workspace directory if it doesn't exist. Returns the path."""
    ws = get_workspace_path(agent_id)
    ws.mkdir(parents=True, exist_ok=True)
    logger.info(f"Workspace ready: {ws}")
    return ws


def cleanup_workspace(agent_id: str) -> None:
    """Remove an agent's workspace directory."""
    ws = get_workspace_path(agent_id)
    if ws.exists():
        shutil.rmtree(ws)
        logger.info(f"Workspace removed: {ws}")


def list_workspace_files(agent_id: str) -> list[dict]:
    """List files in an agent's workspace with basic metadata."""
    ws = get_workspace_path(agent_id)
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
