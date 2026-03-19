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
    AgentSpec,
    AgentState,
    ArtifactRecord,
    CorrectionRequest,
    InvalidTransition,
    MessageRequest,
    SpawnRequest,
    SuspendRequest,
)
from .auth import auth_middleware, log_auth_status
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

    # Log authentication status
    log_auth_status()

    # Register global tools
    register_tool(web_search)
    register_tool(fetch_webpage)
    logger.info("Tools registered: web_search, fetch_webpage")

    # Initialize database (PostgreSQL)
    db = Database()
    await db.init()

    # Wire registry
    registry.publish_event = _publish_event
    registry.db = db

    # Load persisted agents
    agents = await db.load_agents()
    for agent_data in agents:
        node = registry.restore(agent_data)
        # Restore artifacts from DB
        artifacts = await db.list_artifacts(agent_id=node.id)
        for a in artifacts:
            node.artifacts.append(ArtifactRecord(**a))
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
    for artifact in node.artifacts:
        await db.save_artifact(artifact.model_dump())
    for child in node.children.values():
        await _save_tree(child, db)


app = FastAPI(
    title="Auton",
    description="Agent runtime for long-running autonomous agents with built-in oversight",
    version="0.1.0",
    lifespan=lifespan,
)
app.middleware("http")(auth_middleware)

registry = AgentRegistry()
oversight = OversightEngine(registry)

# Load env vars early so dashboard imports (Keycloak client etc.) see them.
_load_all_env()

# Initialize dashboard UI — must happen BEFORE lifespan starts
# so that ui.run_with() can wrap the app's lifespan properly.
try:
    from .dashboard.main import init_dashboard
    init_dashboard(app)
    logger.info("Dashboard UI available at /")
except ImportError as e:
    logger.info("Dashboard not available (missing deps): %s", e)
except Exception as e:
    logger.warning("Dashboard init failed: %s", e)


@app.get("/api/health")
async def health():
    """Service health check. No authentication required."""
    agent_count = sum(1 for _ in registry._roots.values())
    return {"status": "ok", "agents": agent_count}

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


@app.get("/api/agents")
async def list_agents():
    """Full agent tree."""
    return {"agents": registry.list_all()}


@app.get("/api/agents/{path:path}")
async def get_agent(path: str):
    """Subtree at path."""
    # Strip trailing slashes, handle special sub-paths
    path = path.strip("/")

    # Route sub-resource paths
    if path.endswith("/observe"):
        return await _observe_redirect(path)
    if path.endswith("/log"):
        return await _log_redirect(path)
    if path.endswith("/rollout"):
        return await _rollout_redirect(path)
    if path.endswith("/messages"):
        agent_path = path.rsplit("/messages", 1)[0]
        node = registry.resolve(agent_path)
        if node is None:
            raise _error(404, f"Agent not found: {agent_path}")
        return {
            "path": agent_path,
            "state": node.state.value,
            "message_count": len(node.conversation),
            "messages": node.conversation,
        }
    if path.endswith("/workspace"):
        agent_path = path.rsplit("/workspace", 1)[0]
        node = registry.resolve(agent_path)
        if node is None:
            raise _error(404, f"Agent not found: {agent_path}")
        from .workspace import list_workspace_files
        files = list_workspace_files(node.id)
        return {"agent": agent_path, "files": files, "count": len(files)}
    if path.endswith("/artifacts"):
        agent_path = path.rsplit("/artifacts", 1)[0]
        node = registry.resolve(agent_path)
        if node is None:
            raise _error(404, f"Agent not found: {agent_path}")
        artifacts = [a.model_dump() for a in node.artifacts] if node.artifacts else []
        return {"agent": agent_path, "artifacts": artifacts, "count": len(artifacts)}

    result = registry.subtree(path)
    if result is None:
        raise _error(404, f"Agent not found: {path}")
    return result


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@app.post("/api/agents")
async def spawn_root_agent(req: SpawnRequest):
    """Spawn a root agent. Provide spec directly or reference a template."""
    resolved = await _resolve_spec(req)
    req.spec = resolved
    try:
        node = registry.spawn(req)
    except AgentExists as e:
        raise _error(409, str(e))
    except PolicyViolation as e:
        raise _error(422, str(e))
    _publish_event(node.path, {"type": "spawned", "path": node.path})
    return _json_response(node.to_dict(), 201)


async def _resolve_spec(req: SpawnRequest) -> AgentSpec:
    """Resolve a SpawnRequest to a concrete AgentSpec (from template or inline)."""
    if req.spec:
        return req.spec
    if req.template:
        if not registry.db:
            raise _error(500, "Database not initialized")
        tmpl = await registry.db.load_template(req.template)
        if not tmpl:
            raise _error(404, f"Template not found: {req.template}")
        spec_data = tmpl["spec"]
        if req.overrides:
            spec_data = {**spec_data, **req.overrides}
        return AgentSpec(**spec_data)
    raise _error(422, "Either 'spec' or 'template' must be provided")


@app.post("/api/agents/{parent_path:path}")
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


@app.delete("/api/agents/{path:path}")
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


@app.patch("/api/agents/{path:path}")
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


@app.get("/api/agents/{path:path}/observe")
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


@app.get("/api/agents/{path:path}/log")
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


@app.post("/api/oversight/check")
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


async def _rollout_redirect(path: str):
    """Handle /agents/foo/rollout routed through the catch-all GET."""
    return await stream_rollout(path, None)


@app.get("/api/agents/{path:path}/rollout")
async def stream_rollout(path: str, request: Request):
    """SSE stream of the agent's LLM conversation — tool calls, results, responses."""
    path = path.strip("/")
    if path.endswith("/rollout"):
        path = path.rsplit("/rollout", 1)[0]

    node = registry.resolve(path)
    if node is None:
        raise _error(404, f"Agent not found: {path}")

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _observers.setdefault(path, []).append(queue)

    ROLLOUT_EVENTS = {"llm_response", "tool_call", "tool_result"}

    async def event_generator():
        try:
            # Catch-up: send recent conversation messages
            for msg in node.conversation[-50:]:
                yield {
                    "event": "message",
                    "data": json.dumps(msg, default=str),
                }
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    if event.get("type") in ROLLOUT_EVENTS:
                        yield {
                            "event": event["type"],
                            "data": json.dumps(event, default=str),
                        }
                except asyncio.TimeoutError:
                    yield {"event": "keepalive", "data": "{}"}
        except asyncio.CancelledError:
            pass
        finally:
            if queue in _observers.get(path, []):
                _observers[path].remove(queue)

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


@app.get("/api/artifacts")
async def list_artifacts(
    agent_id: str | None = None,
    status: str | None = None,
    mime_type: str | None = None,
    tag: str | None = None,
):
    """List all artifacts, optionally filtered."""
    if not registry.db:
        raise _error(500, "Database not initialized")
    artifacts = await registry.db.list_artifacts(
        agent_id=agent_id, status=status, mime_type=mime_type, tag=tag,
    )
    return {"artifacts": artifacts, "count": len(artifacts)}


@app.get("/api/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str):
    """Get artifact metadata by ID."""
    if not registry.db:
        raise _error(500, "Database not initialized")
    artifact = await registry.db.get_artifact(artifact_id)
    if not artifact:
        raise _error(404, f"Artifact not found: {artifact_id}")
    return artifact


@app.get("/api/artifacts/{artifact_id}/content")
async def get_artifact_content(artifact_id: str):
    """Serve the actual file content of an artifact."""
    if not registry.db:
        raise _error(500, "Database not initialized")
    artifact = await registry.db.get_artifact(artifact_id)
    if not artifact:
        raise _error(404, f"Artifact not found: {artifact_id}")

    from pathlib import Path
    from fastapi.responses import FileResponse
    from .workspace import get_workspace_path

    # Resolve workspace — check if agent has a user_id for per-user workspace
    agent_node = registry.resolve(artifact.get("agent_path", ""))
    uid = agent_node.user_id if agent_node else None
    ws = get_workspace_path(artifact["agent_id"], uid)
    file_path = ws / artifact["file_path"]
    if not file_path.exists():
        raise _error(404, f"Artifact file not found on disk: {artifact['file_path']}")
    return FileResponse(
        str(file_path),
        media_type=artifact.get("mime_type", "text/plain"),
        filename=Path(artifact["file_path"]).name,
    )


# ---------------------------------------------------------------------------
# Spec Templates
# ---------------------------------------------------------------------------


class TemplateRequest(BaseModel):
    spec: dict  # AgentSpec fields as dict
    description: str = ""


@app.get("/api/specs")
async def list_templates():
    """List all available spec templates."""
    if not registry.db:
        raise _error(500, "Database not initialized")
    templates = await registry.db.list_templates()
    return {"templates": templates}


@app.get("/api/specs/{name}")
async def get_template(name: str):
    """Get a spec template by name."""
    if not registry.db:
        raise _error(500, "Database not initialized")
    tmpl = await registry.db.load_template(name)
    if not tmpl:
        raise _error(404, f"Template not found: {name}")
    return tmpl


@app.post("/api/specs/{name}")
async def save_template(name: str, req: TemplateRequest):
    """Create or update a spec template."""
    if not registry.db:
        raise _error(500, "Database not initialized")
    # Validate that the spec is valid
    try:
        AgentSpec(**req.spec)
    except Exception as e:
        raise _error(422, f"Invalid spec: {e}")
    await registry.db.save_template(name, req.spec, req.description)
    return {"status": "saved", "name": name}


@app.delete("/api/specs/{name}")
async def delete_template(name: str):
    """Delete a spec template."""
    if not registry.db:
        raise _error(500, "Database not initialized")
    deleted = await registry.db.delete_template(name)
    if not deleted:
        raise _error(404, f"Template not found: {name}")
    return {"status": "deleted", "name": name}

