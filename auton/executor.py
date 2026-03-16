"""Agent executor — runs the LLM tool-calling loop for an agent.

Modeled on Lethe's ActorRunner. The executor operates ON the AgentNode
(agents as data) but doesn't subclass it.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from auton.budget import BudgetPlanner
from auton.drift import DRIFT_CHECK_INTERVAL, judge_drift
from auton.llm import LLMClient
from auton.models import AgentNode, AgentState, ArtifactRecord, ArtifactStatus, IdleReason, SuspendReason
from auton.tools import TOOL_REGISTRY, function_to_schema
from auton.tools.workspace_tools import (
    make_list_files,
    make_pass_artifact,
    make_publish_artifact,
    make_read_file,
    make_shell_exec,
    make_write_file,
)
from auton.tools.coordination import (
    make_check_child_status,
    make_list_children,
    make_message_child,
    make_read_child_file,
    make_spawn_child,
)
from auton.workspace import ensure_workspace

logger = logging.getLogger(__name__)

CHECKPOINT_INTERVAL = 5

# Budget thresholds for escalating warnings
BUDGET_WARN_PCT = 70      # Start mentioning budget
BUDGET_URGENT_PCT = 85    # Urgent: demand deliverables
BUDGET_FINAL_PCT = 95     # Final turn: produce output NOW
BUDGET_HARD_STOP_PCT = 115  # LLM client hard-stop (safety valve — planner handles graceful stop)

# All closure-bound tool names — these skip the global TOOL_REGISTRY lookup
CLOSURE_TOOLS = {
    # Workspace file tools
    "write_file", "read_file", "list_files",
    # Shell tool
    "shell_exec",
    # Artifact sharing
    "pass_artifact", "publish_artifact",
    # Agent coordination
    "spawn_child", "message_agent", "list_children", "check_child_status", "read_child_file",
    # Control
    "finish", "check_budget",
}

# Tools that require a workspace directory
WORKSPACE_TOOLS = {"write_file", "read_file", "list_files", "shell_exec", "pass_artifact", "publish_artifact"}

# Tools available during write gate (save + informational — everything else is gated)
SAVE_GATE_TOOLS = {"write_file", "read_file", "list_files", "finish", "check_budget"}


# ---------------------------------------------------------------------------
# Generic tools (closure-bound to agent state)
# ---------------------------------------------------------------------------


def make_check_budget(node: AgentNode, planner: BudgetPlanner | None = None):
    """Create a check_budget tool bound to this agent's node."""

    def check_budget() -> str:
        """Check remaining token budget and runtime. Call this to plan your work
        and know when to start producing final deliverables."""
        tokens_used = node.health.tokens_total
        max_tokens = node.spec.max_tokens
        elapsed = (datetime.now(timezone.utc) - node.created_at).total_seconds()
        max_runtime = node.spec.max_runtime_seconds

        result = {
            "tokens_used": tokens_used,
            "max_tokens": max_tokens,
            "tokens_remaining": (max_tokens - tokens_used) if max_tokens else None,
            "budget_pct": round(tokens_used / max_tokens * 100, 1) if max_tokens else None,
            "elapsed_seconds": round(elapsed),
            "max_runtime_seconds": max_runtime,
        }

        # Add planner estimates if available
        if planner:
            snap = planner.snapshot()
            result["estimated_next_cost"] = snap["estimated_next_cost"]
            result["estimated_calls_remaining"] = snap["estimated_calls_remaining"]
            result["finalize_imminent"] = snap["finalize_triggered"]
            result["reserved_for_children"] = snap["reserved_for_children"]
            result["available_for_children"] = snap["available_for_children"]

        # Add actionable advice
        if max_tokens:
            pct = tokens_used / max_tokens * 100
            if pct >= BUDGET_FINAL_PCT:
                result["advice"] = (
                    "CRITICAL: Budget nearly exhausted. "
                    "Produce final deliverables NOW — write output files and finalize results immediately."
                )
            elif pct >= BUDGET_URGENT_PCT:
                result["advice"] = (
                    "WARNING: Budget running low. "
                    "Stop new research. Start producing deliverables and writing output files."
                )
            elif pct >= BUDGET_WARN_PCT:
                result["advice"] = (
                    "Budget past midpoint. Plan remaining work carefully. "
                    "Prioritize deliverables over additional research."
                )
            else:
                result["advice"] = "Budget is healthy. Continue working."

        return json.dumps(result)

    return check_budget


def make_finish(stop_callback):
    """Create a generic finish tool that signals task completion."""

    def finish(summary: str) -> str:
        """Signal that your task is complete.

        Call this when you have completed your goal or gathered sufficient information.
        Make sure you have written all output files before calling this.

        Args:
            summary: Brief summary of what was accomplished

        Returns:
            Confirmation
        """
        stop_callback()
        return json.dumps({"status": "OK", "message": "Task complete."})

    return finish


class AgentExecutor:
    """Executes an agent's LLM loop as an asyncio.Task."""

    def __init__(self, node: AgentNode, publish_event, db=None, registry=None):
        self.node = node
        self.publish_event = publish_event
        self.db = db
        self.registry = registry
        self._task: asyncio.Task | None = None
        self._idle_reason: IdleReason | None = None

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
                    # Check expected artifact completeness before going IDLE
                    await self._check_artifact_completeness()
                    self.node.idle_reason = self._idle_reason
                    self.node.transition(AgentState.IDLE)
            if self.db:
                await self.db.save_agent(self.node)

    def _get_budget_pct(self) -> float | None:
        """Return current token budget usage as a percentage, or None if unlimited."""
        node = self.node
        if not node.spec.max_tokens:
            return None
        return (node.health.tokens_total / node.spec.max_tokens) * 100

    def _build_continuation_message(self) -> str:
        """Build a budget-aware continuation message with escalating urgency."""
        budget_pct = self._get_budget_pct()

        if budget_pct is None:
            return "[Continue working toward your goal. Respond with your findings when done.]"

        if budget_pct >= BUDGET_FINAL_PCT:
            return (
                f"[⚠️ FINAL TURN — {budget_pct:.0f}% of your token budget is used. "
                "You MUST produce your final deliverables NOW. "
                "Write any output files, finalize your report, and complete your goal immediately. "
                "This is your LAST CHANCE to produce output before the budget runs out.]"
            )
        elif budget_pct >= BUDGET_URGENT_PCT:
            return (
                f"[URGENT: {budget_pct:.0f}% of your token budget is consumed. "
                "You are running low on budget. Start producing final deliverables NOW. "
                "Write output files and finalize your results. "
                "Do not start new research — focus on completing your goal with what you have.]"
            )
        elif budget_pct >= BUDGET_WARN_PCT:
            return (
                f"[Budget notice: {budget_pct:.0f}% of your token budget is used. "
                "Begin wrapping up your work. Prioritize producing deliverables "
                "(writing files, finalizing reports) over additional research. "
                "Continue working toward your goal.]"
            )
        else:
            return "[Continue working toward your goal. Respond with your findings when done.]"

    def _build_initial_message(self) -> str:
        """Build the initial goal message with budget awareness."""
        node = self.node
        msg = node.spec.goal

        if node.spec.max_tokens:
            msg += (
                f"\n\n[Budget: You have a budget of {node.spec.max_tokens:,} tokens for this task. "
                "Use check_budget to monitor your usage. "
                "When your budget runs low, prioritize producing deliverables "
                "(writing output files, finalizing reports) over additional research. "
                "Do NOT leave deliverables for the end — save incrementally.]"
            )

        if node.spec.expected_artifacts:
            names = [f"{a.name} ({a.file_path})" for a in node.spec.expected_artifacts]
            msg += (
                f"\n\n[Expected deliverables: {', '.join(names)}. "
                "Write each file to your workspace and call publish_artifact to register it.]"
            )

        return msg

    async def _check_artifact_completeness(self) -> None:
        """Check expected artifacts: auto-promote if file exists, mark missing otherwise."""
        from auton.workspace import get_workspace_path
        node = self.node
        if not node.artifacts:
            return
        for artifact in node.artifacts:
            if artifact.status != ArtifactStatus.EXPECTED:
                continue
            ws = get_workspace_path(node.id)
            target = ws / artifact.file_path
            if target.exists() and target.is_file():
                artifact.status = ArtifactStatus.PUBLISHED
                artifact.file_size = target.stat().st_size
                artifact.updated_at = datetime.now(timezone.utc)
            else:
                artifact.status = ArtifactStatus.MISSING
                artifact.updated_at = datetime.now(timezone.utc)
            if self.db:
                await self.db.save_artifact(artifact.model_dump())
        missing = [a for a in node.artifacts if a.status == ArtifactStatus.MISSING]
        if missing:
            names = [a.name for a in missing]
            node._log_event("missing_artifacts", {"names": names})
            self.publish_event(node.path, {
                "type": "missing_artifacts",
                "artifacts": names,
            })

    async def run(self) -> None:
        """Main execution loop with budget-aware steering."""
        node = self.node
        logger.info(f"Starting execution for agent {node.id}: {node.spec.goal}")

        # Ensure workspace if agent uses any workspace tools
        requested_tools = set(node.spec.tools)
        if requested_tools & WORKSPACE_TOOLS:
            ensure_workspace(node.id)

        # System prompt comes directly from the spec — no implicit defaults
        system_prompt = node.spec.system_prompt

        # Create budget planner if agent has a token budget
        planner = None
        if node.spec.max_tokens:
            planner = BudgetPlanner(max_budget=node.spec.max_tokens)

        # Budget check callback — called after every API call inside LLM client.
        # Safety valve at BUDGET_HARD_STOP_PCT (115%) — planner handles graceful stop.
        def _check_budget() -> bool:
            # Update health metrics on every API call
            node.health.tokens_total = llm.usage.total_tokens
            elapsed = (datetime.now(timezone.utc) - node.created_at).total_seconds()
            if elapsed > 0:
                node.health.token_rate = node.health.tokens_total / (elapsed / 3600)
            node.health.last_check = datetime.now(timezone.utc)

            # Hard stop at BUDGET_HARD_STOP_PCT (115%) — safety valve
            if node.spec.max_tokens:
                hard_limit = node.spec.max_tokens * BUDGET_HARD_STOP_PCT / 100
                if llm.usage.total_tokens >= hard_limit:
                    logger.warning(
                        f"Agent {node.id} hard token limit exceeded: "
                        f"{llm.usage.total_tokens} >= {hard_limit:.0f}"
                    )
                    return True
            if node.spec.max_runtime_seconds and node.started_at:
                runtime = (datetime.now(timezone.utc) - node.started_at).total_seconds()
                if runtime > node.spec.max_runtime_seconds:
                    logger.warning(f"Agent {node.id} runtime budget exceeded: {runtime:.0f}s")
                    return True
            return False

        # Event callback — forward granular LLM events to SSE observers
        def _on_llm_event(event: dict) -> None:
            self.publish_event(node.path, event)

        # Write gate: auto-enabled for agents with a budget and write_file tool.
        # When active, only output_tools are available until the agent saves.
        output_tools: set[str] = set()
        if planner and "write_file" in requested_tools:
            output_tools = requested_tools & SAVE_GATE_TOOLS

        # Create LLM client — agents with write_file need higher output limits
        # to write complete files in tool call arguments
        max_out = 8192 if "write_file" in requested_tools else 4096
        llm = LLMClient(
            model=node.spec.model,
            system_prompt=system_prompt,
            max_output_tokens=max_out,
            budget_check=_check_budget,
            on_event=_on_llm_event,
            budget_planner=planner,
            output_tools=output_tools,
        )

        # ------------------------------------------------------------------
        # Register tools from spec
        # ------------------------------------------------------------------

        # Global tools (from TOOL_REGISTRY)
        for tool_name in node.spec.tools:
            if tool_name in CLOSURE_TOOLS:
                continue  # handled below with closures
            if tool_name in TOOL_REGISTRY:
                func, schema = TOOL_REGISTRY[tool_name]
                llm.add_tool(func, schema)

        # Workspace file tools (closure-bound to agent_id)
        if "write_file" in requested_tools:
            fn = make_write_file(node.id)
            llm.add_tool(fn, function_to_schema(fn))
        if "read_file" in requested_tools:
            fn = make_read_file(node.id)
            llm.add_tool(fn, function_to_schema(fn))
        if "list_files" in requested_tools:
            fn = make_list_files(node.id)
            llm.add_tool(fn, function_to_schema(fn))

        # Shell tool (closure-bound to agent_id)
        if "shell_exec" in requested_tools:
            fn = make_shell_exec(node.id)
            llm.add_tool(fn, function_to_schema(fn))

        # Artifact sharing (needs registry for cross-agent access)
        if "pass_artifact" in requested_tools and self.registry:
            fn = make_pass_artifact(node.id, self.registry)
            llm.add_tool(fn, function_to_schema(fn))

        # Artifact publishing (registers workspace files with metadata)
        if "publish_artifact" in requested_tools and self.registry:
            fn = make_publish_artifact(node.id, node.path, self.registry)
            llm.add_tool(fn, function_to_schema(fn))

        # Agent coordination tools (need registry)
        if "spawn_child" in requested_tools and self.registry:
            fn = make_spawn_child(node.id, node.path, self.registry, budget_planner=planner)
            llm.add_tool(fn, function_to_schema(fn))
        if "message_agent" in requested_tools and self.registry:
            fn = make_message_child(node.id, self.registry)
            llm.add_tool(fn, function_to_schema(fn))
        if "list_children" in requested_tools and self.registry:
            fn = make_list_children(node.id, node.path, self.registry)
            llm.add_tool(fn, function_to_schema(fn))
        if "check_child_status" in requested_tools and self.registry:
            fn = make_check_child_status(node.id, self.registry)
            llm.add_tool(fn, function_to_schema(fn))
        if "read_child_file" in requested_tools and self.registry:
            fn = make_read_child_file(node.id, self.registry)
            llm.add_tool(fn, function_to_schema(fn))

        # Generic finish tool — any agent can signal completion
        if "finish" in requested_tools:
            fn = make_finish(lambda: setattr(llm, '_stop_requested', True))
            llm.add_tool(fn, function_to_schema(fn))

        # Budget awareness tool — auto-registered when agent has a token budget
        if node.spec.max_tokens or "check_budget" in requested_tools:
            fn = make_check_budget(node, planner=planner)
            llm.add_tool(fn, function_to_schema(fn))

        logger.info(f"Registered tools for {node.id}: {list(llm._tools.keys())}")

        # Seed expected artifacts from spec
        if node.spec.expected_artifacts and not node.artifacts:
            for aspec in node.spec.expected_artifacts:
                record = ArtifactRecord(
                    name=aspec.name,
                    file_path=aspec.file_path,
                    agent_id=node.id,
                    agent_path=node.path,
                    mime_type=aspec.mime_type,
                    description=aspec.description,
                    tags=aspec.tags,
                    status=ArtifactStatus.EXPECTED,
                )
                node.artifacts.append(record)
                if self.db:
                    await self.db.save_artifact(record.model_dump())

        # Build initial message — goal with budget context
        initial_message = self._build_initial_message()

        # Publish start event
        self.publish_event(node.path, {
            "type": "execution_started",
            "goal": node.spec.goal,
            "model": node.spec.model,
        })

        # Run the LLM loop with budget-aware steering
        for turn in range(node.spec.max_turns):
            if node.state in (AgentState.DEAD, AgentState.TERMINATING, AgentState.SUSPENDED):
                logger.info(f"Agent {node.id} state changed to {node.state.value}, stopping")
                break

            # Check for correction messages
            corrections = [m for m in node.messages if m.get("kind") == "correction"]
            if corrections:
                msg = corrections[-1]
                initial_message = f"[Correction from operator]: {msg['content']}\n\nAdjust your work accordingly."
                node.messages = [m for m in node.messages if m.get("kind") != "correction"]

            message = initial_message if turn == 0 else self._build_continuation_message()

            response = await llm.chat(message)

            # Sync conversation to node (bounded)
            node.conversation = llm.messages[-200:]

            # Update health metrics
            node.health.tokens_total = llm.usage.total_tokens
            elapsed = (datetime.now(timezone.utc) - node.created_at).total_seconds()
            if elapsed > 0:
                node.health.token_rate = node.health.tokens_total / (elapsed / 3600)
            node.health.last_check = datetime.now(timezone.utc)

            # Drift detection via LLM judge (every N turns to limit cost)
            if (turn + 1) % DRIFT_CHECK_INTERVAL == 0 and llm.messages:
                try:
                    coherence, drift, assessment = await judge_drift(
                        node.spec.goal, llm.messages,
                        prev_coherence=node.health.coherence,
                    )
                    node.health.coherence = coherence
                    node.health.drift = drift
                    logger.info(
                        f"Agent {node.id} drift: coherence={coherence:.3f}, "
                        f"drift={drift:.3f} — {assessment}"
                    )
                    self.publish_event(node.path, {
                        "type": "drift_check",
                        "coherence": coherence,
                        "drift": drift,
                        "assessment": assessment,
                    })
                except Exception as e:
                    logger.warning(f"Drift check failed for {node.id}: {e}")

            budget_pct = self._get_budget_pct()

            # Budget enforcement — if budget >= 95%, give the agent one final
            # chance to save its work before stopping.
            if budget_pct is not None and budget_pct >= BUDGET_FINAL_PCT:
                logger.warning(
                    f"Agent {node.id} budget at {budget_pct:.0f}% — final save. "
                    f"({node.health.tokens_total:,} tokens)"
                )
                # Direct final-save API call with trimmed context.
                # Uses a separate acompletion call to avoid chat() loop issues.
                try:
                    from litellm import acompletion as _acompletion

                    # Extract last few assistant messages as a summary of work done
                    assistant_msgs = [
                        m["content"][:500] for m in llm.messages
                        if m.get("role") == "assistant" and m.get("content")
                    ]
                    work_summary = "\n".join(assistant_msgs[-3:]) if assistant_msgs else "(no findings yet)"

                    # If expected artifacts are declared, mention them in the save prompt
                    expected_files = []
                    if node.spec.expected_artifacts:
                        expected_files = [
                            f"{a.name} ({a.file_path})" for a in node.spec.expected_artifacts
                            if not any(r.file_path == a.file_path and r.status == ArtifactStatus.PUBLISHED
                                       for r in node.artifacts)
                        ]
                    deliverable_hint = (
                        f"Expected deliverables still missing: {', '.join(expected_files)}. "
                        if expected_files else ""
                    )

                    save_messages = [
                        {"role": "system", "content": (
                            "You are a research agent. Your budget is exhausted. "
                            "You MUST call the write_file tool to save your findings to dossier.md. "
                            "Write a comprehensive dossier based on what you found."
                        )},
                        {"role": "user", "content": (
                            f"Your original goal: {node.spec.goal}\n\n"
                            f"Your recent findings:\n{work_summary}\n\n"
                            f"{deliverable_hint}"
                            "WRITE ALL YOUR FINDINGS to dossier.md using the write_file tool NOW."
                        )},
                    ]

                    # Only offer write_file tool
                    write_file_schema = None
                    for name, (_, schema) in llm._tools.items():
                        if name == "write_file":
                            write_file_schema = {"type": "function", "function": schema}
                            break

                    if write_file_schema:
                        save_resp = await _acompletion(
                            model=node.spec.model,
                            messages=save_messages,
                            tools=[write_file_schema],
                            max_tokens=16384,
                            temperature=0.3,
                        )
                        save_msg = save_resp.choices[0].message
                        if save_msg.tool_calls:
                            for tc in save_msg.tool_calls:
                                if tc.function.name == "write_file":
                                    try:
                                        args = json.loads(tc.function.arguments)
                                    except json.JSONDecodeError:
                                        from auton.llm import _repair_json_args
                                        args = _repair_json_args(tc.function.arguments)
                                    if args:
                                        handler = llm._tools.get("write_file", (None,))[0]
                                        if handler:
                                            result = handler(**args)
                                            logger.warning(f"Agent {node.id} final save wrote: {args.get('path', '?')}")
                        else:
                            logger.warning(f"Agent {node.id} final save: LLM responded with text, no write_file call")
                    else:
                        logger.warning(f"Agent {node.id} final save: no write_file tool available")
                except Exception as e:
                    logger.warning(f"Agent {node.id} final save failed: {e}")
                self._idle_reason = IdleReason.BUDGET_EXHAUSTED
                break

            # Runtime budget check
            if node.spec.max_runtime_seconds and elapsed > node.spec.max_runtime_seconds:
                logger.warning(
                    f"Agent {node.id} exceeded runtime budget: "
                    f"{elapsed:.0f}s > {node.spec.max_runtime_seconds}s"
                )
                self._idle_reason = IdleReason.TIME_EXHAUSTED
                break

            # Publish progress with budget info
            self.publish_event(node.path, {
                "type": "progress",
                "turn": turn + 1,
                "tokens": llm.usage.total_tokens,
                "budget_pct": round(budget_pct, 1) if budget_pct is not None else None,
                "response_preview": response[:200] if response else "",
            })

            # Auto-checkpoint
            if (turn + 1) % CHECKPOINT_INTERVAL == 0:
                node.checkpoint()
                node._log_event("auto_checkpoint", {"turn": turn + 1})
                if self.db:
                    await self.db.save_agent(node)

            # Stop requested (by finish tool, budget planner, etc.)
            if llm._stop_requested:
                logger.info(f"Agent {node.id} finished (stop requested)")
                self._idle_reason = IdleReason.COMPLETED
                break
        else:
            # Loop completed without break — all turns consumed
            self._idle_reason = IdleReason.TURNS_EXHAUSTED

        # Final checkpoint
        turns_completed = turn + 1 if node.spec.max_turns > 0 else 0
        node.checkpoint()
        node._log_event("execution_complete", {
            "turns": turns_completed,
            "total_tokens": llm.usage.total_tokens,
        })

        self.publish_event(node.path, {
            "type": "execution_complete",
            "total_tokens": llm.usage.total_tokens,
        })
