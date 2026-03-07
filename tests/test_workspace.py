"""Tests for workspace file tools, shell execution, and artifact sharing."""

import asyncio
import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from auton.api import app, registry, _publish_event
from auton.workspace import (
    WORKSPACES_ROOT,
    cleanup_workspace,
    ensure_workspace,
    get_workspace_path,
    list_workspace_files,
)
from auton.tools.workspace_tools import (
    _safe_resolve,
    make_list_files,
    make_pass_artifact,
    make_read_file,
    make_shell_exec,
    make_write_file,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_workspaces():
    """Clean up test workspaces after each test."""
    yield
    # Remove any test workspaces
    for name in ["test-ws", "test-ws-2", "test-src", "test-target"]:
        cleanup_workspace(name)


# ---------------------------------------------------------------------------
# Workspace management
# ---------------------------------------------------------------------------


def test_ensure_workspace():
    """ensure_workspace creates the directory."""
    ws = ensure_workspace("test-ws")
    assert ws.exists()
    assert ws.is_dir()


def test_get_workspace_path_sanitizes():
    """Agent IDs with / or .. are sanitized."""
    path = get_workspace_path("../evil")
    assert ".." not in str(path.relative_to(WORKSPACES_ROOT))


def test_list_workspace_files_empty():
    """Empty workspace returns empty list."""
    ensure_workspace("test-ws")
    files = list_workspace_files("test-ws")
    assert files == []


def test_list_workspace_files():
    """Files are listed with metadata."""
    ws = ensure_workspace("test-ws")
    (ws / "hello.txt").write_text("hello")
    (ws / "sub").mkdir()
    (ws / "sub" / "nested.py").write_text("print('hi')")

    files = list_workspace_files("test-ws")
    paths = [f["path"] for f in files]
    assert "hello.txt" in paths
    assert "sub/nested.py" in paths
    assert len(files) == 2


# ---------------------------------------------------------------------------
# File tools
# ---------------------------------------------------------------------------


def test_write_and_read_file():
    """write_file creates a file, read_file reads it back."""
    ensure_workspace("test-ws")

    write = make_write_file("test-ws")
    result = json.loads(write("doc/report.md", "# Hello World\n\nSome content."))
    assert result["status"] == "OK"
    assert result["path"] == "doc/report.md"

    read = make_read_file("test-ws")
    result = json.loads(read("doc/report.md"))
    assert result["status"] == "OK"
    assert "# Hello World" in result["content"]


def test_read_file_not_found():
    """read_file returns error for missing files."""
    ensure_workspace("test-ws")
    read = make_read_file("test-ws")
    result = json.loads(read("nonexistent.txt"))
    assert result["status"] == "error"
    assert "not found" in result["message"].lower()


def test_path_escape_blocked():
    """Paths that escape the workspace are rejected."""
    ensure_workspace("test-ws")
    write = make_write_file("test-ws")
    result = json.loads(write("../../etc/passwd", "hacked"))
    assert result["status"] == "error"
    assert "escapes" in result["message"].lower()

    read = make_read_file("test-ws")
    result = json.loads(read("../../../etc/passwd"))
    assert result["status"] == "error"


def test_list_files_tool():
    """list_files tool returns workspace contents."""
    ws = ensure_workspace("test-ws")
    (ws / "a.txt").write_text("aaa")
    (ws / "b.txt").write_text("bbb")

    lf = make_list_files("test-ws")
    result = json.loads(lf())
    assert result["status"] == "OK"
    assert result["count"] == 2


def test_safe_resolve_blocks_traversal():
    """_safe_resolve returns None for path traversal attempts."""
    ws = ensure_workspace("test-ws")
    assert _safe_resolve(ws, "normal/file.txt") is not None
    assert _safe_resolve(ws, "../escape") is None
    assert _safe_resolve(ws, "../../etc/passwd") is None
    assert _safe_resolve(ws, "/absolute/path") is None


# ---------------------------------------------------------------------------
# Shell tool
# ---------------------------------------------------------------------------


def test_shell_exec_runs_in_workspace():
    """shell_exec runs commands in the workspace directory."""
    ws = ensure_workspace("test-ws")

    shell = make_shell_exec("test-ws")
    result = json.loads(shell("pwd"))
    assert result["status"] == "OK"
    assert result["return_code"] == 0
    assert str(ws.resolve()) in result["stdout"]


def test_shell_exec_creates_files():
    """shell_exec can create files via shell commands."""
    ensure_workspace("test-ws")

    shell = make_shell_exec("test-ws")
    result = json.loads(shell("echo 'hello world' > test.txt"))
    assert result["status"] == "OK"

    # Verify file exists
    read = make_read_file("test-ws")
    content = json.loads(read("test.txt"))
    assert "hello world" in content["content"]


def test_shell_exec_timeout():
    """shell_exec respects timeout."""
    ensure_workspace("test-ws")

    shell = make_shell_exec("test-ws")
    result = json.loads(shell("sleep 10", timeout=1))
    assert result["status"] == "error"
    assert "timed out" in result["message"].lower()


def test_shell_exec_blocks_dangerous():
    """shell_exec blocks obviously dangerous commands."""
    ensure_workspace("test-ws")

    shell = make_shell_exec("test-ws")
    result = json.loads(shell("sudo rm -rf /"))
    assert result["status"] == "error"
    assert "blocked" in result["message"].lower()


def test_shell_exec_captures_stderr():
    """shell_exec captures both stdout and stderr."""
    ensure_workspace("test-ws")

    shell = make_shell_exec("test-ws")
    result = json.loads(shell("echo 'out' && echo 'err' >&2"))
    assert result["status"] == "OK"
    assert "out" in result["stdout"]
    assert "err" in result["stderr"]


# ---------------------------------------------------------------------------
# Artifact sharing
# ---------------------------------------------------------------------------


def _spawn_no_executor(req, parent_path=None):
    """Spawn agent without starting executor (for sync unit tests)."""
    saved = registry.publish_event
    registry.publish_event = None
    try:
        return registry.spawn(req, parent_path=parent_path)
    finally:
        registry.publish_event = saved


def test_pass_artifact():
    """pass_artifact copies a file to another agent's workspace."""
    # Create source workspace with a file
    src_ws = ensure_workspace("test-src")
    (src_ws / "report.md").write_text("# Research Report\n\nFindings here.")

    # Create target agent in registry (no executor needed)
    registry.publish_event = _publish_event
    registry.db = None
    from auton.models import AgentSpec, SpawnRequest
    spec = AgentSpec(
        name="Target Agent",
        system_prompt="You are a target.",
        goal="Receive artifacts",
    )
    _spawn_no_executor(SpawnRequest(id="test-target", spec=spec))

    # Pass artifact
    pass_fn = make_pass_artifact("test-src", registry)
    result = json.loads(pass_fn("report.md", "test-target"))
    assert result["status"] == "OK"
    assert result["target_agent"] == "test-target"

    # Verify file exists in target workspace
    target_ws = get_workspace_path("test-target")
    assert (target_ws / "report.md").exists()
    assert "Research Report" in (target_ws / "report.md").read_text()


def test_pass_artifact_custom_target_path():
    """pass_artifact can place file at a different path in target workspace."""
    src_ws = ensure_workspace("test-src")
    (src_ws / "data.json").write_text('{"key": "value"}')

    registry.publish_event = _publish_event
    registry.db = None
    from auton.models import AgentSpec, SpawnRequest
    spec = AgentSpec(
        name="Target Agent",
        system_prompt="You are a target.",
        goal="Receive artifacts",
    )
    _spawn_no_executor(SpawnRequest(id="test-target", spec=spec))

    pass_fn = make_pass_artifact("test-src", registry)
    result = json.loads(pass_fn("data.json", "test-target", "incoming/results.json"))
    assert result["status"] == "OK"
    assert result["target_path"] == "incoming/results.json"

    target_ws = get_workspace_path("test-target")
    assert (target_ws / "incoming" / "results.json").exists()


def test_pass_artifact_agent_not_found():
    """pass_artifact returns error for nonexistent target agent."""
    src_ws = ensure_workspace("test-src")
    (src_ws / "file.txt").write_text("content")

    registry.publish_event = _publish_event
    registry.db = None

    pass_fn = make_pass_artifact("test-src", registry)
    result = json.loads(pass_fn("file.txt", "nonexistent-agent"))
    assert result["status"] == "error"
    assert "not found" in result["message"].lower()


# ---------------------------------------------------------------------------
# Integration: spawn agent with workspace tools via API
# ---------------------------------------------------------------------------


def _make_llm_response(content="", tool_calls=None):
    """Build a mock LLM API response."""
    msg = {"content": content, "tool_calls": tool_calls}
    resp = MagicMock()
    resp.model_dump.return_value = {
        "choices": [{"message": msg}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }
    return resp


@pytest.mark.asyncio
async def test_spawn_agent_with_workspace_tools():
    """POST /agents with workspace tools creates workspace and registers tools."""
    mock_resp = _make_llm_response(content="Done writing files.")
    registered_tools = []

    with patch("auton.llm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        from auton.llm import LLMClient
        original_add = LLMClient.add_tool

        def tracking_add(self, func, schema):
            registered_tools.append(schema["name"])
            return original_add(self, func, schema)

        with patch("auton.llm.LLMClient.add_tool", tracking_add):
            registry.publish_event = _publish_event
            registry.db = None

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/agents", json={
                    "id": "test-ws",
                    "spec": {
                        "name": "Workspace Agent",
                        "goal": "Write some files",
                        "system_prompt": "You write files to your workspace.",
                        "tools": ["write_file", "read_file", "list_files", "shell_exec"],
                    },
                })
                assert resp.status_code == 201
                await asyncio.sleep(0.5)

    # Workspace tools should be registered
    assert "write_file" in registered_tools
    assert "read_file" in registered_tools
    assert "list_files" in registered_tools
    assert "shell_exec" in registered_tools

    # Workspace directory should exist
    ws = get_workspace_path("test-ws")
    assert ws.exists()


@pytest.mark.asyncio
async def test_workspace_api_endpoint():
    """GET /agents/{id}/workspace returns workspace file listing."""
    mock_resp = _make_llm_response(content="Done.")

    with patch("auton.llm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        registry.publish_event = _publish_event
        registry.db = None

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Spawn agent
            resp = await client.post("/agents", json={
                "id": "test-ws",
                "spec": {
                    "name": "Workspace Agent",
                    "goal": "Test workspace",
                    "system_prompt": "You are a test agent.",
                    "tools": ["write_file"],
                },
            })
            assert resp.status_code == 201
            await asyncio.sleep(0.3)

            # Write a file to workspace manually
            ws = ensure_workspace("test-ws")
            (ws / "result.txt").write_text("test result")

            # Get workspace listing via API
            resp2 = await client.get("/agents/test-ws/workspace")
            assert resp2.status_code == 200
            data = resp2.json()
            assert data["agent"] == "test-ws"
            assert data["count"] >= 1
            paths = [f["path"] for f in data["files"]]
            assert "result.txt" in paths
