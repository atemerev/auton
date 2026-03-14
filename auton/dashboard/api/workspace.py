"""Workspace file download endpoint."""

from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from auton.workspace import get_workspace_path

router = APIRouter(tags=["workspace"])


@router.get("/workspace/{agent_id}/{filepath:path}")
async def download_workspace_file(agent_id: str, filepath: str):
    """Download a file from an agent's workspace."""
    ws = get_workspace_path(agent_id)
    full_path = (ws / filepath).resolve()

    # Prevent path traversal
    if not str(full_path).startswith(str(ws.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")

    if not full_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        path=full_path,
        filename=Path(filepath).name,
        media_type="application/octet-stream",
    )
