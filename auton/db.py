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

            CREATE TABLE IF NOT EXISTS employees (
                id TEXT PRIMARY KEY,
                profile_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                agent_path TEXT NOT NULL,
                mime_type TEXT NOT NULL DEFAULT 'text/markdown',
                description TEXT NOT NULL DEFAULT '',
                tags_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'expected',
                file_size INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (agent_id) REFERENCES agents(id)
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
        # Migrate old artifacts schema (had source_agent_id instead of agent_id)
        try:
            cursor = await self._conn.execute("PRAGMA table_info(artifacts)")
            columns = [row[1] for row in await cursor.fetchall()]
            if "source_agent_id" in columns:
                await self._conn.execute("DROP TABLE artifacts")
                await self._conn.execute("""
                    CREATE TABLE artifacts (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        agent_id TEXT NOT NULL,
                        agent_path TEXT NOT NULL,
                        mime_type TEXT NOT NULL DEFAULT 'text/markdown',
                        description TEXT NOT NULL DEFAULT '',
                        tags_json TEXT NOT NULL DEFAULT '[]',
                        status TEXT NOT NULL DEFAULT 'expected',
                        file_size INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY (agent_id) REFERENCES agents(id)
                    )
                """)
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

    async def save_artifact(self, record: dict) -> None:
        """Upsert an artifact record."""
        now = datetime.now(timezone.utc).isoformat()
        tags_json = json.dumps(record.get("tags", []))
        await self._conn.execute(
            """INSERT OR REPLACE INTO artifacts
               (id, name, file_path, agent_id, agent_path, mime_type,
                description, tags_json, status, file_size, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record["id"],
                record["name"],
                record["file_path"],
                record["agent_id"],
                record["agent_path"],
                record.get("mime_type", "text/markdown"),
                record.get("description", ""),
                tags_json,
                record.get("status", "expected"),
                record.get("file_size", 0),
                record.get("created_at", now),
                now,
            ),
        )
        await self._conn.commit()

    async def get_artifact(self, artifact_id: str) -> dict | None:
        """Get a single artifact by ID."""
        cursor = await self._conn.execute(
            "SELECT id, name, file_path, agent_id, agent_path, mime_type, "
            "description, tags_json, status, file_size, created_at, updated_at "
            "FROM artifacts WHERE id = ?",
            (artifact_id,),
        )
        row = await cursor.fetchone()
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
            "description, tags_json, status, file_size, created_at, updated_at "
            "FROM artifacts WHERE 1=1"
        )
        params: list = []
        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        if mime_type:
            query += " AND mime_type = ?"
            params.append(mime_type)
        if tag:
            query += " AND tags_json LIKE ?"
            params.append(f'%"{tag}"%')
        query += " ORDER BY created_at DESC"
        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()
        return [self._row_to_artifact(row) for row in rows]

    async def update_artifact_status(self, artifact_id: str, status: str) -> None:
        """Update artifact status."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "UPDATE artifacts SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, artifact_id),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Employees
    # ------------------------------------------------------------------

    async def save_employee(self, profile_data: dict) -> None:
        """Upsert an employee profile."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT OR REPLACE INTO employees
               (id, profile_json, status, created_at, updated_at)
               VALUES (?, ?, ?, COALESCE(
                   (SELECT created_at FROM employees WHERE id = ?), ?
               ), ?)""",
            (
                profile_data["id"],
                json.dumps(profile_data),
                profile_data.get("status", "active"),
                profile_data["id"],
                now,
                now,
            ),
        )
        await self._conn.commit()

    async def load_employees(self, status: str | None = None) -> list[dict]:
        """Load employee profiles, optionally filtered by status."""
        if status:
            cursor = await self._conn.execute(
                "SELECT profile_json FROM employees WHERE status = ? ORDER BY created_at DESC",
                (status,),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT profile_json FROM employees ORDER BY created_at DESC"
            )
        rows = await cursor.fetchall()
        return [json.loads(row[0]) for row in rows]

    async def get_employee(self, employee_id: str) -> dict | None:
        """Get a single employee by ID."""
        cursor = await self._conn.execute(
            "SELECT profile_json FROM employees WHERE id = ?",
            (employee_id,),
        )
        row = await cursor.fetchone()
        return json.loads(row[0]) if row else None

    async def update_employee_status(self, employee_id: str, status: str) -> None:
        """Update employee status (active/suspended/archived)."""
        now = datetime.now(timezone.utc).isoformat()
        # Also update status inside the profile JSON
        cursor = await self._conn.execute(
            "SELECT profile_json FROM employees WHERE id = ?", (employee_id,)
        )
        row = await cursor.fetchone()
        if row:
            profile = json.loads(row[0])
            profile["status"] = status
            await self._conn.execute(
                "UPDATE employees SET profile_json = ?, status = ?, updated_at = ? WHERE id = ?",
                (json.dumps(profile), status, now, employee_id),
            )
            await self._conn.commit()

    async def delete_employee(self, employee_id: str) -> bool:
        """Delete an employee. Returns True if deleted."""
        cursor = await self._conn.execute(
            "DELETE FROM employees WHERE id = ?", (employee_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_artifact(row) -> dict:
        return {
            "id": row[0],
            "name": row[1],
            "file_path": row[2],
            "agent_id": row[3],
            "agent_path": row[4],
            "mime_type": row[5],
            "description": row[6],
            "tags": json.loads(row[7]),
            "status": row[8],
            "file_size": row[9],
            "created_at": row[10],
            "updated_at": row[11],
        }
