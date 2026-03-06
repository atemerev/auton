"""Agent executor — runs the LLM tool-calling loop for an agent.

Modeled on Lethe's ActorRunner. The executor operates ON the AgentNode
(agents as data) but doesn't subclass it.
"""

import asyncio
import logging
from datetime import datetime, timezone

from auton.llm import LLMClient
from auton.models import AgentNode, AgentState, SuspendReason
from auton.tools import TOOL_REGISTRY, function_to_schema
from auton.tools.dossier import (
    get_dossier,
    make_finish_research,
    make_read_dossier,
    make_update_dossier,
    set_dossier,
)

logger = logging.getLogger(__name__)

MAX_TURNS = 30
CHECKPOINT_INTERVAL = 5


class AgentExecutor:
    """Executes an agent's LLM loop as an asyncio.Task."""

    def __init__(self, node: AgentNode, publish_event, db=None):
        self.node = node
        self.publish_event = publish_event
        self.db = db
        self._task: asyncio.Task | None = None

    def start(self) -> asyncio.Task:
        """Start execution as a background task."""
        self._task = asyncio.create_task(
            self._run_safe(), name=f"agent-{self.node.id}"
        )
        return self._task

    def stop(self) -> None:
        """Cancel the execution task."""
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run_safe(self) -> None:
        """Wrapper that catches exceptions and handles state transitions."""
        failed = False
        error_msg = ""
        try:
            await self.run()
        except asyncio.CancelledError:
            logger.info(f"Agent {self.node.id} execution cancelled")
        except Exception as e:
            failed = True
            error_msg = str(e)
            logger.error(f"Agent {self.node.id} execution failed: {e}", exc_info=True)
            self.node._log_event("execution_error", {"message": error_msg})
            self.publish_event(self.node.path, {
                "type": "execution_error",
                "error": error_msg,
            })
        finally:
            if self.node.state == AgentState.RUNNING:
                if failed:
                    # Suspend with error reason so the user sees why it stopped
                    self.node.error_message = error_msg
                    self.node.transition(AgentState.SUSPENDED)
                    self.node.suspend_reason = SuspendReason.EXECUTION_ERROR
                    self.node._log_event("suspended", {
                        "reason": "execution_error",
                        "detail": error_msg,
                    })
                else:
                    self.node.transition(AgentState.IDLE)
            if self.db:
                await self.db.save_agent(self.node)
                dossier = get_dossier(self.node.id)
                if dossier.get("last_updated"):
                    await self.db.save_dossier(self.node.id, dossier)

    async def run(self) -> None:
        """Main execution loop."""
        node = self.node
        logger.info(f"Starting execution for agent {node.id}: {node.spec.goal}")

        # Load existing dossier from DB if available
        if self.db:
            existing = await self.db.load_dossier(node.id)
            if existing:
                set_dossier(node.id, existing)
                logger.info(f"Loaded existing dossier for {node.id}")

        # Build system prompt
        system_prompt = self._build_system_prompt()

        # Budget check callback — called after every API call inside LLM client
        def _check_budget() -> bool:
            # Update health metrics on every API call
            node.health.tokens_total = llm.usage.total_tokens
            elapsed = (datetime.now(timezone.utc) - node.created_at).total_seconds()
            if elapsed > 0:
                node.health.token_rate = node.health.tokens_total / (elapsed / 3600)
            node.health.last_check = datetime.now(timezone.utc)

            budget = node.policy.budget
            if budget.max_total_tokens and llm.usage.total_tokens >= budget.max_total_tokens:
                logger.warning(f"Agent {node.id} token budget exceeded: {llm.usage.total_tokens}")
                return True
            if budget.max_runtime_seconds and node.started_at:
                runtime = (datetime.now(timezone.utc) - node.started_at).total_seconds()
                if runtime > budget.max_runtime_seconds:
                    logger.warning(f"Agent {node.id} runtime budget exceeded: {runtime:.0f}s")
                    return True
            return False

        # Create LLM client
        llm = LLMClient(
            model=node.spec.model,
            system_prompt=system_prompt,
            budget_check=_check_budget,
        )

        # Register tools from spec
        for tool_name in node.spec.tools:
            if tool_name in TOOL_REGISTRY:
                func, schema = TOOL_REGISTRY[tool_name]
                llm.add_tool(func, schema)

        # Register dossier tools (bound to this agent)
        read_fn = make_read_dossier(node.id)
        llm.add_tool(read_fn, function_to_schema(read_fn))

        update_fn = make_update_dossier(node.id)
        llm.add_tool(update_fn, function_to_schema(update_fn))

        finish_fn = make_finish_research(node.id, lambda: setattr(llm, '_stop_requested', True))
        llm.add_tool(finish_fn, function_to_schema(finish_fn))

        logger.info(f"Registered tools for {node.id}: {list(llm._tools.keys())}")

        # Build initial message
        has_existing = bool(get_dossier(node.id).get("last_updated"))
        if has_existing:
            initial_message = (
                f"Continue researching: {node.spec.goal}\n\n"
                "This is a recurring run. Read the existing dossier first with read_dossier(), "
                "then search for NEW information, updates, or changes. "
                "Focus on recent news, social media activity, and any changes since the last run."
            )
        else:
            initial_message = (
                f"Research this person: {node.spec.goal}\n\n"
                "Begin by searching for them. Build a comprehensive dossier covering "
                "basic info, social media profiles, career history, education, publications, "
                "and recent news. Update the dossier as you find information."
            )

        # Publish start event
        self.publish_event(node.path, {
            "type": "execution_started",
            "goal": node.spec.goal,
            "model": node.spec.model,
        })

        # Run the LLM loop
        for turn in range(MAX_TURNS):
            if node.state in (AgentState.DEAD, AgentState.TERMINATING, AgentState.SUSPENDED):
                logger.info(f"Agent {node.id} state changed to {node.state.value}, stopping")
                break

            # Check for correction messages
            corrections = [m for m in node.messages if m.get("kind") == "correction"]
            if corrections:
                msg = corrections[-1]
                initial_message = f"[Correction from operator]: {msg['content']}\n\nAdjust your research accordingly."
                node.messages = [m for m in node.messages if m.get("kind") != "correction"]

            message = initial_message if turn == 0 else "[Continue research. Update the dossier with any new findings. Call finish_research() when done.]"

            response = await llm.chat(message)

            # Update health metrics
            node.health.tokens_total = llm.usage.total_tokens
            elapsed = (datetime.now(timezone.utc) - node.created_at).total_seconds()
            if elapsed > 0:
                node.health.token_rate = node.health.tokens_total / (elapsed / 3600)
            node.health.last_check = datetime.now(timezone.utc)

            # Inline budget enforcement
            budget = node.policy.budget
            if budget.max_total_tokens and node.health.tokens_total >= budget.max_total_tokens:
                logger.warning(
                    f"Agent {node.id} exceeded token budget: "
                    f"{node.health.tokens_total} >= {budget.max_total_tokens}"
                )
                break
            if budget.max_runtime_seconds and elapsed > budget.max_runtime_seconds:
                logger.warning(
                    f"Agent {node.id} exceeded runtime budget: "
                    f"{elapsed:.0f}s > {budget.max_runtime_seconds}s"
                )
                break

            # Publish progress
            self.publish_event(node.path, {
                "type": "progress",
                "turn": turn + 1,
                "tokens": llm.usage.total_tokens,
                "response_preview": response[:200] if response else "",
            })

            # Auto-checkpoint
            if (turn + 1) % CHECKPOINT_INTERVAL == 0:
                node.checkpoint()
                node._log_event("auto_checkpoint", {"turn": turn + 1})
                if self.db:
                    await self.db.save_agent(node)
                    dossier = get_dossier(node.id)
                    if dossier.get("last_updated"):
                        await self.db.save_dossier(node.id, dossier)

            if llm._stop_requested:
                logger.info(f"Agent {node.id} finished research")
                break

        # Final checkpoint
        node.checkpoint()
        node._log_event("execution_complete", {
            "turns": turn + 1 if 'turn' in dir() else 0,
            "total_tokens": llm.usage.total_tokens,
        })

        self.publish_event(node.path, {
            "type": "execution_complete",
            "total_tokens": llm.usage.total_tokens,
        })

    def _build_system_prompt(self) -> str:
        """Build the system prompt for the agent."""
        return (
            "You are an autonomous research agent. Your job is to thoroughly research "
            "a person and build a comprehensive dossier.\n\n"
            "You have these tools:\n"
            "- web_search: Search the web (natural language queries work best)\n"
            "- fetch_webpage: Fetch and read a specific webpage for details\n"
            "- read_dossier: Read the current dossier to see what you already know\n"
            "- update_dossier: Add findings to a dossier section\n"
            "- finish_research: Signal that you're done researching\n\n"
            "Dossier sections: basic_info, social_media, career, education, publications, news, other\n\n"
            "Each entry you add to update_dossier should be a JSON array of objects like:\n"
            '[{"title": "...", "url": "...", "description": "..."}]\n\n'
            "CRITICAL WORKFLOW — you MUST follow this pattern:\n"
            "1. Search for the person with web_search\n"
            "2. IMMEDIATELY call update_dossier with findings BEFORE doing the next search\n"
            "3. Repeat: search → update_dossier → search → update_dossier\n"
            "4. NEVER batch all updates for the end — save incrementally after each search\n"
            "5. When you've covered enough ground, call finish_research()\n\n"
            "You have a limited token budget. If you don't save findings incrementally, "
            "they will be LOST when the budget runs out.\n\n"
            "Research areas to cover:\n"
            "- Basic info (name, location, contact)\n"
            "- Social media profiles (LinkedIn, Twitter/X, GitHub, etc.)\n"
            "- Career history and current role\n"
            "- Education\n"
            "- Publications, patents, blog posts\n"
            "- Recent news and mentions\n\n"
            "Deduplicate information — don't add the same finding twice.\n"
            "Be thorough but efficient. Focus on verifiable, factual information."
        )
