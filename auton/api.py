"""HTTP + SSE API: filesystem-like agent tree navigation and lifecycle management."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from .models import (
    AgentState,
    CorrectionRequest,
    InvalidTransition,
    MessageRequest,
    SpawnRequest,
    SuspendRequest,
)
from .oversight import OversightEngine
from .registry import AgentExists, AgentNotFound, AgentRegistry, PolicyViolation

app = FastAPI(
    title="Auton",
    description="Agent runtime for long-running autonomous agents with built-in oversight",
    version="0.1.0",
)

registry = AgentRegistry()
oversight = OversightEngine(registry)

# SSE subscribers: path → list of asyncio.Queue
_observers: dict[str, list[asyncio.Queue]] = {}


def _error(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


def _json_response(content: dict[str, Any], status_code: int = 200) -> JSONResponse:
    """JSONResponse that handles datetime serialization."""
    body = json.dumps(content, default=str)
    return JSONResponse(content=json.loads(body), status_code=status_code)


def _publish_event(path: str, event: dict[str, Any]) -> None:
    """Push event to all SSE observers for this path and its ancestors."""
    parts = path.split("/")
    for i in range(len(parts)):
        prefix = "/".join(parts[: i + 1])
        for queue in _observers.get(prefix, []):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # Drop if observer is slow


# ---------------------------------------------------------------------------
# Tree navigation
# ---------------------------------------------------------------------------


@app.get("/agents")
async def list_agents():
    """Full agent tree."""
    return {"agents": registry.list_all()}


@app.get("/agents/{path:path}")
async def get_agent(path: str):
    """Subtree at path."""
    # Strip trailing slashes, handle special sub-paths
    path = path.strip("/")

    # Route sub-resource paths
    if path.endswith("/observe"):
        return await _observe_redirect(path)
    if path.endswith("/log"):
        return await _log_redirect(path)

    result = registry.subtree(path)
    if result is None:
        raise _error(404, f"Agent not found: {path}")
    return result


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@app.post("/agents")
async def spawn_root_agent(req: SpawnRequest):
    """Spawn a root agent."""
    try:
        node = registry.spawn(req)
    except AgentExists as e:
        raise _error(409, str(e))
    except PolicyViolation as e:
        raise _error(422, str(e))
    _publish_event(node.path, {"type": "spawned", "path": node.path})
    return _json_response(node.to_dict(), 201)


@app.post("/agents/{parent_path:path}")
async def spawn_or_action(parent_path: str, request: Request):
    """Spawn under parent, or dispatch to sub-actions (checkpoint, fork, etc.)."""
    parent_path = parent_path.strip("/")

    # Route action endpoints
    if parent_path.endswith("/checkpoint"):
        return await checkpoint_agent(parent_path.rsplit("/checkpoint", 1)[0])
    if parent_path.endswith("/fork"):
        return await fork_agent(parent_path.rsplit("/fork", 1)[0])
    if parent_path.endswith("/restart"):
        return await restart_agent(parent_path.rsplit("/restart", 1)[0])
    if parent_path.endswith("/suspend"):
        body = await request.json() if await request.body() else {}
        return await suspend_agent(parent_path.rsplit("/suspend", 1)[0], body)
    if parent_path.endswith("/resume"):
        return await resume_agent(parent_path.rsplit("/resume", 1)[0])
    if parent_path.endswith("/message"):
        body = await request.json()
        return await message_agent(parent_path.rsplit("/message", 1)[0], body)

    # Default: spawn under parent
    body = await request.json()
    req = SpawnRequest(**body)
    try:
        node = registry.spawn(req, parent_path=parent_path)
    except AgentNotFound as e:
        raise _error(404, str(e))
    except AgentExists as e:
        raise _error(409, str(e))
    except PolicyViolation as e:
        raise _error(422, str(e))
    _publish_event(node.path, {"type": "spawned", "path": node.path})
    return _json_response(node.to_dict(), 201)


# ---------------------------------------------------------------------------
# Termination
# ---------------------------------------------------------------------------


@app.delete("/agents/{path:path}")
async def terminate_agent(path: str):
    """Terminate an agent (cascades to children)."""
    path = path.strip("/")
    try:
        node = registry.terminate(path)
    except AgentNotFound as e:
        raise _error(404, str(e))
    except InvalidTransition as e:
        raise _error(409, str(e))
    _publish_event(path, {"type": "terminated", "path": path})
    return node.to_dict()


# ---------------------------------------------------------------------------
# Correction
# ---------------------------------------------------------------------------


@app.patch("/agents/{path:path}")
async def correct_agent(path: str, req: CorrectionRequest):
    """Inject guidance into a running agent."""
    path = path.strip("/")
    try:
        node = registry.correct(path, req.guidance)
    except AgentNotFound as e:
        raise _error(404, str(e))
    except InvalidTransition as e:
        raise _error(409, str(e))
    _publish_event(path, {"type": "correction", "path": path, "guidance": req.guidance[:200]})
    return node.to_dict()


# ---------------------------------------------------------------------------
# Sub-action handlers
# ---------------------------------------------------------------------------


async def checkpoint_agent(path: str):
    try:
        snap = registry.checkpoint(path)
    except AgentNotFound as e:
        raise _error(404, str(e))
    _publish_event(path, {"type": "checkpoint", "path": path})
    return snap


async def fork_agent(path: str):
    try:
        node = registry.fork(path)
    except AgentNotFound as e:
        raise _error(404, str(e))
    except PolicyViolation as e:
        raise _error(422, str(e))
    _publish_event(node.path, {"type": "forked", "path": node.path, "from": path})
    return _json_response(node.to_dict(), 201)


async def restart_agent(path: str):
    try:
        node = registry.restart(path)
    except AgentNotFound as e:
        raise _error(404, str(e))
    except (InvalidTransition, PolicyViolation) as e:
        raise _error(422, str(e))
    _publish_event(path, {"type": "restarted", "path": path})
    return node.to_dict()


async def suspend_agent(path: str, body: dict):
    req = SuspendRequest(**(body or {}))
    try:
        node = registry.suspend(path, req.reason)
    except AgentNotFound as e:
        raise _error(404, str(e))
    except InvalidTransition as e:
        raise _error(409, str(e))
    _publish_event(path, {"type": "suspended", "path": path, "reason": req.reason.value})
    return node.to_dict()


async def resume_agent(path: str):
    try:
        node = registry.resume(path)
    except AgentNotFound as e:
        raise _error(404, str(e))
    except InvalidTransition as e:
        raise _error(409, str(e))
    _publish_event(path, {"type": "resumed", "path": path})
    return node.to_dict()


async def message_agent(path: str, body: dict):
    req = MessageRequest(**body)
    try:
        node = registry.send_message(
            path, req.content, channel=req.channel, kind=req.kind, metadata=req.metadata
        )
    except AgentNotFound as e:
        raise _error(404, str(e))
    except InvalidTransition as e:
        raise _error(409, str(e))
    _publish_event(path, {"type": "message", "path": path, "channel": req.channel})
    return {"status": "delivered", "path": path}


# ---------------------------------------------------------------------------
# SSE Observation
# ---------------------------------------------------------------------------


@app.get("/agents/{path:path}/observe")
async def observe_agent(path: str, request: Request):
    """SSE stream of health events for an agent."""
    path = path.strip("/")
    if path.endswith("/observe"):
        path = path.rsplit("/observe", 1)[0]

    node = registry.resolve(path)
    if node is None:
        raise _error(404, f"Agent not found: {path}")

    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _observers.setdefault(path, []).append(queue)

    async def event_generator():
        try:
            # Send initial health snapshot
            yield {
                "event": "health",
                "data": json.dumps(node.health.model_dump(), default=str),
            }
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {
                        "event": event.get("type", "update"),
                        "data": json.dumps(event, default=str),
                    }
                except asyncio.TimeoutError:
                    # Send keepalive with current health
                    events = oversight.check_agent(path)
                    health_data = node.health.model_dump() if node else {}
                    payload = {
                        "health": health_data,
                        "events": [e.to_dict() for e in events],
                    }
                    yield {
                        "event": "heartbeat",
                        "data": json.dumps(payload, default=str),
                    }
        except asyncio.CancelledError:
            pass
        finally:
            _observers.get(path, []).remove(queue) if queue in _observers.get(path, []) else None

    return EventSourceResponse(event_generator())


@app.get("/agents/{path:path}/log")
async def stream_log(path: str, request: Request):
    """SSE stream of the agent's event log."""
    path = path.strip("/")
    if path.endswith("/log"):
        path = path.rsplit("/log", 1)[0]

    node = registry.resolve(path)
    if node is None:
        raise _error(404, f"Agent not found: {path}")

    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _observers.setdefault(path, []).append(queue)

    async def event_generator():
        try:
            # Send existing log entries
            for entry in node.log[-50:]:
                yield {
                    "event": "log",
                    "data": json.dumps(entry, default=str),
                }
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=60.0)
                    yield {
                        "event": "log",
                        "data": json.dumps(event, default=str),
                    }
                except asyncio.TimeoutError:
                    yield {"event": "keepalive", "data": "{}"}
        except asyncio.CancelledError:
            pass
        finally:
            _observers.get(path, []).remove(queue) if queue in _observers.get(path, []) else None

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------------
# Oversight
# ---------------------------------------------------------------------------


@app.post("/oversight/check")
async def run_oversight():
    """Manually trigger oversight checks on all agents."""
    events = oversight.check_all()
    return {"events": [e.to_dict() for e in events]}


async def _observe_redirect(path: str):
    """Handle /agents/foo/observe routed through the catch-all GET."""
    return await observe_agent(path, None)


async def _log_redirect(path: str):
    """Handle /agents/foo/log routed through the catch-all GET."""
    return await stream_log(path, None)
