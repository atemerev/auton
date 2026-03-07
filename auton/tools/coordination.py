"""Agent coordination tools — closure-bound to agent and registry.

These tools let agents spawn children, send messages, and monitor
sub-agents. Factory functions return closures bound to a specific agent.
"""

import json

from auton.models import AgentSpec, SpawnRequest
from auton.workspace import list_workspace_files


def make_spawn_child(agent_id: str, agent_path: str, registry):
    """Create a spawn_child tool bound to this agent as parent."""

    def spawn_child(
        child_id: str,
        name: str,
        system_prompt: str,
        goal: str,
        tools: str = "[]",
        model: str = "openrouter/anthropic/claude-sonnet-4-6",
    ) -> str:
        """Spawn a child agent under this agent.

        The child runs autonomously with its own LLM loop. Use list_children
        and check_child_status to monitor progress.

        Args:
            child_id: Unique ID for the child (e.g. "worker-1", "researcher")
            name: Human-readable name for the child
            system_prompt: Full system prompt defining the child's behavior
            goal: The specific objective for the child
            tools: JSON array of tool names (e.g. '["shell_exec", "write_file", "read_file"]')
            model: LLM model to use (default: claude-sonnet)

        Returns:
            Confirmation with child path and ID, or error
        """
        try:
            tools_list = json.loads(tools) if isinstance(tools, str) else tools
        except json.JSONDecodeError:
            return json.dumps({"status": "error", "message": "Invalid tools JSON array"})

        try:
            spec = AgentSpec(
                name=name,
                system_prompt=system_prompt,
                goal=goal,
                tools=tools_list,
                model=model,
            )
        except Exception as e:
            return json.dumps({"status": "error", "message": f"Invalid spec: {e}"})

        req = SpawnRequest(id=child_id, spec=spec)
        try:
            node = registry.spawn(req, parent_path=agent_path)
            # Publish spawn event
            if registry.publish_event:
                registry.publish_event(node.path, {"type": "spawned", "path": node.path})
            return json.dumps({
                "status": "OK",
                "child_id": node.id,
                "child_path": node.path,
                "state": node.state.value,
            })
        except Exception as e:
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
