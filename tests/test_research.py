"""Tests for the agent execution, rollout streaming, and configurable agents."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from auton.api import app, registry, _publish_event


# ---------------------------------------------------------------------------
# Helpers
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


def _make_tool_call(name, arguments, call_id="call-001"):
    """Build a tool_call object."""
    return {
        "id": call_id,
        "function": {
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_custom_agent_with_system_prompt():
    """POST /agents with a custom system_prompt runs the agent correctly."""
    # Mock: single text response, no tool calls
    mock_resp = _make_llm_response(content="Hello! Task complete.")

    with patch("auton.llm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        registry.publish_event = _publish_event
        registry.db = None

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/agents", json={
                "id": "test-custom",
                "spec": {
                    "name": "Test Custom Agent",
                    "goal": "Say hello",
                    "system_prompt": "You are a friendly greeting bot. Respond with a greeting.",
                    "tools": [],
                },
            })
            assert resp.status_code == 201
            data = resp.json()
            assert data["id"] == "test-custom"
            assert data["state"] == "running"

            # Wait for execution to complete (it's a single LLM call, should be fast)
            await asyncio.sleep(0.5)

            # Check state transitioned to idle
            resp2 = await client.get("/agents/test-custom")
            assert resp2.status_code == 200
            state = resp2.json()["state"]
            assert state == "idle", f"Expected idle, got {state}"

            # Check conversation is populated
            resp3 = await client.get("/agents/test-custom/messages")
            assert resp3.status_code == 200
            messages = resp3.json()
            assert messages["message_count"] > 0
            assert len(messages["messages"]) > 0


@pytest.mark.asyncio
async def test_spawn_custom_agent_only_gets_requested_tools():
    """Custom agents only get the tools listed in their spec."""
    mock_resp = _make_llm_response(content="Done.")
    registered_tools = []

    with patch("auton.llm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        original_init = __import__("auton.llm", fromlist=["LLMClient"]).LLMClient.add_tool
        def tracking_add_tool(self, func, schema):
            registered_tools.append(schema["name"])
            return original_init(self, func, schema)

        with patch("auton.llm.LLMClient.add_tool", tracking_add_tool):
            registry.publish_event = _publish_event
            registry.db = None

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/agents", json={
                    "id": "test-minimal",
                    "spec": {
                        "name": "Minimal Agent",
                        "goal": "Do something",
                        "system_prompt": "You are a helper.",
                        "tools": [],
                    },
                })
                assert resp.status_code == 201
                await asyncio.sleep(0.5)

    # No tools should be registered (empty tools list, no budget)
    assert "write_file" not in registered_tools
    assert "finish" not in registered_tools


@pytest.mark.asyncio
async def test_agent_with_file_tools_gets_workspace_and_budget():
    """Agents with file tools in their spec get workspace tools and auto budget tool."""
    mock_resp = _make_llm_response(content="Research complete.")
    registered_tools = []

    with patch("auton.llm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        original_init = __import__("auton.llm", fromlist=["LLMClient"]).LLMClient.add_tool
        def tracking_add_tool(self, func, schema):
            registered_tools.append(schema["name"])
            return original_init(self, func, schema)

        with patch("auton.llm.LLMClient.add_tool", tracking_add_tool):
            registry.publish_event = _publish_event
            registry.db = None

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/agents", json={
                    "id": "test-file-tools",
                    "spec": {
                        "name": "File Tools Agent",
                        "goal": "Do research",
                        "system_prompt": "You are a researcher.",
                        "tools": ["web_search", "write_file", "read_file", "finish"],
                        "max_tokens": 100000,
                    },
                })
                assert resp.status_code == 201
                await asyncio.sleep(0.5)

    # Should have file tools, finish, and auto-registered budget tool
    assert "write_file" in registered_tools
    assert "read_file" in registered_tools
    assert "finish" in registered_tools
    assert "check_budget" in registered_tools


@pytest.mark.asyncio
async def test_tool_call_events_published():
    """LLM tool calls should be published as granular events."""
    # First response: tool call. Second response: final text.
    tool_call_resp = _make_llm_response(
        content="",
        tool_calls=[_make_tool_call("web_search", {"query": "test person"})],
    )
    final_resp = _make_llm_response(content="Found info about test person.")

    call_count = 0

    async def mock_acompletion(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return tool_call_resp
        return final_resp

    collected_events = []
    original_publish = _publish_event

    def capturing_publish(path, event):
        collected_events.append(event)
        original_publish(path, event)

    with patch("auton.llm.acompletion", side_effect=mock_acompletion):
        # Mock web_search tool
        with patch.dict(
            "auton.tools.TOOL_REGISTRY",
            {"web_search": (lambda query: "Search results for: " + query, {
                "name": "web_search",
                "description": "Search the web",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            })},
        ):
            registry.publish_event = capturing_publish
            registry.db = None

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/agents", json={
                    "id": "test-events",
                    "spec": {
                        "name": "Test Events Agent",
                        "goal": "Find info",
                        "system_prompt": "You are a researcher.",
                        "tools": ["web_search"],
                    },
                })
                assert resp.status_code == 201
                await asyncio.sleep(1.0)

    # Check that granular events were published
    event_types = [e.get("type") for e in collected_events]
    assert "tool_call" in event_types, f"Expected tool_call event, got: {event_types}"
    assert "tool_result" in event_types, f"Expected tool_result event, got: {event_types}"
    assert "llm_response" in event_types, f"Expected llm_response event, got: {event_types}"

    # Verify tool_call event details
    tool_call_events = [e for e in collected_events if e.get("type") == "tool_call"]
    assert len(tool_call_events) >= 1
    assert tool_call_events[0]["tool_name"] == "web_search"
    assert tool_call_events[0]["arguments"] == {"query": "test person"}


@pytest.mark.asyncio
async def test_messages_endpoint():
    """GET /agents/{id}/messages returns conversation history."""
    mock_resp = _make_llm_response(content="Here is my answer.")

    with patch("auton.llm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        registry.publish_event = _publish_event
        registry.db = None

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Spawn
            resp = await client.post("/agents", json={
                "id": "test-messages",
                "spec": {
                    "name": "Test Messages Agent",
                    "goal": "Answer a question",
                    "system_prompt": "You answer questions.",
                    "tools": [],
                },
            })
            assert resp.status_code == 201

            await asyncio.sleep(0.5)

            # Get messages
            resp2 = await client.get("/agents/test-messages/messages")
            assert resp2.status_code == 200
            data = resp2.json()
            assert data["path"] == "test-messages"
            assert data["message_count"] > 0

            # Should have at least a user message and an assistant message
            roles = [m["role"] for m in data["messages"]]
            assert "user" in roles
            assert "assistant" in roles


@pytest.mark.asyncio
async def test_messages_endpoint_404():
    """GET /agents/{id}/messages returns 404 for nonexistent agent."""
    registry.publish_event = _publish_event
    registry.db = None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/agents/nonexistent/messages")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_spec_contains_name_and_system_prompt():
    """AgentSpec must include name and system_prompt (required fields)."""
    mock_resp = _make_llm_response(content="Done.")

    with patch("auton.llm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        registry.publish_event = _publish_event
        registry.db = None

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Missing name — should fail validation
            resp = await client.post("/agents", json={
                "id": "test-missing-name",
                "spec": {
                    "goal": "Do stuff",
                    "system_prompt": "You are a helper.",
                    "tools": [],
                },
            })
            assert resp.status_code == 422

            # Missing system_prompt — should fail validation
            resp2 = await client.post("/agents", json={
                "id": "test-missing-prompt",
                "spec": {
                    "name": "Test Agent",
                    "goal": "Do stuff",
                    "tools": [],
                },
            })
            assert resp2.status_code == 422


@pytest.mark.asyncio
async def test_agent_to_dict_includes_spec():
    """Agent to_dict() includes the full spec (no separate policy)."""
    mock_resp = _make_llm_response(content="Done.")

    with patch("auton.llm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        registry.publish_event = _publish_event
        registry.db = None

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/agents", json={
                "id": "test-spec-dict",
                "spec": {
                    "name": "Spec Dict Test",
                    "goal": "Test spec serialization",
                    "system_prompt": "You are a test agent.",
                    "tools": [],
                    "max_tokens": 50000,
                    "max_restarts": 5,
                    "drift_threshold": 0.3,
                },
            })
            assert resp.status_code == 201
            data = resp.json()

            # Spec should be in the response
            assert "spec" in data
            assert data["spec"]["name"] == "Spec Dict Test"
            assert data["spec"]["system_prompt"] == "You are a test agent."
            assert data["spec"]["max_tokens"] == 50000
            assert data["spec"]["max_restarts"] == 5
            assert data["spec"]["drift_threshold"] == 0.3

            # There should be no separate "policy" key
            assert "policy" not in data
