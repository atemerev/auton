"""Workspace file and shell tools — closure-bound to agent workspace.

Factory functions return closures bound to a specific agent_id.
Each tool is sandboxed to the agent's workspace directory.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from auton.workspace import get_workspace_path, list_workspace_files


def _safe_resolve(workspace: Path, relative_path: str) -> Path | None:
    """Resolve a relative path within workspace. Returns None if it escapes."""
    try:
        ws_resolved = workspace.resolve()
        target = (workspace / relative_path).resolve()
        if not str(target).startswith(str(ws_resolved)):
            return None
        return target
    except Exception:
        return None


# ---------------------------------------------------------------------------
# File tools
# ---------------------------------------------------------------------------


def make_write_file(agent_id: str):
    """Create a write_file tool bound to an agent's workspace."""

    def write_file(path: str, content: str) -> str:
        """Write content to a file in the workspace.

        Creates parent directories as needed. Overwrites if file exists.

        Args:
            path: Relative file path within workspace (e.g. "src/main.py", "report.md")
            content: File content to write

        Returns:
            Confirmation with file path and size
        """
        ws = get_workspace_path(agent_id)
        ws.mkdir(parents=True, exist_ok=True)
        target = _safe_resolve(ws, path)
        if target is None:
            return json.dumps({"status": "error", "message": "Path escapes workspace boundary"})
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return json.dumps({"status": "OK", "path": path, "size": len(content)})

    return write_file


def make_read_file(agent_id: str):
    """Create a read_file tool bound to an agent's workspace."""

    def read_file(path: str) -> str:
        """Read a file from the workspace.

        Args:
            path: Relative file path within workspace

        Returns:
            File content or error message
        """
        ws = get_workspace_path(agent_id)
        target = _safe_resolve(ws, path)
        if target is None:
            return json.dumps({"status": "error", "message": "Path escapes workspace boundary"})
        if not target.exists():
            return json.dumps({"status": "error", "message": f"File not found: {path}"})
        try:
            content = target.read_text()
        except UnicodeDecodeError:
            return json.dumps({"status": "error", "message": f"Binary file, cannot read as text: {path}"})
        # Truncate very large files
        if len(content) > 100_000:
            content = content[:100_000] + "\n\n[... truncated at 100K chars ...]"
        return json.dumps({"status": "OK", "path": path, "size": len(content), "content": content})

    return read_file


def make_list_files(agent_id: str):
    """Create a list_files tool bound to an agent's workspace."""

    def list_files(subpath: str = "") -> str:
        """List files in the workspace.

        Args:
            subpath: Optional subdirectory to list (default: entire workspace)

        Returns:
            JSON with file paths, sizes, and modification times
        """
        if subpath:
            ws = get_workspace_path(agent_id)
            target = _safe_resolve(ws, subpath)
            if target is None:
                return json.dumps({"status": "error", "message": "Path escapes workspace"})
            if not target.exists():
                return json.dumps({"status": "OK", "files": [], "count": 0})
            files = []
            for item in sorted(target.rglob("*")):
                if item.is_file():
                    rel = item.relative_to(ws)
                    stat = item.stat()
                    files.append({
                        "path": str(rel),
                        "size": stat.st_size,
                    })
        else:
            files = list_workspace_files(agent_id)
        return json.dumps({"status": "OK", "files": files, "count": len(files)})

    return list_files


# ---------------------------------------------------------------------------
# Shell tool
# ---------------------------------------------------------------------------


def make_shell_exec(agent_id: str):
    """Create a shell_exec tool bound to an agent's workspace."""

    def shell_exec(command: str, timeout: int = 60) -> str:
        """Execute a shell command in the agent's workspace directory.

        The working directory is the agent's workspace. Use for git, build
        tools, scripts, file manipulation, etc.

        Args:
            command: Shell command to execute (e.g. "git status", "ls -la", "python script.py")
            timeout: Maximum seconds to wait (default: 60, max: 300)

        Returns:
            JSON with stdout, stderr, and return code
        """
        ws = get_workspace_path(agent_id)
        ws.mkdir(parents=True, exist_ok=True)
        timeout = min(max(timeout, 1), 300)

        # Block obviously destructive system-level commands
        blocked_patterns = ["rm -rf /", "sudo ", "mkfs ", "dd if=/dev"]
        cmd_lower = command.lower().strip()
        for pattern in blocked_patterns:
            if pattern in cmd_lower:
                return json.dumps({"status": "error", "message": f"Blocked command pattern: {pattern}"})

        try:
            env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(ws),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            # Truncate output to prevent token explosion
            stdout = result.stdout[-10_000:] if len(result.stdout) > 10_000 else result.stdout
            stderr = result.stderr[-5_000:] if len(result.stderr) > 5_000 else result.stderr
            return json.dumps({
                "status": "OK",
                "return_code": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
            })
        except subprocess.TimeoutExpired:
            return json.dumps({"status": "error", "message": f"Command timed out after {timeout}s"})
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})

    return shell_exec


# ---------------------------------------------------------------------------
# Artifact sharing
# ---------------------------------------------------------------------------


def make_publish_artifact(agent_id: str, agent_path: str, registry):
    """Create a publish_artifact tool that registers a workspace file as a named artifact."""

    def publish_artifact(
        file_path: str,
        name: str,
        description: str = "",
        mime_type: str = "text/markdown",
        tags: str = "[]",
    ) -> str:
        """Register a file in your workspace as a named artifact.

        Call this after writing a file to make it discoverable by parent agents
        and the API. Use descriptive names and tags for easy retrieval.

        Args:
            file_path: Relative path to the file in your workspace (must already exist)
            name: Human-readable artifact name (e.g. "Market Analysis Report")
            description: What this artifact contains
            mime_type: Content type (e.g. "text/markdown", "application/json", "text/csv")
            tags: JSON array of tags for categorization (e.g. '["research", "finance"]')

        Returns:
            Confirmation with artifact ID and metadata
        """
        from auton.models import ArtifactRecord, ArtifactStatus

        ws = get_workspace_path(agent_id)
        target = _safe_resolve(ws, file_path)
        if target is None:
            return json.dumps({"status": "error", "message": "Path escapes workspace boundary"})
        if not target.exists():
            return json.dumps({"status": "error", "message": f"File not found: {file_path}. Write the file first."})
        if not target.is_file():
            return json.dumps({"status": "error", "message": f"Not a file: {file_path}"})

        try:
            parsed_tags = json.loads(tags) if isinstance(tags, str) else tags
        except (json.JSONDecodeError, TypeError):
            parsed_tags = []

        file_size = target.stat().st_size
        node = registry.resolve(agent_path)

        # Check if this matches an expected artifact (by file_path)
        existing = None
        if node:
            for artifact in node.artifacts:
                if artifact.file_path == file_path and artifact.status == ArtifactStatus.EXPECTED:
                    existing = artifact
                    break

        if existing:
            existing.status = ArtifactStatus.PUBLISHED
            existing.file_size = file_size
            existing.name = name or existing.name
            existing.description = description or existing.description
            existing.mime_type = mime_type or existing.mime_type
            if parsed_tags:
                existing.tags = parsed_tags
            from datetime import datetime, timezone
            existing.updated_at = datetime.now(timezone.utc)
            artifact_id = existing.id
        else:
            record = ArtifactRecord(
                name=name,
                file_path=file_path,
                agent_id=agent_id,
                agent_path=agent_path,
                mime_type=mime_type,
                description=description,
                tags=parsed_tags,
                status=ArtifactStatus.PUBLISHED,
                file_size=file_size,
            )
            if node:
                node.artifacts.append(record)
            artifact_id = record.id

        return json.dumps({
            "status": "OK",
            "artifact_id": artifact_id,
            "name": name,
            "file_path": file_path,
            "file_size": file_size,
            "mime_type": mime_type,
        })

    return publish_artifact


def make_pass_artifact(agent_id: str, registry):
    """Create a pass_artifact tool that copies files to another agent's workspace."""

    def pass_artifact(file_path: str, target_agent_path: str, target_file_path: str = "") -> str:
        """Pass a file from this workspace to another agent's workspace.

        Use this to share documents, code, data, or results with parent agents,
        child agents, or sibling agents.

        Args:
            file_path: Relative path of the file in this workspace to share
            target_agent_path: Path of the target agent (e.g. "coordinator" or "coordinator/worker-2")
            target_file_path: Where to place in target workspace (default: same relative path)

        Returns:
            Confirmation with source and destination details
        """
        # Resolve source file
        src_ws = get_workspace_path(agent_id)
        src = _safe_resolve(src_ws, file_path)
        if src is None:
            return json.dumps({"status": "error", "message": "Source path escapes workspace"})
        if not src.exists():
            return json.dumps({"status": "error", "message": f"Source file not found: {file_path}"})
        if not src.is_file():
            return json.dumps({"status": "error", "message": f"Not a file: {file_path}"})

        # Resolve target agent
        target_node = registry.resolve(target_agent_path)
        if target_node is None:
            return json.dumps({"status": "error", "message": f"Target agent not found: {target_agent_path}"})

        # Copy to target workspace
        target_ws = get_workspace_path(target_node.id)
        target_ws.mkdir(parents=True, exist_ok=True)
        dest_rel = target_file_path or file_path
        dest = _safe_resolve(target_ws, dest_rel)
        if dest is None:
            return json.dumps({"status": "error", "message": "Target path escapes workspace"})

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dest))

        return json.dumps({
            "status": "OK",
            "source": file_path,
            "target_agent": target_agent_path,
            "target_path": dest_rel,
            "size": src.stat().st_size,
        })

    return pass_artifact
