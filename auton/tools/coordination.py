"""Agent coordination tools — closure-bound to agent and registry.

These tools let agents spawn children, send messages, and monitor
sub-agents. Factory functions return closures bound to a specific agent.
"""

import json
import logging

logger = logging.getLogger(__name__)

from auton.models import AgentSpec, SpawnRequest
from auton.tools.workspace_tools import _safe_resolve
from auton.workspace import get_workspace_path, list_workspace_files


def make_spawn_child(agent_id: str, agent_path: str, registry, budget_planner=None):
    """Create a spawn_child tool bound to this agent as parent.

    If budget_planner is provided, child token budgets are reserved from
    the parent's budget. The parent cannot allocate more tokens to children
    than it has remaining.
    """

    # Default tools for children when none specified
    _DEFAULT_CHILD_TOOLS = [
        "web_search", "fetch_webpage",
        "write_file", "read_file", "list_files",
        "publish_artifact",
        "check_budget", "finish",
    ]

    def spawn_child(
        child_id: str,
        name: str,
        system_prompt: str,
        goal: str,
        tools: str = "",
        model: str = "openrouter/anthropic/claude-sonnet-4-6",
        max_tokens: int | None = None,
    ) -> str:
        """Spawn a child agent under this agent.

        The child runs autonomously with its own LLM loop. Use list_children
        and check_child_status to monitor progress.

        When max_tokens is specified, that amount is reserved from your
        budget. Use check_budget to see how much you can allocate.

        Args:
            child_id: Unique ID for the child (e.g. "worker-1", "researcher")
            name: Human-readable name for the child
            system_prompt: Full system prompt defining the child's behavior
            goal: The specific objective for the child
            tools: JSON array of tool names, or omit for defaults (web_search, fetch_webpage, write_file, read_file, list_files, check_budget, finish)
            model: LLM model to use (default: claude-sonnet)
            max_tokens: Token budget for the child (reserved from your budget)

        Returns:
            Confirmation with child path and ID, or error
        """
        try:
            if tools and isinstance(tools, str):
                tools_list = json.loads(tools)
            elif isinstance(tools, list) and tools:
                tools_list = tools
            else:
                tools_list = list(_DEFAULT_CHILD_TOOLS)
            if not isinstance(tools_list, list):
                tools_list = list(_DEFAULT_CHILD_TOOLS)
        except (json.JSONDecodeError, TypeError):
            tools_list = list(_DEFAULT_CHILD_TOOLS)

        # Coerce max_tokens to int (LLMs sometimes send it as a string)
        if max_tokens is not None:
            try:
                max_tokens = int(max_tokens)
            except (ValueError, TypeError):
                return json.dumps({"status": "error", "message": f"Invalid max_tokens: {max_tokens}"})

        # Reserve budget from parent if child has a token budget
        if max_tokens and budget_planner:
            try:
                budget_planner.reserve_for_child(max_tokens)
            except ValueError as e:
                return json.dumps({"status": "error", "message": str(e)})

        try:
            spec = AgentSpec(
                name=name,
                system_prompt=system_prompt,
                goal=goal,
                tools=tools_list,
                model=model,
                max_tokens=max_tokens,
            )
        except Exception as e:
            # Roll back reservation on spec error
            if max_tokens and budget_planner:
                budget_planner.release_child_reservation(max_tokens)
            return json.dumps({"status": "error", "message": f"Invalid spec: {e}"})

        req = SpawnRequest(id=child_id, spec=spec)
        try:
            node = registry.spawn(req, parent_path=agent_path)
            # Publish spawn event
            if registry.publish_event:
                registry.publish_event(node.path, {"type": "spawned", "path": node.path})
            result = {
                "status": "OK",
                "child_id": node.id,
                "child_path": node.path,
                "state": node.state.value,
            }
            if max_tokens and budget_planner:
                result["budget_reserved"] = max_tokens
                result["parent_remaining"] = budget_planner.remaining
            return json.dumps(result)
        except Exception as e:
            # Roll back reservation on spawn error
            if max_tokens and budget_planner:
                budget_planner.release_child_reservation(max_tokens)
            return json.dumps({"status": "error", "message": str(e)})

    return spawn_child


def make_message_child(agent_id: str, registry):
    """Create a message_child tool to send messages to any reachable agent."""

    def message_agent(target_path: str, content: str) -> str:
        """Send a message to another agent (child, sibling, or parent).

        If the target agent is idle, this will wake it up to process the message.

        Args:
            target_path: Full path of the target agent (e.g. "coordinator/worker-1")
            content: Message content to deliver

        Returns:
            Confirmation or error
        """
        try:
            registry.send_message(
                target_path, content, channel="agent", kind="message",
                metadata={"from": agent_id},
            )
            return json.dumps({"status": "OK", "target": target_path, "delivered": True})
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})

    return message_agent


def make_list_children(agent_id: str, agent_path: str, registry):
    """Create a list_children tool to see child agents and their states."""

    def list_children() -> str:
        """List all child agents of this agent with their current state.

        Returns:
            JSON with children info including id, path, state, name, goal,
            and workspace file count
        """
        node = registry.resolve(agent_path)
        if node is None:
            return json.dumps({"status": "error", "message": "Parent agent not found"})

        children = []
        for child in node.children.values():
            info = {
                "id": child.id,
                "path": child.path,
                "state": child.state.value,
                "name": child.spec.name,
                "goal": child.spec.goal,
                "tokens_used": child.health.tokens_total,
                "workspace_files": len(list_workspace_files(child.id)),
            }
            if child.error_message:
                info["error"] = child.error_message
            if child.artifacts:
                info["artifact_count"] = len(child.artifacts)
                info["artifacts_published"] = sum(
                    1 for a in child.artifacts if a.status.value == "published"
                )
            children.append(info)

        return json.dumps({"status": "OK", "children": children, "count": len(children)})

    return list_children


def make_check_child_status(agent_id: str, registry):
    """Create a tool that checks a specific child/agent's detailed status."""

    def check_child_status(child_path: str) -> str:
        """Check the detailed status of a child or any other agent.

        Returns the agent's state, token usage, workspace files, and
        the last assistant response if the agent is idle.

        Args:
            child_path: Full path of the agent to check (e.g. "coordinator/worker-1")

        Returns:
            JSON with detailed status information
        """
        node = registry.resolve(child_path)
        if node is None:
            return json.dumps({"status": "error", "message": f"Agent not found: {child_path}"})

        result = {
            "status": "OK",
            "child_path": child_path,
            "state": node.state.value,
            "name": node.spec.name,
            "goal": node.spec.goal,
            "tokens_used": node.health.tokens_total,
            "uptime": node.uptime,
            "workspace_files": len(list_workspace_files(node.id)),
        }

        if node.error_message:
            result["error"] = node.error_message
        if node.idle_reason:
            result["idle_reason"] = node.idle_reason.value

        # Include artifact info
        if node.artifacts:
            published = [a for a in node.artifacts if a.status.value == "published"]
            missing = [a for a in node.artifacts if a.status.value == "missing"]
            expected = [a for a in node.artifacts if a.status.value == "expected"]
            result["artifacts"] = {
                "published": [{"name": a.name, "file_path": a.file_path} for a in published],
                "missing": [a.name for a in missing],
                "pending": [a.name for a in expected],
                "total": len(node.artifacts),
            }

        # Include last assistant response if idle (i.e., finished)
        if node.state.value in ("idle", "suspended"):
            assistant_msgs = [
                m for m in node.conversation
                if m.get("role") == "assistant" and m.get("content")
            ]
            if assistant_msgs:
                last = assistant_msgs[-1]["content"]
                result["last_response"] = last[:1000] if len(last) > 1000 else last

        return json.dumps(result)

    return check_child_status


def make_read_child_file(agent_id: str, registry):
    """Create a tool that reads a file from a child agent's workspace."""

    def read_child_file(child_path: str, file_path: str) -> str:
        """Read a file from a child agent's workspace.

        Use this after a child agent finishes to retrieve its output files
        (e.g. dossier.md, report.md).

        Args:
            child_path: Full path of the child agent (e.g. "coordinator/worker-1")
            file_path: Relative path within the child's workspace (e.g. "dossier.md")

        Returns:
            JSON with file content or error
        """
        node = registry.resolve(child_path)
        if node is None:
            return json.dumps({"status": "error", "message": f"Agent not found: {child_path}"})

        child_ws = get_workspace_path(node.id)
        target = _safe_resolve(child_ws, file_path)
        if target is None:
            return json.dumps({"status": "error", "message": "Path escapes workspace boundary"})
        if not target.exists():
            return json.dumps({"status": "error", "message": f"File not found: {file_path}"})

        try:
            content = target.read_text()
        except UnicodeDecodeError:
            return json.dumps({"status": "error", "message": f"Binary file: {file_path}"})

        if len(content) > 100_000:
            content = content[:100_000] + "\n\n[... truncated at 100K chars ...]"

        return json.dumps({
            "status": "OK",
            "child": child_path,
            "path": file_path,
            "size": len(content),
            "content": content,
        })

    return read_child_file
