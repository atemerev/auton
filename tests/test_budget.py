"""Tests for budget-aware execution: check_budget tool, escalating warnings, planner integration."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auton.budget import BudgetPlanner
from auton.executor import (
    BUDGET_FINAL_PCT,
    BUDGET_HARD_STOP_PCT,
    BUDGET_URGENT_PCT,
    BUDGET_WARN_PCT,
    AgentExecutor,
    make_check_budget,
)
from auton.llm import LLMClient
from auton.models import AgentNode, AgentSpec, AgentState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_spec(**overrides):
    """Create an AgentSpec with sensible defaults."""
    defaults = {
        "name": "Test Agent",
        "system_prompt": "You are a test agent.",
        "goal": "Do the task",
        "tools": [],
        "max_turns": 10,
    }
    defaults.update(overrides)
    return AgentSpec(**defaults)


def _make_node(agent_id="test-budget", **spec_overrides):
    """Create an AgentNode with the given spec overrides."""
    spec = _make_spec(**spec_overrides)
    node = AgentNode(id=agent_id, spec=spec)
    node.transition(AgentState.RUNNING)
    return node


def _noop_publish(path, event):
    """No-op publish_event."""
    pass


# ---------------------------------------------------------------------------
# check_budget tool tests
# ---------------------------------------------------------------------------


def test_check_budget_returns_budget_info():
    """check_budget returns tokens used, max, remaining, and percentage."""
    node = _make_node(max_tokens=100_000)
    node.health.tokens_total = 30_000

    fn = make_check_budget(node)
    result = json.loads(fn())

    assert result["tokens_used"] == 30_000
    assert result["max_tokens"] == 100_000
    assert result["tokens_remaining"] == 70_000
    assert result["budget_pct"] == 30.0
    assert "advice" in result
    assert "healthy" in result["advice"].lower()


def test_check_budget_no_budget():
    """check_budget with no max_tokens returns None for budget fields."""
    node = _make_node(max_tokens=None)
    node.health.tokens_total = 5_000

    fn = make_check_budget(node)
    result = json.loads(fn())

    assert result["tokens_used"] == 5_000
    assert result["max_tokens"] is None
    assert result["tokens_remaining"] is None
    assert result["budget_pct"] is None


def test_check_budget_warning_at_70pct():
    """check_budget gives warning advice at 70%+ usage."""
    node = _make_node(max_tokens=100_000)
    node.health.tokens_total = 75_000

    fn = make_check_budget(node)
    result = json.loads(fn())

    assert result["budget_pct"] == 75.0
    assert "midpoint" in result["advice"].lower() or "prioritize" in result["advice"].lower()


def test_check_budget_urgent_at_85pct():
    """check_budget gives urgent advice at 85%+ usage."""
    node = _make_node(max_tokens=100_000)
    node.health.tokens_total = 90_000

    fn = make_check_budget(node)
    result = json.loads(fn())

    assert result["budget_pct"] == 90.0
    assert "warning" in result["advice"].lower() or "stop" in result["advice"].lower()


def test_check_budget_critical_at_95pct():
    """check_budget gives critical advice at 95%+ usage."""
    node = _make_node(max_tokens=100_000)
    node.health.tokens_total = 97_000

    fn = make_check_budget(node)
    result = json.loads(fn())

    assert result["budget_pct"] == 97.0
    assert "critical" in result["advice"].lower()


# ---------------------------------------------------------------------------
# Continuation message tests
# ---------------------------------------------------------------------------


def test_continuation_message_normal():
    """Below 70%, continuation message is standard."""
    node = _make_node(max_tokens=100_000)
    node.health.tokens_total = 20_000

    executor = AgentExecutor(node, _noop_publish)
    msg = executor._build_continuation_message()

    assert "Continue working" in msg
    assert "URGENT" not in msg
    assert "FINAL" not in msg


def test_continuation_message_warn():
    """At 70-85%, continuation message includes budget notice."""
    node = _make_node(max_tokens=100_000)
    node.health.tokens_total = 75_000

    executor = AgentExecutor(node, _noop_publish)
    msg = executor._build_continuation_message()

    assert "75%" in msg
    assert "Budget notice" in msg or "wrapping up" in msg.lower()


def test_continuation_message_urgent():
    """At 85-95%, continuation message is urgent."""
    node = _make_node(max_tokens=100_000)
    node.health.tokens_total = 90_000

    executor = AgentExecutor(node, _noop_publish)
    msg = executor._build_continuation_message()

    assert "90%" in msg
    assert "URGENT" in msg


def test_continuation_message_final():
    """At 95%+, continuation message demands final deliverables."""
    node = _make_node(max_tokens=100_000)
    node.health.tokens_total = 97_000

    executor = AgentExecutor(node, _noop_publish)
    msg = executor._build_continuation_message()

    assert "97%" in msg
    assert "FINAL TURN" in msg
    assert "LAST CHANCE" in msg


def test_continuation_message_no_budget():
    """With no max_tokens, continuation message is standard."""
    node = _make_node(max_tokens=None)
    node.health.tokens_total = 999_999

    executor = AgentExecutor(node, _noop_publish)
    msg = executor._build_continuation_message()

    assert "Continue working" in msg
    assert "URGENT" not in msg


# ---------------------------------------------------------------------------
# Initial message tests
# ---------------------------------------------------------------------------


def test_initial_message_includes_budget_context():
    """Initial message includes budget context when max_tokens is set."""
    node = _make_node(max_tokens=200_000, goal="Research topic X")

    executor = AgentExecutor(node, _noop_publish)
    msg = executor._build_initial_message()

    assert "Research topic X" in msg
    assert "200,000 tokens" in msg
    assert "check_budget" in msg
    assert "incrementally" in msg


def test_initial_message_no_budget_context():
    """Initial message has no budget context when max_tokens is None."""
    node = _make_node(max_tokens=None, goal="Research topic X")

    executor = AgentExecutor(node, _noop_publish)
    msg = executor._build_initial_message()

    assert msg == "Research topic X"
    assert "budget" not in msg.lower()


# ---------------------------------------------------------------------------
# Planner integration tests
# ---------------------------------------------------------------------------


def _make_llm_response(content="", tool_calls=None, prompt_tokens=100, completion_tokens=50):
    """Build a mock LLM API response."""
    msg = {"content": content, "tool_calls": tool_calls}
    total = prompt_tokens + completion_tokens
    resp = MagicMock()
    resp.model_dump.return_value = {
        "choices": [{"message": msg}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total,
        },
    }
    return resp


@pytest.mark.asyncio
async def test_planner_finalize_triggers_in_tool_loop():
    """BudgetPlanner triggers finalize inside the LLM client tool loop."""
    planner = BudgetPlanner(max_budget=100_000)
    call_count = 0

    async def mock_acompletion(**kwargs):
        nonlocal call_count
        has_tools = "tools" in kwargs and kwargs["tools"]
        resp = MagicMock()
        if has_tools and call_count < 3:
            # Return tool calls for first few API calls (30K tokens each)
            resp.model_dump.return_value = {
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": f"call-{call_count}",
                            "function": {"name": "test_tool", "arguments": "{}"},
                            "type": "function",
                        }],
                    }
                }],
                "usage": {"prompt_tokens": 15_000, "completion_tokens": 15_000, "total_tokens": 30_000},
            }
        else:
            # Final response (no tool calls)
            resp.model_dump.return_value = {
                "choices": [{"message": {"content": "Final summary of research.", "tool_calls": None}}],
                "usage": {"prompt_tokens": 15_000, "completion_tokens": 15_000, "total_tokens": 30_000},
            }
        call_count += 1
        return resp

    with patch("auton.llm.acompletion", side_effect=mock_acompletion):
        client = LLMClient(
            model="test",
            system_prompt="You are a test agent.",
            budget_planner=planner,
        )
        client.add_tool(
            lambda: "result",
            {"name": "test_tool", "description": "Test tool", "parameters": {"type": "object", "properties": {}}},
        )
        result = await client.chat("Do research")

    # Planner should have triggered finalize
    assert planner.finalize_triggered is True
    # Should have stopped before using unlimited tokens
    assert client.usage.total_tokens <= 120_000
    assert client._stop_requested is True
    # Should have returned a final response
    assert result != ""


@pytest.mark.asyncio
async def test_planner_no_finalize_with_healthy_budget():
    """BudgetPlanner doesn't trigger when budget is healthy."""
    planner = BudgetPlanner(max_budget=1_000_000)  # Very large budget
    call_count = 0

    async def mock_acompletion(**kwargs):
        nonlocal call_count
        resp = MagicMock()
        if call_count < 2:
            resp.model_dump.return_value = {
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": f"call-{call_count}",
                            "function": {"name": "test_tool", "arguments": "{}"},
                            "type": "function",
                        }],
                    }
                }],
                "usage": {"prompt_tokens": 5_000, "completion_tokens": 5_000, "total_tokens": 10_000},
            }
        else:
            resp.model_dump.return_value = {
                "choices": [{"message": {"content": "Done with research.", "tool_calls": None}}],
                "usage": {"prompt_tokens": 5_000, "completion_tokens": 5_000, "total_tokens": 10_000},
            }
        call_count += 1
        return resp

    with patch("auton.llm.acompletion", side_effect=mock_acompletion):
        client = LLMClient(
            model="test",
            system_prompt="You are a test agent.",
            budget_planner=planner,
        )
        client.add_tool(
            lambda: "result",
            {"name": "test_tool", "description": "Test tool", "parameters": {"type": "object", "properties": {}}},
        )
        result = await client.chat("Do research")

    # Planner should NOT have triggered finalize
    assert planner.finalize_triggered is False
    assert client.usage.total_tokens == 30_000  # 3 API calls × 10K
    assert client._stop_requested is False
    assert result == "Done with research."


@pytest.mark.asyncio
async def test_executor_stops_at_budget_limit():
    """Executor stops when budget reaches 95% (between turns)."""
    call_count = 0

    async def mock_acompletion(**kwargs):
        nonlocal call_count
        # Each chat() call: 1 API call returning no tool calls.
        # Turn 0: 60K tokens → 60% — continue
        # Turn 1: +40K tokens → 100% — planner triggers finalize inside chat(),
        #   finalize call adds another 40K → 140K total. _stop_requested = True.
        # But since planner triggers inside chat(), the executor sees
        # _stop_requested and breaks.
        resp = MagicMock()
        has_tools = "tools" in kwargs and kwargs["tools"]
        if has_tools and call_count == 0:
            # First call: return tool calls so planner records data
            resp.model_dump.return_value = {
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": f"call-{call_count}",
                            "function": {"name": "check_budget", "arguments": "{}"},
                            "type": "function",
                        }],
                    }
                }],
                "usage": {"prompt_tokens": 30_000, "completion_tokens": 30_000, "total_tokens": 60_000},
            }
        else:
            resp.model_dump.return_value = {
                "choices": [{"message": {"content": f"Turn {call_count}", "tool_calls": None}}],
                "usage": {"prompt_tokens": 20_000, "completion_tokens": 20_000, "total_tokens": 40_000},
            }
        call_count += 1
        return resp

    node = _make_node(max_tokens=100_000, max_turns=10)

    events = []

    with patch("auton.llm.acompletion", side_effect=mock_acompletion):
        executor = AgentExecutor(node, lambda p, e: events.append(e))
        await executor.run()

    # Agent should have stopped — either via planner finalize or executor budget check
    assert node.health.tokens_total > 0
    complete_events = [e for e in events if e.get("type") == "execution_complete"]
    assert len(complete_events) == 1


@pytest.mark.asyncio
async def test_budget_tool_auto_registered():
    """check_budget is auto-registered when agent has max_tokens."""
    mock_resp = _make_llm_response(content="Done.")
    registered_tools = []

    async def mock_acompletion(**kwargs):
        # Check if tools include check_budget
        tools = kwargs.get("tools", [])
        for t in tools:
            name = t.get("function", {}).get("name", "")
            if name not in registered_tools:
                registered_tools.append(name)
        return mock_resp

    node = _make_node(max_tokens=100_000, max_turns=1)

    with patch("auton.llm.acompletion", side_effect=mock_acompletion):
        executor = AgentExecutor(node, _noop_publish)
        await executor.run()

    assert "check_budget" in registered_tools


@pytest.mark.asyncio
async def test_budget_tool_not_registered_without_budget():
    """check_budget is NOT registered when agent has no max_tokens and doesn't request it."""
    mock_resp = _make_llm_response(content="Done.")
    registered_tools = []

    async def mock_acompletion(**kwargs):
        tools = kwargs.get("tools", [])
        for t in tools:
            name = t.get("function", {}).get("name", "")
            if name not in registered_tools:
                registered_tools.append(name)
        return mock_resp

    node = _make_node(max_tokens=None, max_turns=1)

    with patch("auton.llm.acompletion", side_effect=mock_acompletion):
        executor = AgentExecutor(node, _noop_publish)
        await executor.run()

    assert "check_budget" not in registered_tools


# ---------------------------------------------------------------------------
# Budget percentage helper
# ---------------------------------------------------------------------------


def test_get_budget_pct():
    """_get_budget_pct returns correct percentage."""
    node = _make_node(max_tokens=100_000)
    node.health.tokens_total = 45_000

    executor = AgentExecutor(node, _noop_publish)
    pct = executor._get_budget_pct()
    assert pct == 45.0


def test_get_budget_pct_none():
    """_get_budget_pct returns None when no budget set."""
    node = _make_node(max_tokens=None)
    node.health.tokens_total = 45_000

    executor = AgentExecutor(node, _noop_publish)
    pct = executor._get_budget_pct()
    assert pct is None
