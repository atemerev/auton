"""HTTP + SSE API: filesystem-like agent tree navigation and lifecycle management."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
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

logger = logging.getLogger(__name__)


def _load_env_file(path):
    """Load env vars from a file. Only sets vars not already in environment."""
    import os
    from pathlib import Path

    env_path = Path(path)
    if not env_path.exists():
        return False

    logger.info(f"Loading env from {env_path}")
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value and not os.environ.get(key):
            os.environ[key] = value
            if "KEY" in key or "TOKEN" in key:
                logger.info(f"  {key} = {value[:8]}...")
            else:
                logger.info(f"  {key} = {value}")
    return True


def _load_all_env():
    """Load env vars from local .env, then fall back to Lethe config."""
    from pathlib import Path

    # Local .env first (project-specific keys)
    _load_env_file(Path.cwd() / ".env")

    # Then Lethe config (shared keys)
    lethe_env = Path.home() / ".config" / "lethe" / ".env"
    if not lethe_env.exists():
        lethe_env = Path.home() / ".lethe" / ".env"
    _load_env_file(lethe_env)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database, load agents, register tools, start scheduler."""
    from .db import Database
    from .scheduler import Scheduler
    from .tools import register_tool
    from .tools.web import fetch_webpage, web_search

    # Load API keys from .env files
    _load_all_env()

    # Register global tools
    register_tool(web_search)
    register_tool(fetch_webpage)
    logger.info("Tools registered: web_search, fetch_webpage")

    # Initialize database
    db = Database()
    await db.init()

    # Wire registry
    registry.publish_event = _publish_event
    registry.db = db

    # Load persisted agents
    agents = await db.load_agents()
    for agent_data in agents:
        node = registry.restore(agent_data)
        # Resume scheduled agents that were idle
        if node.state == AgentState.IDLE and node.spec.schedule:
            logger.info(f"Restored scheduled agent: {node.path}")

    logger.info(f"Loaded {len(agents)} agents from database")

    # Start scheduler
    scheduler = Scheduler(registry, _publish_event, db)
    scheduler.start()

    yield

    # Cleanup
    scheduler.stop()
    # Save all running agents
    for root in registry._roots.values():
        await _save_tree(root, db)
    await db.close()
    logger.info("Auton shutdown complete")


async def _save_tree(node, db):
    """Recursively save all agents in a tree."""
    await db.save_agent(node)
    for child in node.children.values():
        await _save_tree(child, db)


app = FastAPI(
    title="Auton",
    description="Agent runtime for long-running autonomous agents with built-in oversight",
    version="0.1.0",
    lifespan=lifespan,
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


# ---------------------------------------------------------------------------
# Person Research (Demo)
# ---------------------------------------------------------------------------


class ResearchRequest(BaseModel):
    name: str
    details: str = ""
    schedule: str | None = None
    model: str = "openrouter/anthropic/claude-sonnet-4-6"


@app.post("/research")
async def start_person_research(req: ResearchRequest):
    """Spawn a person research agent."""
    from .agents.person_research import create_person_research_spec

    spec = create_person_research_spec(
        person_name=req.name,
        known_details=req.details,
        schedule=req.schedule,
        model=req.model,
    )
    try:
        node = registry.spawn(spec)
    except AgentExists as e:
        raise _error(409, str(e))
    except PolicyViolation as e:
        raise _error(422, str(e))
    _publish_event(node.path, {"type": "spawned", "path": node.path})
    return _json_response(node.to_dict(), 201)


@app.get("/research/{agent_id}/dossier")
async def get_dossier(agent_id: str):
    """Get the research dossier for an agent."""
    from .tools.dossier import get_dossier as get_mem_dossier

    # Try in-memory first
    dossier = get_mem_dossier(agent_id)
    if dossier.get("last_updated"):
        return dossier

    # Try database
    if registry.db:
        db_dossier = await registry.db.load_dossier(agent_id)
        if db_dossier:
            return db_dossier

    raise _error(404, f"No dossier found for agent {agent_id}")
