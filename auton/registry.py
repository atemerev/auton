"""Agent registry: filesystem-like tree of agents with path-based navigation."""

from __future__ import annotations

import uuid
from typing import Any

from .models import (
    AgentNode,
    AgentPolicy,
    AgentSpec,
    AgentState,
    InvalidTransition,
    SpawnRequest,
    SuspendReason,
)


class AgentRegistry:
    """Thread-safe agent tree with path-based lookup."""

    def __init__(self) -> None:
        self._roots: dict[str, AgentNode] = {}

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def resolve(self, path: str) -> AgentNode | None:
        """Resolve a slash-separated path to an agent node."""
        parts = [p for p in path.strip("/").split("/") if p]
        if not parts:
            return None
        node = self._roots.get(parts[0])
        for part in parts[1:]:
            if node is None:
                return None
            node = node.children.get(part)
        return node

    # ------------------------------------------------------------------
    # Tree queries
    # ------------------------------------------------------------------

    def list_all(self) -> list[dict[str, Any]]:
        """Return the full agent tree."""
        return [node.to_dict() for node in self._roots.values()]

    def subtree(self, path: str) -> dict[str, Any] | None:
        """Return the subtree rooted at path."""
        node = self.resolve(path)
        if node is None:
            return None
        return node.to_dict()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def spawn(self, req: SpawnRequest, parent_path: str | None = None) -> AgentNode:
        """Spawn a new agent, optionally under a parent."""
        agent_id = req.id or f"agent-{uuid.uuid4().hex[:8]}"

        if parent_path:
            parent = self.resolve(parent_path)
            if parent is None:
                raise AgentNotFound(f"Parent not found: {parent_path}")
            if len(parent.children) >= parent.policy.max_children:
                raise PolicyViolation(
                    f"Parent {parent_path} at max_children ({parent.policy.max_children})"
                )
            depth = parent.depth + 1
            if depth >= parent.policy.max_depth:
                raise PolicyViolation(
                    f"Max depth ({parent.policy.max_depth}) exceeded at {parent_path}"
                )
            if agent_id in parent.children:
                raise AgentExists(f"Agent {agent_id} already exists under {parent_path}")
            node = AgentNode(
                id=agent_id,
                spec=req.spec,
                policy=req.policy,
                parent_path=parent.path,
            )
            parent.children[agent_id] = node
        else:
            if agent_id in self._roots:
                raise AgentExists(f"Root agent {agent_id} already exists")
            node = AgentNode(id=agent_id, spec=req.spec, policy=req.policy)
            self._roots[agent_id] = node

        # Immediately transition to running (MVP — no async boot)
        node.transition(AgentState.RUNNING)
        node._log_event("spawned", {"goal": req.spec.goal, "model": req.spec.model})
        return node

    def terminate(self, path: str, cascade: bool = True) -> AgentNode:
        """Terminate an agent. If cascade, also terminate children."""
        node = self.resolve(path)
        if node is None:
            raise AgentNotFound(path)

        if cascade:
            for child in list(node.children.values()):
                self._terminate_recursive(child)

        node.transition(AgentState.TERMINATING)
        node.transition(AgentState.DEAD)
        node._log_event("terminated")
        return node

    def _terminate_recursive(self, node: AgentNode) -> None:
        for child in list(node.children.values()):
            self._terminate_recursive(child)
        if node.state != AgentState.DEAD:
            node.transition(AgentState.TERMINATING)
            node.transition(AgentState.DEAD)

    def remove_dead(self, path: str) -> None:
        """Remove a dead agent from the tree."""
        node = self.resolve(path)
        if node is None:
            raise AgentNotFound(path)
        if node.state != AgentState.DEAD:
            raise InvalidTransition("Can only remove dead agents")

        if node.parent_path:
            parent = self.resolve(node.parent_path)
            if parent:
                parent.children.pop(node.id, None)
        else:
            self._roots.pop(node.id, None)

    def suspend(self, path: str, reason: SuspendReason = SuspendReason.MANUAL) -> AgentNode:
        """Suspend an agent."""
        node = self.resolve(path)
        if node is None:
            raise AgentNotFound(path)
        node.transition(AgentState.SUSPENDED)
        node.suspend_reason = reason
        node._log_event("suspended", {"reason": reason.value})
        return node

    def resume(self, path: str) -> AgentNode:
        """Resume a suspended agent."""
        node = self.resolve(path)
        if node is None:
            raise AgentNotFound(path)
        if node.state != AgentState.SUSPENDED:
            raise InvalidTransition(f"Agent {path} is {node.state.value}, not suspended")
        node.transition(AgentState.RUNNING)
        node.suspend_reason = None
        node._log_event("resumed")
        return node

    def correct(self, path: str, guidance: str) -> AgentNode:
        """Inject a correction into a running/idle agent."""
        node = self.resolve(path)
        if node is None:
            raise AgentNotFound(path)
        if node.state not in (AgentState.RUNNING, AgentState.IDLE):
            raise InvalidTransition(
                f"Cannot correct agent in state {node.state.value}"
            )
        node.transition(AgentState.CORRECTING)
        node.messages.append({
            "role": "correction",
            "content": guidance,
            "timestamp": node.log[-1]["timestamp"] if node.log else None,
        })
        node._log_event("correction", {"guidance": guidance[:200]})
        # Transition back to running after correction
        node.transition(AgentState.RUNNING)
        return node

    def checkpoint(self, path: str) -> dict:
        """Create a checkpoint for an agent."""
        node = self.resolve(path)
        if node is None:
            raise AgentNotFound(path)
        return node.checkpoint()

    def fork(self, path: str, new_id: str | None = None) -> AgentNode:
        """Fork an agent from its latest checkpoint."""
        node = self.resolve(path)
        if node is None:
            raise AgentNotFound(path)
        if not node.checkpoints:
            raise PolicyViolation(f"Agent {path} has no checkpoints to fork from")

        fork_id = new_id or f"{node.id}-fork-{uuid.uuid4().hex[:4]}"
        req = SpawnRequest(id=fork_id, spec=node.spec, policy=node.policy)
        forked = self.spawn(req, parent_path=node.parent_path)
        forked._log_event("forked", {"from": path, "checkpoint": node.checkpoints[-1]["id"]})
        return forked

    def send_message(self, path: str, content: str, channel: str = "user", kind: str = "message", metadata: dict | None = None) -> AgentNode:
        """Send a message to an agent."""
        node = self.resolve(path)
        if node is None:
            raise AgentNotFound(path)
        if node.state in (AgentState.DEAD, AgentState.TERMINATING):
            raise InvalidTransition(f"Cannot message agent in state {node.state.value}")
        msg = {
            "content": content,
            "channel": channel,
            "kind": kind,
            "metadata": metadata or {},
            "timestamp": node.health.last_check.isoformat(),
        }
        node.messages.append(msg)
        node._log_event("message_received", {"channel": channel, "kind": kind})
        # If idle, wake up
        if node.state == AgentState.IDLE:
            node.transition(AgentState.RUNNING)
        return node

    def restart(self, path: str) -> AgentNode:
        """Restart an agent from its latest checkpoint."""
        node = self.resolve(path)
        if node is None:
            raise AgentNotFound(path)
        if node.restart_count >= node.policy.max_restarts:
            raise PolicyViolation(
                f"Agent {path} exceeded max_restarts ({node.policy.max_restarts})"
            )
        # Terminate and re-spawn in place
        old_state = node.state
        if old_state not in (AgentState.DEAD, AgentState.TERMINATING):
            node.transition(AgentState.TERMINATING)
            node.transition(AgentState.DEAD)

        # Re-initialize
        node.state = AgentState.SPAWNING
        node.restart_count += 1
        node.health = node.health.model_copy()
        node.health.loops_detected = 0
        node.health.drift = 0.0
        node.transition(AgentState.RUNNING)
        node._log_event("restarted", {"restart_count": node.restart_count})
        return node


class AgentNotFound(Exception):
    pass


class AgentExists(Exception):
    pass


class PolicyViolation(Exception):
    pass
