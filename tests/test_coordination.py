"""Tests for agent coordination tools: spawn_child, message, list_children."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from auton.api import app, registry, _publish_event
from auton.models import AgentNode, AgentSpec, AgentState, IdleReason, SpawnRequest
from auton.tools.coordination import (
    make_check_child_status,
    make_list_children,
    make_message_child,
    make_read_child_file,
    make_spawn_child,
)
from auton.budget import BudgetPlanner
from auton.workspace import cleanup_workspace, ensure_workspace, get_workspace_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_workspaces():
    """Clean up test workspaces after each test."""
    yield
    for name in ["test-parent", "child-1", "child-2", "child-0", "test-coord"]:
        cleanup_workspace(name)


def _spawn_no_executor(req: SpawnRequest, parent_path=None):
    """Spawn an agent without starting an executor (for sync unit tests)."""
    saved = registry.publish_event
    registry.publish_event = None
    try:
        return registry.spawn(req, parent_path=parent_path)
    finally:
        registry.publish_event = saved


# ---------------------------------------------------------------------------
# Unit tests for coordination tools
# ---------------------------------------------------------------------------


def test_spawn_child_via_tool():
    """spawn_child creates a child agent in the tree."""
    registry.publish_event = _publish_event
    registry.db = None

    # Create parent agent (no executor)
    parent_spec = AgentSpec(
        name="Parent",
        system_prompt="You are a coordinator.",
        goal="Manage children",
    )
    parent = _spawn_no_executor(SpawnRequest(id="test-parent", spec=parent_spec))
    assert parent.state == AgentState.RUNNING

    # Use spawn_child tool — it will also spawn without executor since publish_event
    # triggers _start_executor which needs an event loop. We mock it.
    with patch.object(registry, '_start_executor'):
        spawn_fn = make_spawn_child("test-parent", "test-parent", registry)
        result = json.loads(spawn_fn(
            child_id="child-1",
            name="Worker 1",
            system_prompt="You are a worker agent.",
            goal="Do some work",
            tools='["write_file", "shell_exec"]',
        ))

    assert result["status"] == "OK"
    assert result["child_id"] == "child-1"
    assert result["child_path"] == "test-parent/child-1"

    # Verify child is in the tree
    child = registry.resolve("test-parent/child-1")
    assert child is not None
    assert child.spec.name == "Worker 1"
    assert child.spec.goal == "Do some work"
    assert "write_file" in child.spec.tools
    assert "shell_exec" in child.spec.tools


def test_spawn_child_max_children():
    """spawn_child respects max_children limit."""
    registry.publish_event = _publish_event
    registry.db = None

    parent_spec = AgentSpec(
        name="Parent",
        system_prompt="You are a coordinator.",
        goal="Manage children",
        max_children=1,
    )
    parent = _spawn_no_executor(SpawnRequest(id="test-parent", spec=parent_spec))

    with patch.object(registry, '_start_executor'):
        spawn_fn = make_spawn_child("test-parent", "test-parent", registry)

        # First child should succeed
        result = json.loads(spawn_fn("child-1", "Worker 1", "Prompt", "Goal"))
        assert result["status"] == "OK"

        # Second child should fail (max_children=1)
        result = json.loads(spawn_fn("child-2", "Worker 2", "Prompt", "Goal"))
        assert result["status"] == "error"
        assert "max_children" in result["message"]


def test_message_child_via_tool():
    """message_agent delivers message to target agent."""
    registry.publish_event = _publish_event
    registry.db = None

    # Create parent and child (no executors)
    parent_spec = AgentSpec(
        name="Parent",
        system_prompt="Coordinator.",
        goal="Manage",
    )
    _spawn_no_executor(SpawnRequest(id="test-parent", spec=parent_spec))

    child_spec = AgentSpec(
        name="Child",
        system_prompt="Worker.",
        goal="Work",
    )
    _spawn_no_executor(SpawnRequest(id="child-1", spec=child_spec), parent_path="test-parent")

    # Send message
    msg_fn = make_message_child("test-parent", registry)
    result = json.loads(msg_fn("test-parent/child-1", "Please do X next"))
    assert result["status"] == "OK"
    assert result["delivered"] is True

    # Verify message is in child's messages
    child = registry.resolve("test-parent/child-1")
    assert len(child.messages) == 1
    assert child.messages[0]["content"] == "Please do X next"
    assert child.messages[0]["channel"] == "agent"


def test_message_agent_not_found():
    """message_agent returns error for nonexistent agent."""
    registry.publish_event = _publish_event
    registry.db = None

    msg_fn = make_message_child("test-parent", registry)
    result = json.loads(msg_fn("nonexistent/agent", "hello"))
    assert result["status"] == "error"


def test_list_children():
    """list_children returns info about all child agents."""
    registry.publish_event = _publish_event
    registry.db = None

    # Create parent with children (no executors)
    parent_spec = AgentSpec(
        name="Parent",
        system_prompt="Coordinator.",
        goal="Manage",
        max_children=10,
    )
    _spawn_no_executor(SpawnRequest(id="test-parent", spec=parent_spec))

    for i in range(3):
        child_spec = AgentSpec(
            name=f"Worker {i}",
            system_prompt="Worker.",
            goal=f"Task {i}",
        )
        _spawn_no_executor(SpawnRequest(id=f"child-{i}", spec=child_spec), parent_path="test-parent")

    # List children
    list_fn = make_list_children("test-parent", "test-parent", registry)
    result = json.loads(list_fn())
    assert result["status"] == "OK"
    assert result["count"] == 3
    assert len(result["children"]) == 3

    # Verify child info
    child_ids = [c["id"] for c in result["children"]]
    assert "child-0" in child_ids
    assert "child-1" in child_ids
    assert "child-2" in child_ids

    for child in result["children"]:
        assert "state" in child
        assert "name" in child
        assert "goal" in child


def test_check_child_status():
    """check_child_status returns detailed agent status."""
    registry.publish_event = _publish_event
    registry.db = None

    parent_spec = AgentSpec(
        name="Parent",
        system_prompt="Coordinator.",
        goal="Manage",
    )
    _spawn_no_executor(SpawnRequest(id="test-parent", spec=parent_spec))

    child_spec = AgentSpec(
        name="Worker",
        system_prompt="Worker.",
        goal="Do research",
    )
    _spawn_no_executor(SpawnRequest(id="child-1", spec=child_spec), parent_path="test-parent")

    check_fn = make_check_child_status("test-parent", registry)
    result = json.loads(check_fn("test-parent/child-1"))
    assert result["status"] == "OK"
    assert result["state"] == "running"
    assert result["name"] == "Worker"
    assert result["goal"] == "Do research"


def test_check_child_status_idle_includes_response():
    """check_child_status includes last_response when agent is idle."""
    registry.publish_event = _publish_event
    registry.db = None

    parent_spec = AgentSpec(
        name="Parent",
        system_prompt="Coordinator.",
        goal="Manage",
    )
    _spawn_no_executor(SpawnRequest(id="test-parent", spec=parent_spec))

    child_spec = AgentSpec(
        name="Worker",
        system_prompt="Worker.",
        goal="Work",
    )
    child = _spawn_no_executor(SpawnRequest(id="child-1", spec=child_spec), parent_path="test-parent")

    # Simulate idle state with conversation
    child.conversation = [
        {"role": "user", "content": "Do the task"},
        {"role": "assistant", "content": "Task completed successfully. Here are my findings."},
    ]
    child.idle_reason = IdleReason.COMPLETED
    child.transition(AgentState.IDLE)

    check_fn = make_check_child_status("test-parent", registry)
    result = json.loads(check_fn("test-parent/child-1"))
    assert result["status"] == "OK"
    assert result["state"] == "idle"
    assert result["idle_reason"] == "completed"
    assert "last_response" in result
    assert "Task completed" in result["last_response"]


def test_check_child_not_found():
    """check_child_status returns error for nonexistent agent."""
    registry.publish_event = _publish_event
    registry.db = None

    check_fn = make_check_child_status("test-parent", registry)
    result = json.loads(check_fn("nonexistent"))
    assert result["status"] == "error"


def test_read_child_file():
    """read_child_file reads a file from a child agent's workspace."""
    registry.publish_event = _publish_event
    registry.db = None

    parent_spec = AgentSpec(name="Parent", system_prompt="Coord.", goal="Manage")
    _spawn_no_executor(SpawnRequest(id="test-parent", spec=parent_spec))

    child_spec = AgentSpec(name="Worker", system_prompt="Worker.", goal="Work")
    _spawn_no_executor(SpawnRequest(id="child-1", spec=child_spec), parent_path="test-parent")

    # Write a file to child's workspace
    ensure_workspace("child-1")
    ws = get_workspace_path("child-1")
    (ws / "dossier.md").write_text("# Dossier\nSome findings here.")

    read_fn = make_read_child_file("test-parent", registry)
    result = json.loads(read_fn("test-parent/child-1", "dossier.md"))
    assert result["status"] == "OK"
    assert "# Dossier" in result["content"]
    assert result["child"] == "test-parent/child-1"


def test_read_child_file_not_found():
    """read_child_file returns error for missing file or agent."""
    registry.publish_event = _publish_event
    registry.db = None

    # Nonexistent agent
    read_fn = make_read_child_file("test-parent", registry)
    result = json.loads(read_fn("nonexistent", "dossier.md"))
    assert result["status"] == "error"

    # Existing agent, missing file
    parent_spec = AgentSpec(name="Parent", system_prompt="Coord.", goal="Manage")
    _spawn_no_executor(SpawnRequest(id="test-parent", spec=parent_spec))
    child_spec = AgentSpec(name="Worker", system_prompt="Worker.", goal="Work")
    _spawn_no_executor(SpawnRequest(id="child-1", spec=child_spec), parent_path="test-parent")
    ensure_workspace("child-1")

    result = json.loads(read_fn("test-parent/child-1", "nonexistent.md"))
    assert result["status"] == "error"
    assert "not found" in result["message"].lower()


def test_spawn_child_reserves_budget():
    """spawn_child deducts max_tokens from parent's budget planner."""
    registry.publish_event = _publish_event
    registry.db = None

    parent_spec = AgentSpec(
        name="Parent",
        system_prompt="Coordinator.",
        goal="Manage",
        max_tokens=300_000,
    )
    _spawn_no_executor(SpawnRequest(id="test-parent", spec=parent_spec))

    planner = BudgetPlanner(max_budget=300_000)
    planner.record_call(10_000)  # simulate some initial spending

    with patch.object(registry, '_start_executor'):
        spawn_fn = make_spawn_child("test-parent", "test-parent", registry, budget_planner=planner)

        result = json.loads(spawn_fn(
            child_id="child-1",
            name="Worker 1",
            system_prompt="Worker.",
            goal="Do work",
            max_tokens=50_000,
        ))

    assert result["status"] == "OK"
    assert result["budget_reserved"] == 50_000
    assert planner.reserved_for_children == 50_000
    assert planner.remaining == 240_000  # 300K - 10K spent - 50K reserved


def test_spawn_child_budget_exceeded():
    """spawn_child rejects child if budget insufficient."""
    registry.publish_event = _publish_event
    registry.db = None

    parent_spec = AgentSpec(
        name="Parent",
        system_prompt="Coordinator.",
        goal="Manage",
    )
    _spawn_no_executor(SpawnRequest(id="test-parent", spec=parent_spec))

    planner = BudgetPlanner(max_budget=100_000)
    planner.record_call(80_000)  # only 20K remaining

    with patch.object(registry, '_start_executor'):
        spawn_fn = make_spawn_child("test-parent", "test-parent", registry, budget_planner=planner)

        result = json.loads(spawn_fn(
            child_id="child-1",
            name="Worker 1",
            system_prompt="Worker.",
            goal="Do work",
            max_tokens=50_000,  # needs 50K but only 20K available
        ))

    assert result["status"] == "error"
    assert "Cannot reserve" in result["message"]
    # Reservation should not have been applied
    assert planner.reserved_for_children == 0


def test_spawn_child_no_planner_no_reservation():
    """spawn_child without planner works as before (no reservation)."""
    registry.publish_event = _publish_event
    registry.db = None

    parent_spec = AgentSpec(
        name="Parent",
        system_prompt="Coordinator.",
        goal="Manage",
    )
    _spawn_no_executor(SpawnRequest(id="test-parent", spec=parent_spec))

    with patch.object(registry, '_start_executor'):
        spawn_fn = make_spawn_child("test-parent", "test-parent", registry)  # no planner

        result = json.loads(spawn_fn(
            child_id="child-1",
            name="Worker 1",
            system_prompt="Worker.",
            goal="Do work",
            max_tokens=50_000,
        ))

    assert result["status"] == "OK"
    assert "budget_reserved" not in result


# ---------------------------------------------------------------------------
# Integration: coordinator agent spawns children via API
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
async def test_spawn_coordinator_with_coordination_tools():
    """POST /agents with coordination tools registers them correctly."""
    mock_resp = _make_llm_response(content="Spawned children.")
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
                    "id": "test-coord",
                    "spec": {
                        "name": "Coordinator",
                        "goal": "Manage sub-agents",
                        "system_prompt": "You spawn and coordinate child agents.",
                        "tools": [
                            "spawn_child", "message_agent",
                            "list_children", "check_child_status",
                        ],
                    },
                })
                assert resp.status_code == 201
                await asyncio.sleep(0.5)

    # Coordination tools should be registered
    assert "spawn_child" in registered_tools
    assert "message_agent" in registered_tools
    assert "list_children" in registered_tools
    assert "check_child_status" in registered_tools


# ---------------------------------------------------------------------------
# IdleReason tests
# ---------------------------------------------------------------------------


def test_idle_reason_in_to_dict():
    """idle_reason appears in to_dict() when set."""
    spec = AgentSpec(name="Test", system_prompt="Test.", goal="Test")
    node = AgentNode(id="test-idle", spec=spec)
    node.state = AgentState.IDLE
    node.idle_reason = IdleReason.COMPLETED

    d = node.to_dict()
    assert d["state"] == "idle"
    assert d["idle_reason"] == "completed"


def test_idle_reason_absent_when_none():
    """idle_reason is omitted from to_dict() when not set."""
    spec = AgentSpec(name="Test", system_prompt="Test.", goal="Test")
    node = AgentNode(id="test-idle", spec=spec)
    node.state = AgentState.IDLE

    d = node.to_dict()
    assert "idle_reason" not in d


def test_idle_reason_cleared_on_message_wakeup():
    """Sending a message to an idle agent clears idle_reason."""
    registry.publish_event = _publish_event
    registry.db = None

    spec = AgentSpec(name="Test", system_prompt="Test.", goal="Test")
    node = _spawn_no_executor(SpawnRequest(id="test-idle", spec=spec))
    node.transition(AgentState.IDLE)
    node.idle_reason = IdleReason.TURNS_EXHAUSTED

    registry.send_message("test-idle", "continue please")
    assert node.state == AgentState.RUNNING
    assert node.idle_reason is None


def test_idle_reason_cleared_on_correction():
    """Correcting an idle agent clears idle_reason."""
    registry.publish_event = _publish_event
    registry.db = None

    spec = AgentSpec(name="Test", system_prompt="Test.", goal="Test")
    node = _spawn_no_executor(SpawnRequest(id="test-idle", spec=spec))
    node.transition(AgentState.IDLE)
    node.idle_reason = IdleReason.BUDGET_EXHAUSTED

    registry.correct("test-idle", "try a different approach")
    assert node.state == AgentState.RUNNING
    assert node.idle_reason is None
