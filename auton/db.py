"""PostgreSQL persistence for agents, checkpoints, spec templates, and artifacts."""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from auton.dashboard.db_client import async_engine, AsyncSessionLocal

logger = logging.getLogger(__name__)

# Agent state ↔ employee status mapping
_STATE_TO_STATUS = {
    "running": "active",
    "idle": "waiting_for_input",
    "suspended": "suspended",
    "dead": "archived",
    "terminating": "archived",
    "spawning": "active",
    "correcting": "active",
}


class Database:
    """Async PostgreSQL database for Auton persistence."""

    async def init(self) -> None:
        """Create tables if they don't exist."""
        async with async_engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    parent_path TEXT,
                    user_id UUID,
                    spec_json JSONB NOT NULL,
                    state TEXT NOT NULL,
                    health_json JSONB NOT NULL,
                    restart_count INTEGER DEFAULT 0,
                    idle_reason TEXT,
                    profile_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
                )
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    checkpoint_json JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL
                )
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS spec_templates (
                    name TEXT PRIMARY KEY,
                    spec_json JSONB NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
                )
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    agent_path TEXT NOT NULL,
                    mime_type TEXT NOT NULL DEFAULT 'text/markdown',
                    description TEXT NOT NULL DEFAULT '',
                    tags JSONB NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'expected',
                    file_size INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
                )
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    definition TEXT NOT NULL,
                    deliverables JSONB NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'queued',
                    session_id TEXT,
                    created_at TIMESTAMPTZ NOT NULL,
                    started_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ
                )
            """))
            # Migrate: add columns if missing (safe for existing DBs)
            for col, typedef in [
                ("user_id", "UUID"),
                ("profile_json", "JSONB"),
            ]:
                try:
                    await conn.execute(text(
                        f"ALTER TABLE agents ADD COLUMN IF NOT EXISTS {col} {typedef}"
                    ))
                except Exception:
                    pass
            # Drop legacy employees table if it exists
            await conn.execute(text("DROP TABLE IF EXISTS employees"))
        logger.info("Database initialized (PostgreSQL)")

    async def close(self) -> None:
        """Dispose the engine connection pool."""
        await async_engine.dispose()

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------

    async def save_agent(self, node) -> None:
        """Upsert agent state."""
        now = datetime.now(timezone.utc)
        profile_json = json.dumps(node.profile) if node.profile else None
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO agents
                        (id, path, parent_path, user_id, spec_json, state, health_json,
                         restart_count, idle_reason, profile_json, created_at, updated_at)
                    VALUES (:id, :path, :parent_path, :user_id, :spec_json, :state, :health_json,
                            :restart_count, :idle_reason, :profile_json, :created_at, :updated_at)
                    ON CONFLICT (id) DO UPDATE SET
                        path = EXCLUDED.path,
                        parent_path = EXCLUDED.parent_path,
                        user_id = COALESCE(EXCLUDED.user_id, agents.user_id),
                        spec_json = EXCLUDED.spec_json,
                        state = EXCLUDED.state,
                        health_json = EXCLUDED.health_json,
                        restart_count = EXCLUDED.restart_count,
                        idle_reason = EXCLUDED.idle_reason,
                        profile_json = COALESCE(EXCLUDED.profile_json, agents.profile_json),
                        updated_at = EXCLUDED.updated_at
                """),
                {
                    "id": node.id,
                    "path": node.path,
                    "parent_path": node.parent_path,
                    "user_id": node.user_id,
                    "spec_json": node.spec.model_dump_json(),
                    "state": node.state.value,
                    "health_json": node.health.model_dump_json(),
                    "restart_count": node.restart_count,
                    "idle_reason": node.idle_reason.value if node.idle_reason else None,
                    "profile_json": profile_json,
                    "created_at": node.created_at,
                    "updated_at": now,
                },
            )
            await session.commit()

    async def load_agents(self) -> list[dict]:
        """Load all non-dead agents from the database."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT id, path, parent_path, spec_json, state, health_json, "
                    "restart_count, created_at, idle_reason, user_id, profile_json "
                    "FROM agents WHERE state NOT IN ('dead', 'terminating')"
                )
            )
            rows = result.fetchall()
            agents = []
            for row in rows:
                spec_json = row[3]
                health_json = row[5]
                profile_json = row[10]
                agents.append({
                    "id": row[0],
                    "path": row[1],
                    "parent_path": row[2],
                    "spec": json.loads(spec_json) if isinstance(spec_json, str) else spec_json,
                    "state": row[4],
                    "health": json.loads(health_json) if isinstance(health_json, str) else health_json,
                    "restart_count": row[6],
                    "created_at": row[7].isoformat() if hasattr(row[7], 'isoformat') else row[7],
                    "idle_reason": row[8],
                    "user_id": str(row[9]) if row[9] else None,
                    "profile": (json.loads(profile_json) if isinstance(profile_json, str) else profile_json)
                              if profile_json else None,
                })
            return agents

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------

    async def save_checkpoint(self, agent_id: str, checkpoint: dict) -> None:
        """Save a checkpoint."""
        now = datetime.now(timezone.utc)
        cp_id = checkpoint.get("id", "")
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO checkpoints (id, agent_id, checkpoint_json, created_at)
                    VALUES (:id, :agent_id, :checkpoint_json, :created_at)
                    ON CONFLICT (id) DO UPDATE SET
                        checkpoint_json = EXCLUDED.checkpoint_json
                """),
                {
                    "id": cp_id,
                    "agent_id": agent_id,
                    "checkpoint_json": json.dumps(checkpoint),
                    "created_at": now,
                },
            )
            await session.commit()

    async def delete_agent(self, agent_id: str) -> None:
        """Delete an agent and its associated data."""
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("DELETE FROM checkpoints WHERE agent_id = :agent_id"),
                {"agent_id": agent_id},
            )
            await session.execute(
                text("DELETE FROM agents WHERE id = :id"),
                {"id": agent_id},
            )
            await session.commit()

    # ------------------------------------------------------------------
    # Spec Templates
    # ------------------------------------------------------------------

    async def save_template(self, name: str, spec_data: dict, description: str = "") -> None:
        """Save or update a spec template."""
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO spec_templates (name, spec_json, description, created_at, updated_at)
                    VALUES (:name, :spec_json, :description, :created_at, :updated_at)
                    ON CONFLICT (name) DO UPDATE SET
                        spec_json = EXCLUDED.spec_json,
                        description = EXCLUDED.description,
                        updated_at = EXCLUDED.updated_at
                """),
                {
                    "name": name,
                    "spec_json": json.dumps(spec_data),
                    "description": description,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            await session.commit()

    async def load_template(self, name: str) -> dict | None:
        """Load a spec template by name."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT spec_json, description, created_at, updated_at "
                    "FROM spec_templates WHERE name = :name"
                ),
                {"name": name},
            )
            row = result.fetchone()
            if row:
                spec_json = row[0]
                return {
                    "name": name,
                    "spec": json.loads(spec_json) if isinstance(spec_json, str) else spec_json,
                    "description": row[1],
                    "created_at": row[2].isoformat() if hasattr(row[2], 'isoformat') else row[2],
                    "updated_at": row[3].isoformat() if hasattr(row[3], 'isoformat') else row[3],
                }
            return None

    async def list_templates(self) -> list[dict]:
        """List all spec templates."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT name, description, created_at, updated_at "
                    "FROM spec_templates ORDER BY name"
                )
            )
            rows = result.fetchall()
            return [
                {
                    "name": row[0],
                    "description": row[1],
                    "created_at": row[2].isoformat() if hasattr(row[2], 'isoformat') else row[2],
                    "updated_at": row[3].isoformat() if hasattr(row[3], 'isoformat') else row[3],
                }
                for row in rows
            ]

    async def delete_template(self, name: str) -> bool:
        """Delete a spec template. Returns True if deleted."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("DELETE FROM spec_templates WHERE name = :name"),
                {"name": name},
            )
            await session.commit()
            return result.rowcount > 0

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------

    async def save_artifact(self, record: dict) -> None:
        """Upsert an artifact record."""
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO artifacts
                        (id, name, file_path, agent_id, agent_path, mime_type,
                         description, tags, status, file_size, created_at, updated_at)
                    VALUES (:id, :name, :file_path, :agent_id, :agent_path, :mime_type,
                            :description, :tags, :status, :file_size, :created_at, :updated_at)
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        file_path = EXCLUDED.file_path,
                        mime_type = EXCLUDED.mime_type,
                        description = EXCLUDED.description,
                        tags = EXCLUDED.tags,
                        status = EXCLUDED.status,
                        file_size = EXCLUDED.file_size,
                        updated_at = EXCLUDED.updated_at
                """),
                {
                    "id": record["id"],
                    "name": record["name"],
                    "file_path": record["file_path"],
                    "agent_id": record["agent_id"],
                    "agent_path": record["agent_path"],
                    "mime_type": record.get("mime_type", "text/markdown"),
                    "description": record.get("description", ""),
                    "tags": json.dumps(record.get("tags", [])),
                    "status": record.get("status", "expected"),
                    "file_size": record.get("file_size", 0),
                    "created_at": record.get("created_at", now),
                    "updated_at": now,
                },
            )
            await session.commit()

    async def get_artifact(self, artifact_id: str) -> dict | None:
        """Get a single artifact by ID."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT id, name, file_path, agent_id, agent_path, mime_type, "
                    "description, tags, status, file_size, created_at, updated_at "
                    "FROM artifacts WHERE id = :id"
                ),
                {"id": artifact_id},
            )
            row = result.fetchone()
            if not row:
                return None
            return self._row_to_artifact(row)

    async def list_artifacts(
        self,
        agent_id: str | None = None,
        status: str | None = None,
        mime_type: str | None = None,
        tag: str | None = None,
    ) -> list[dict]:
        """List artifacts with optional filters."""
        query = (
            "SELECT id, name, file_path, agent_id, agent_path, mime_type, "
            "description, tags, status, file_size, created_at, updated_at "
            "FROM artifacts WHERE 1=1"
        )
        params: dict = {}
        if agent_id:
            query += " AND agent_id = :agent_id"
            params["agent_id"] = agent_id
        if status:
            query += " AND status = :status"
            params["status"] = status
        if mime_type:
            query += " AND mime_type = :mime_type"
            params["mime_type"] = mime_type
        if tag:
            query += " AND tags @> :tag::jsonb"
            params["tag"] = json.dumps([tag])
        query += " ORDER BY created_at DESC"
        async with AsyncSessionLocal() as session:
            result = await session.execute(text(query), params)
            rows = result.fetchall()
            return [self._row_to_artifact(row) for row in rows]

    async def update_artifact_status(self, artifact_id: str, status: str) -> None:
        """Update artifact status."""
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "UPDATE artifacts SET status = :status, updated_at = :updated_at "
                    "WHERE id = :id"
                ),
                {"status": status, "updated_at": now, "id": artifact_id},
            )
            await session.commit()

    # ------------------------------------------------------------------
    # Persistent agents (employees) — queries on agents with profile_json
    # ------------------------------------------------------------------

    async def save_employee(self, profile_data: dict) -> None:
        """Save a persistent agent (employee) with profile metadata.

        Writes to the agents table with profile_json set.
        """
        from auton.models import HealthSnapshot
        now = datetime.now(timezone.utc)
        agent_id = profile_data["id"]
        spec = profile_data.get("spec", {})

        # Build profile (everything except id, status, spec)
        profile = {
            k: v for k, v in profile_data.items()
            if k not in ("id", "status", "spec")
        }

        # Map employee status → agent state
        status = profile_data.get("status", "active")
        state_map = {
            "active": "running",
            "waiting_for_input": "idle",
            "suspended": "suspended",
            "archived": "dead",
        }
        state = state_map.get(status, "idle")

        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO agents
                        (id, path, parent_path, spec_json, state, health_json,
                         restart_count, idle_reason, profile_json, created_at, updated_at)
                    VALUES (:id, :path, NULL, :spec_json, :state, :health_json,
                            0, NULL, :profile_json, :created_at, :updated_at)
                    ON CONFLICT (id) DO UPDATE SET
                        spec_json = EXCLUDED.spec_json,
                        state = EXCLUDED.state,
                        profile_json = EXCLUDED.profile_json,
                        updated_at = EXCLUDED.updated_at
                """),
                {
                    "id": agent_id,
                    "path": agent_id,
                    "spec_json": json.dumps(spec) if isinstance(spec, dict) else spec,
                    "state": state,
                    "health_json": HealthSnapshot().model_dump_json(),
                    "profile_json": json.dumps(profile),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            await session.commit()

    async def load_employees(self, status: str | None = None) -> list[dict]:
        """Load persistent agents (employees) as profile dicts."""
        state_filter = ""
        params: dict = {}
        if status:
            state_map = {
                "active": ("running", "correcting", "spawning"),
                "waiting_for_input": ("idle",),
                "suspended": ("suspended",),
                "archived": ("dead", "terminating"),
            }
            states = state_map.get(status, (status,))
            placeholders = ", ".join(f":s{i}" for i in range(len(states)))
            state_filter = f" AND state IN ({placeholders})"
            for i, s in enumerate(states):
                params[f"s{i}"] = s

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT id, spec_json, state, profile_json, created_at "
                    "FROM agents WHERE profile_json IS NOT NULL "
                    f"AND state NOT IN ('dead', 'terminating'){state_filter} "
                    "ORDER BY created_at DESC"
                ),
                params,
            )
            rows = result.fetchall()
            employees = []
            for row in rows:
                profile_json = row[3]
                profile = json.loads(profile_json) if isinstance(profile_json, str) else profile_json
                spec_json = row[1]
                spec = json.loads(spec_json) if isinstance(spec_json, str) else spec_json
                # Reconstruct the employee-style dict
                profile["id"] = row[0]
                profile["spec"] = spec
                profile["status"] = _STATE_TO_STATUS.get(row[2], row[2])
                employees.append(profile)
            return employees

    async def get_employee(self, employee_id: str) -> dict | None:
        """Get a persistent agent's profile by ID."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT id, spec_json, state, profile_json "
                    "FROM agents WHERE id = :id AND profile_json IS NOT NULL"
                ),
                {"id": employee_id},
            )
            row = result.fetchone()
            if not row:
                return None
            profile_json = row[3]
            profile = json.loads(profile_json) if isinstance(profile_json, str) else profile_json
            spec_json = row[1]
            profile["id"] = row[0]
            profile["spec"] = json.loads(spec_json) if isinstance(spec_json, str) else spec_json
            profile["status"] = _STATE_TO_STATUS.get(row[2], row[2])
            return profile

    async def update_employee_status(self, employee_id: str, status: str) -> None:
        """Update a persistent agent's state (using employee status names)."""
        now = datetime.now(timezone.utc)
        state_map = {
            "active": "running",
            "waiting_for_input": "idle",
            "suspended": "suspended",
            "archived": "dead",
        }
        state = state_map.get(status, status)
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "UPDATE agents SET state = :state, updated_at = :updated_at "
                    "WHERE id = :id"
                ),
                {"state": state, "updated_at": now, "id": employee_id},
            )
            await session.commit()

    async def delete_employee(self, employee_id: str) -> bool:
        """Delete a persistent agent."""
        return await self._delete_agent_by_id(employee_id)

    async def _delete_agent_by_id(self, agent_id: str) -> bool:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("DELETE FROM agents WHERE id = :id"),
                {"id": agent_id},
            )
            await session.commit()
            return result.rowcount > 0

    # ------------------------------------------------------------------
    # Tasks (queued work for persistent agents)
    # ------------------------------------------------------------------

    async def create_task(self, agent_id: str, definition: str, deliverables: list[dict]) -> dict:
        """Create a queued task for a persistent agent."""
        import uuid
        now = datetime.now(timezone.utc)
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        task = {
            "id": task_id,
            "agent_id": agent_id,
            "definition": definition,
            "deliverables": deliverables,
            "status": "queued",
            "session_id": None,
            "created_at": now.isoformat(),
            "started_at": None,
            "completed_at": None,
        }
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO tasks
                        (id, agent_id, definition, deliverables, status, created_at)
                    VALUES (:id, :agent_id, :definition, :deliverables, :status, :created_at)
                """),
                {
                    "id": task_id,
                    "agent_id": agent_id,
                    "definition": definition,
                    "deliverables": json.dumps(deliverables),
                    "status": "queued",
                    "created_at": now,
                },
            )
            await session.commit()
        return task

    async def list_tasks(self, agent_id: str, status: str | None = None) -> list[dict]:
        """List tasks for a persistent agent, optionally filtered by status."""
        query = (
            "SELECT id, agent_id, definition, deliverables, status, "
            "session_id, created_at, started_at, completed_at "
            "FROM tasks WHERE agent_id = :agent_id"
        )
        params: dict = {"agent_id": agent_id}
        if status:
            query += " AND status = :status"
            params["status"] = status
        query += " ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, created_at ASC"
        async with AsyncSessionLocal() as session:
            result = await session.execute(text(query), params)
            rows = result.fetchall()
            return [self._row_to_task(row) for row in rows]

    async def get_task(self, task_id: str) -> dict | None:
        """Get a single task by ID."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT id, agent_id, definition, deliverables, status, "
                    "session_id, created_at, started_at, completed_at "
                    "FROM tasks WHERE id = :id"
                ),
                {"id": task_id},
            )
            row = result.fetchone()
            return self._row_to_task(row) if row else None

    async def update_task_status(
        self, task_id: str, status: str, session_id: str | None = None,
    ) -> None:
        """Update task status and optional session linkage."""
        now = datetime.now(timezone.utc)
        sets = ["status = :status"]
        params: dict = {"id": task_id, "status": status}
        if status == "active":
            sets.append("started_at = :started_at")
            params["started_at"] = now
        if status == "completed":
            sets.append("completed_at = :completed_at")
            params["completed_at"] = now
        if session_id is not None:
            sets.append("session_id = :session_id")
            params["session_id"] = session_id
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(f"UPDATE tasks SET {', '.join(sets)} WHERE id = :id"),
                params,
            )
            await session.commit()

    async def delete_task(self, task_id: str) -> bool:
        """Delete a task."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("DELETE FROM tasks WHERE id = :id"),
                {"id": task_id},
            )
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    def _row_to_task(row) -> dict:
        deliverables = row[3]
        return {
            "id": row[0],
            "agent_id": row[1],
            "definition": row[2],
            "deliverables": json.loads(deliverables) if isinstance(deliverables, str) else deliverables,
            "status": row[4],
            "session_id": row[5],
            "created_at": row[6].isoformat() if hasattr(row[6], 'isoformat') else row[6],
            "started_at": row[7].isoformat() if hasattr(row[7], 'isoformat') else row[7] if row[7] else None,
            "completed_at": row[8].isoformat() if hasattr(row[8], 'isoformat') else row[8] if row[8] else None,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_artifact(row) -> dict:
        tags = row[7]
        return {
            "id": row[0],
            "name": row[1],
            "file_path": row[2],
            "agent_id": row[3],
            "agent_path": row[4],
            "mime_type": row[5],
            "description": row[6],
            "tags": json.loads(tags) if isinstance(tags, str) else tags,
            "status": row[8],
            "file_size": row[9],
            "created_at": row[10].isoformat() if hasattr(row[10], 'isoformat') else row[10],
            "updated_at": row[11].isoformat() if hasattr(row[11], 'isoformat') else row[11],
        }
