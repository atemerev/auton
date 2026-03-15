"""SQLite persistence for agents, checkpoints, and spec templates."""

import json
import logging
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger(__name__)


class Database:
    """Async SQLite database for Auton persistence."""

    def __init__(self, path: str = "auton.db"):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Initialize database and create tables."""
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                parent_path TEXT,
                spec_json TEXT NOT NULL,
                state TEXT NOT NULL,
                health_json TEXT NOT NULL,
                restart_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS checkpoints (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                checkpoint_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (agent_id) REFERENCES agents(id)
            );

            CREATE TABLE IF NOT EXISTS spec_templates (
                name TEXT PRIMARY KEY,
                spec_json TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                source_agent_id TEXT NOT NULL,
                target_agent_id TEXT,
                file_path TEXT NOT NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
        """)
        # Migrations
        try:
            cursor = await self._conn.execute("PRAGMA table_info(agents)")
            columns = [row[1] for row in await cursor.fetchall()]
            if "idle_reason" not in columns:
                await self._conn.execute("ALTER TABLE agents ADD COLUMN idle_reason TEXT")
        except Exception:
            pass
        await self._conn.commit()
        logger.info(f"Database initialized at {self.path}")

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------

    async def save_agent(self, node) -> None:
        """Upsert agent state."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT OR REPLACE INTO agents
               (id, path, parent_path, spec_json, state, health_json, restart_count, idle_reason, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                node.id,
                node.path,
                node.parent_path,
                node.spec.model_dump_json(),
                node.state.value,
                node.health.model_dump_json(),
                node.restart_count,
                node.idle_reason.value if node.idle_reason else None,
                node.created_at.isoformat(),
                now,
            ),
        )
        await self._conn.commit()

    async def load_agents(self) -> list[dict]:
        """Load all non-dead agents from the database."""
        cursor = await self._conn.execute(
            "SELECT id, path, parent_path, spec_json, state, health_json, restart_count, created_at, idle_reason "
            "FROM agents WHERE state NOT IN ('dead', 'terminating')"
        )
        rows = await cursor.fetchall()
        agents = []
        for row in rows:
            agents.append({
                "id": row[0],
                "path": row[1],
                "parent_path": row[2],
                "spec": json.loads(row[3]),
                "state": row[4],
                "health": json.loads(row[5]),
                "restart_count": row[6],
                "created_at": row[7],
                "idle_reason": row[8],
            })
        return agents

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------

    async def save_checkpoint(self, agent_id: str, checkpoint: dict) -> None:
        """Save a checkpoint."""
        now = datetime.now(timezone.utc).isoformat()
        cp_id = checkpoint.get("id", "")
        await self._conn.execute(
            "INSERT OR REPLACE INTO checkpoints (id, agent_id, checkpoint_json, created_at) VALUES (?, ?, ?, ?)",
            (cp_id, agent_id, json.dumps(checkpoint), now),
        )
        await self._conn.commit()

    async def delete_agent(self, agent_id: str) -> None:
        """Delete an agent and its associated data."""
        await self._conn.execute("DELETE FROM checkpoints WHERE agent_id = ?", (agent_id,))
        await self._conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Spec Templates
    # ------------------------------------------------------------------

    async def save_template(self, name: str, spec_data: dict, description: str = "") -> None:
        """Save or update a spec template."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT OR REPLACE INTO spec_templates
               (name, spec_json, description, created_at, updated_at)
               VALUES (?, ?, ?, COALESCE(
                   (SELECT created_at FROM spec_templates WHERE name = ?), ?
               ), ?)""",
            (name, json.dumps(spec_data), description, name, now, now),
        )
        await self._conn.commit()

    async def load_template(self, name: str) -> dict | None:
        """Load a spec template by name."""
        cursor = await self._conn.execute(
            "SELECT spec_json, description, created_at, updated_at FROM spec_templates WHERE name = ?",
            (name,),
        )
        row = await cursor.fetchone()
        if row:
            return {
                "name": name,
                "spec": json.loads(row[0]),
                "description": row[1],
                "created_at": row[2],
                "updated_at": row[3],
            }
        return None

    async def list_templates(self) -> list[dict]:
        """List all spec templates."""
        cursor = await self._conn.execute(
            "SELECT name, description, created_at, updated_at FROM spec_templates ORDER BY name"
        )
        rows = await cursor.fetchall()
        return [
            {"name": row[0], "description": row[1], "created_at": row[2], "updated_at": row[3]}
            for row in rows
        ]

    async def delete_template(self, name: str) -> bool:
        """Delete a spec template. Returns True if deleted."""
        cursor = await self._conn.execute(
            "DELETE FROM spec_templates WHERE name = ?", (name,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------

    async def save_artifact(
        self, artifact_id: str, source_id: str, target_id: str | None,
        file_path: str, file_size: int,
    ) -> None:
        """Record an artifact transfer."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT OR REPLACE INTO artifacts (id, source_agent_id, target_agent_id, file_path, file_size, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (artifact_id, source_id, target_id, file_path, file_size, now),
        )
        await self._conn.commit()

    async def list_artifacts(self, agent_id: str) -> list[dict]:
        """List artifacts created by or sent to an agent."""
        cursor = await self._conn.execute(
            "SELECT id, source_agent_id, target_agent_id, file_path, file_size, created_at "
            "FROM artifacts WHERE source_agent_id = ? OR target_agent_id = ? "
            "ORDER BY created_at DESC",
            (agent_id, agent_id),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "source_agent_id": row[1],
                "target_agent_id": row[2],
                "file_path": row[3],
                "file_size": row[4],
                "created_at": row[5],
            }
            for row in rows
        ]
