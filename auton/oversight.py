"""Oversight engine: coherence, drift, loop detection, budget enforcement."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from .models import AgentNode, AgentState, HealthSnapshot, SuspendReason
from .registry import AgentRegistry


class OversightEngine:
    """Periodic health checker for all agents in the registry."""

    def __init__(self, registry: AgentRegistry):
        self.registry = registry
        self._loop_window: dict[str, list[str]] = {}  # path → recent actions

    def check_all(self) -> list[OversightEvent]:
        """Run oversight checks on every live agent."""
        events: list[OversightEvent] = []
        for root in self.registry._roots.values():
            events.extend(self._check_recursive(root))
        return events

    def check_agent(self, path: str) -> list[OversightEvent]:
        """Run oversight checks on a single agent."""
        node = self.registry.resolve(path)
        if node is None:
            return []
        return self._check_node(node)

    def _check_recursive(self, node: AgentNode) -> list[OversightEvent]:
        events = self._check_node(node)
        for child in node.children.values():
            events.extend(self._check_recursive(child))
        return events

    def _check_node(self, node: AgentNode) -> list[OversightEvent]:
        if node.state in (AgentState.DEAD, AgentState.TERMINATING, AgentState.SPAWNING):
            return []

        events: list[OversightEvent] = []
        now = datetime.now(timezone.utc)
        node.health.last_check = now

        # Budget check: total tokens
        if node.spec.max_tokens and node.health.tokens_total >= node.spec.max_tokens:
            events.append(OversightEvent(
                path=node.path,
                kind="budget_exceeded",
                detail=f"Total tokens {node.health.tokens_total} >= {node.spec.max_tokens}",
                action="suspend",
            ))
            self._auto_suspend(node, SuspendReason.BUDGET_EXCEEDED)

        # Budget check: token rate
        if node.spec.max_tokens_per_hour and node.health.token_rate > node.spec.max_tokens_per_hour:
            events.append(OversightEvent(
                path=node.path,
                kind="token_rate_exceeded",
                detail=f"Token rate {node.health.token_rate:.0f}/h > {node.spec.max_tokens_per_hour}/h",
                action="warn",
            ))

        # Budget check: runtime
        if node.spec.max_runtime_seconds and node.started_at:
            elapsed = (now - node.started_at).total_seconds()
            if elapsed > node.spec.max_runtime_seconds:
                events.append(OversightEvent(
                    path=node.path,
                    kind="runtime_exceeded",
                    detail=f"Runtime {elapsed:.0f}s > {node.spec.max_runtime_seconds}s",
                    action="suspend",
                ))
                self._auto_suspend(node, SuspendReason.BUDGET_EXCEEDED)

        # Drift check
        if node.health.drift > node.spec.drift_threshold:
            events.append(OversightEvent(
                path=node.path,
                kind="drift_detected",
                detail=f"Drift {node.health.drift:.2f} > threshold {node.spec.drift_threshold}",
                action="suspend",
            ))
            self._auto_suspend(node, SuspendReason.DRIFT_DETECTED)

        # Loop detection (simple: check recent log for repeated patterns)
        recent = node.log[-20:]
        if len(recent) >= 10:
            event_types = [e.get("type", "") for e in recent]
            # Detect if the same sequence of 3 events repeats 3+ times
            for window_size in (2, 3, 4):
                if len(event_types) >= window_size * 3:
                    pattern = tuple(event_types[-window_size:])
                    count = 0
                    for i in range(len(event_types) - window_size + 1):
                        if tuple(event_types[i:i + window_size]) == pattern:
                            count += 1
                    if count >= 3:
                        node.health.loops_detected += 1
                        events.append(OversightEvent(
                            path=node.path,
                            kind="loop_detected",
                            detail=f"Repeated pattern detected ({count}x): {pattern}",
                            action="warn" if node.health.loops_detected < 3 else "suspend",
                        ))
                        if node.health.loops_detected >= 3:
                            self._auto_suspend(node, SuspendReason.LOOP_DETECTED)
                        break

        # Coherence check (low coherence = drifting from goal)
        if node.health.coherence < 0.3:
            events.append(OversightEvent(
                path=node.path,
                kind="low_coherence",
                detail=f"Coherence {node.health.coherence:.2f} is critically low",
                action="suspend",
            ))
            self._auto_suspend(node, SuspendReason.DRIFT_DETECTED)

        return events

    def _auto_suspend(self, node: AgentNode, reason: SuspendReason) -> None:
        """Suspend an agent if it's still in a suspendable state."""
        if node.state in (AgentState.RUNNING, AgentState.IDLE, AgentState.CORRECTING):
            try:
                self.registry.suspend(node.path, reason)
            except Exception:
                pass  # Already suspended or transitioning


class OversightEvent:
    """A single oversight finding."""

    def __init__(self, path: str, kind: str, detail: str, action: str = "warn"):
        self.path = path
        self.kind = kind
        self.detail = detail
        self.action = action  # "warn", "suspend", "terminate"
        self.timestamp = datetime.now(timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "detail": self.detail,
            "action": self.action,
            "timestamp": self.timestamp.isoformat(),
        }
