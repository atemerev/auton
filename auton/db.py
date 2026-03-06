"""SQLite persistence for agents, dossiers, and checkpoints."""

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
                policy_json TEXT NOT NULL,
                state TEXT NOT NULL,
                health_json TEXT NOT NULL,
                restart_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dossiers (
                agent_id TEXT PRIMARY KEY,
                data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (agent_id) REFERENCES agents(id)
            );

            CREATE TABLE IF NOT EXISTS checkpoints (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                checkpoint_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (agent_id) REFERENCES agents(id)
            );
        """)
        await self._conn.commit()
        logger.info(f"Database initialized at {self.path}")

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def save_agent(self, node) -> None:
        """Upsert agent state."""
        from auton.models import AgentState
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT OR REPLACE INTO agents
               (id, path, parent_path, spec_json, policy_json, state, health_json, restart_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                node.id,
                node.path,
                node.parent_path,
                node.spec.model_dump_json(),
                node.policy.model_dump_json(),
                node.state.value,
                node.health.model_dump_json(),
                node.restart_count,
                node.created_at.isoformat(),
                now,
            ),
        )
        await self._conn.commit()

    async def load_agents(self) -> list[dict]:
        """Load all non-dead agents from the database."""
        cursor = await self._conn.execute(
            "SELECT id, path, parent_path, spec_json, policy_json, state, health_json, restart_count, created_at "
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
                "policy": json.loads(row[4]),
                "state": row[5],
                "health": json.loads(row[6]),
                "restart_count": row[7],
                "created_at": row[8],
            })
        return agents

    async def save_dossier(self, agent_id: str, data: dict) -> None:
        """Save or update a dossier."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT OR REPLACE INTO dossiers (agent_id, data_json, updated_at) VALUES (?, ?, ?)",
            (agent_id, json.dumps(data, indent=2), now),
        )
        await self._conn.commit()

    async def load_dossier(self, agent_id: str) -> dict | None:
        """Load a dossier by agent ID."""
        cursor = await self._conn.execute(
            "SELECT data_json FROM dossiers WHERE agent_id = ?", (agent_id,)
        )
        row = await cursor.fetchone()
        if row:
            return json.loads(row[0])
        return None

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
        await self._conn.execute("DELETE FROM dossiers WHERE agent_id = ?", (agent_id,))
        await self._conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        await self._conn.commit()
