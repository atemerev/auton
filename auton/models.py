"""Core data models: agent state machine, specs, policies, health."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Agent state machine
# ---------------------------------------------------------------------------
#
#  spawning ──► running ◄──► idle
#                  │              │
#                  ▼              │
#             correcting ────────┘
#                  │
#                  ▼
#             suspended
#                  │
#                  ▼
#            terminating ──► dead
#
# Additional transitions:
#   any → terminating (explicit kill)
#   suspended → running (resume)


class AgentState(str, Enum):
    SPAWNING = "spawning"
    RUNNING = "running"
    IDLE = "idle"
    CORRECTING = "correcting"
    SUSPENDED = "suspended"
    TERMINATING = "terminating"
    DEAD = "dead"


class SuspendReason(str, Enum):
    BUDGET_EXCEEDED = "budget_exceeded"
    DRIFT_DETECTED = "drift_detected"
    LOOP_DETECTED = "loop_detected"
    DEPTH_EXCEEDED = "depth_exceeded"
    MANUAL = "manual"


class RestartPolicy(str, Enum):
    NEVER = "never"
    ON_FAILURE = "on_failure"
    ALWAYS = "always"


class SupervisionStrategy(str, Enum):
    ONE_FOR_ONE = "one_for_one"
    ONE_FOR_ALL = "one_for_all"
    ESCALATE = "escalate"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class BudgetSpec(BaseModel):
    max_tokens_per_hour: int | None = None
    max_total_tokens: int | None = None
    max_runtime_seconds: int | None = None


class AgentPolicy(BaseModel):
    restart: RestartPolicy = RestartPolicy.ON_FAILURE
    supervision: SupervisionStrategy = SupervisionStrategy.ONE_FOR_ONE
    max_restarts: int = 3
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    drift_threshold: float = 0.4
    max_children: int = 10
    max_depth: int = 3


class AgentSpec(BaseModel):
    goal: str
    model: str = "claude-sonnet-4-6"
    tools: list[str] = Field(default_factory=list)
    schedule: str | None = None  # cron expression for periodic agents


class HealthSnapshot(BaseModel):
    coherence: float = 1.0        # 0-1, similarity to original goal
    drift: float = 0.0            # 0-1, accumulated drift from goal
    token_rate: float = 0.0       # tokens/hour
    tokens_total: int = 0
    loops_detected: int = 0
    last_check: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SpawnRequest(BaseModel):
    id: str | None = None
    spec: AgentSpec
    policy: AgentPolicy = Field(default_factory=AgentPolicy)


class CorrectionRequest(BaseModel):
    guidance: str
    force: bool = False  # if True, override even running agents


class MessageRequest(BaseModel):
    content: str
    channel: str = "user"
    kind: str = "message"
    metadata: dict[str, Any] = Field(default_factory=dict)


class SuspendRequest(BaseModel):
    reason: SuspendReason = SuspendReason.MANUAL


# ---------------------------------------------------------------------------
# Agent node
# ---------------------------------------------------------------------------


class AgentNode:
    """A single agent in the supervision tree."""

    def __init__(
        self,
        id: str,
        spec: AgentSpec,
        policy: AgentPolicy,
        parent_path: str | None = None,
    ):
        self.id = id
        self.spec = spec
        self.policy = policy
        self.parent_path = parent_path
        self.state = AgentState.SPAWNING
        self.suspend_reason: SuspendReason | None = None
        self.health = HealthSnapshot()
        self.children: dict[str, AgentNode] = {}
        self.created_at = datetime.now(timezone.utc)
        self.started_at: datetime | None = None
        self.restart_count = 0
        self.checkpoints: list[dict[str, Any]] = []
        self.log: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []

    @property
    def path(self) -> str:
        if self.parent_path:
            return f"{self.parent_path}/{self.id}"
        return self.id

    @property
    def depth(self) -> int:
        return self.path.count("/")

    @property
    def uptime(self) -> str:
        if not self.started_at:
            return "0s"
        delta = datetime.now(timezone.utc) - self.started_at
        days = delta.days
        hours, rem = divmod(delta.seconds, 3600)
        mins, secs = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if mins:
            parts.append(f"{mins}m")
        if not parts:
            parts.append(f"{secs}s")
        return " ".join(parts)

    def transition(self, new_state: AgentState) -> None:
        """Transition to a new state with validation."""
        valid = _VALID_TRANSITIONS.get(self.state, set())
        # any state can go to terminating
        if new_state == AgentState.TERMINATING or new_state in valid:
            old = self.state
            self.state = new_state
            if new_state == AgentState.RUNNING and self.started_at is None:
                self.started_at = datetime.now(timezone.utc)
            self._log_event("state_change", {"from": old.value, "to": new_state.value})
        else:
            raise InvalidTransition(
                f"Cannot transition from {self.state.value} to {new_state.value}"
            )

    def _log_event(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            **(data or {}),
        }
        self.log.append(entry)
        # keep log bounded
        if len(self.log) > 1000:
            self.log = self.log[-500:]

    def to_dict(self, include_children: bool = True, depth: int = 0, max_depth: int = 10) -> dict[str, Any]:
        """Serialize to API response format."""
        result: dict[str, Any] = {
            "id": self.id,
            "path": self.path,
            "state": self.state.value,
            "spec": self.spec.model_dump(),
            "policy": self.policy.model_dump(),
            "health": self.health.model_dump(),
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "uptime": self.uptime,
            "restart_count": self.restart_count,
            "checkpoints": len(self.checkpoints),
        }
        if self.suspend_reason:
            result["suspend_reason"] = self.suspend_reason.value
        if include_children and depth < max_depth:
            result["children"] = [
                child.to_dict(include_children=True, depth=depth + 1, max_depth=max_depth)
                for child in self.children.values()
            ]
        else:
            result["children_count"] = len(self.children)
        return result

    def checkpoint(self) -> dict[str, Any]:
        """Create a checkpoint snapshot."""
        snap = {
            "id": str(uuid.uuid4())[:8],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "state": self.state.value,
            "health": self.health.model_dump(),
            "messages_count": len(self.messages),
            "spec": self.spec.model_dump(),
        }
        self.checkpoints.append(snap)
        self._log_event("checkpoint", {"checkpoint_id": snap["id"]})
        return snap


class InvalidTransition(Exception):
    pass


# Valid state transitions (excluding universal → terminating)
_VALID_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.SPAWNING: {AgentState.RUNNING, AgentState.DEAD},
    AgentState.RUNNING: {AgentState.IDLE, AgentState.CORRECTING, AgentState.SUSPENDED},
    AgentState.IDLE: {AgentState.RUNNING, AgentState.CORRECTING, AgentState.SUSPENDED},
    AgentState.CORRECTING: {AgentState.RUNNING, AgentState.SUSPENDED},
    AgentState.SUSPENDED: {AgentState.RUNNING, AgentState.DEAD},
    AgentState.TERMINATING: {AgentState.DEAD},
    AgentState.DEAD: set(),
}
