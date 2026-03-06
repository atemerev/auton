"""Cron-style scheduler for periodic agent re-execution."""

import asyncio
import logging
from datetime import datetime, timezone

from croniter import croniter

from auton.models import AgentState

logger = logging.getLogger(__name__)


class Scheduler:
    """Checks scheduled agents every minute and re-executes them."""

    def __init__(self, registry, publish_event, db):
        self.registry = registry
        self.publish_event = publish_event
        self.db = db
        self._running = False
        self._task: asyncio.Task | None = None
        # Track last execution time per agent path
        self._last_run: dict[str, datetime] = {}

    def start(self) -> asyncio.Task:
        """Start the scheduler loop as a background task."""
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="scheduler")
        logger.info("Scheduler started")
        return self._task

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _loop(self) -> None:
        """Main scheduler loop — check every 60 seconds."""
        while self._running:
            try:
                await self._check_schedules()
            except Exception as e:
                logger.error(f"Scheduler error: {e}", exc_info=True)
            await asyncio.sleep(60)

    async def _check_schedules(self) -> None:
        """Check all scheduled agents and trigger those that are due."""
        now = datetime.now(timezone.utc)

        for node in self._all_scheduled_agents():
            if node.state != AgentState.IDLE:
                continue

            schedule = node.spec.schedule
            if not schedule:
                continue

            last_run = self._last_run.get(node.path, node.created_at)

            try:
                cron = croniter(schedule, last_run)
                next_run = cron.get_next(datetime)
                # Make next_run timezone-aware if it isn't
                if next_run.tzinfo is None:
                    next_run = next_run.replace(tzinfo=timezone.utc)
            except (ValueError, KeyError) as e:
                logger.error(f"Invalid cron expression for {node.path}: {schedule} - {e}")
                continue

            if now >= next_run:
                logger.info(f"Scheduler triggering {node.path} (schedule: {schedule})")
                self._last_run[node.path] = now
                await self._trigger(node)

    async def _trigger(self, node) -> None:
        """Re-execute a scheduled agent."""
        from auton.executor import AgentExecutor

        try:
            node.transition(AgentState.RUNNING)
            executor = AgentExecutor(node, self.publish_event, self.db)
            self.registry._executors[node.path] = executor
            executor.start()
            node._log_event("scheduled_run", {"schedule": node.spec.schedule})
            self.publish_event(node.path, {"type": "scheduled_run"})
        except Exception as e:
            logger.error(f"Failed to trigger scheduled run for {node.path}: {e}")

    def _all_scheduled_agents(self):
        """Yield all agents that have a schedule configured."""
        for root in self.registry._roots.values():
            yield from self._walk_scheduled(root)

    def _walk_scheduled(self, node):
        """Recursively walk the agent tree for scheduled agents."""
        if node.spec.schedule:
            yield node
        for child in node.children.values():
            yield from self._walk_scheduled(child)
