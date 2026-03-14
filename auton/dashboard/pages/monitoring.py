"""Real-time monitoring page — oversight checks and agent event stream."""

from typing import Optional
from fastapi import Request
from starlette.responses import Response
from nicegui import ui

from ..layouts.default import render as default_layout


def _get_registry():
    from auton.api import registry
    return registry


def _get_oversight():
    from auton.api import oversight
    return oversight


def render_monitoring(request: Request) -> Optional[Response]:

    def content():
        registry = _get_registry()
        oversight_engine = _get_oversight()

        with ui.column().classes("w-full max-w-6xl mx-auto p-4 sm:p-8"):
            ui.label("Monitoring").classes("text-2xl font-bold mb-6")

            with ui.row().classes("w-full gap-6"):
                # Left: oversight + system stats
                with ui.column().classes("w-1/3 gap-4"):
                    with ui.card().classes("w-full p-4"):
                        ui.label("Oversight").classes("text-lg font-semibold mb-3")
                        oversight_log = ui.column().classes("w-full gap-1 max-h-96 overflow-y-auto")

                        def run_oversight():
                            events = oversight_engine.check_all()
                            oversight_log.clear()
                            with oversight_log:
                                if not events:
                                    ui.label("All clear").classes("text-green-600 text-sm")
                                else:
                                    for event in events:
                                        _render_oversight_event(event)

                        ui.button("Run Checks", icon="health_and_safety", on_click=run_oversight) \
                            .props("flat dense color=primary no-caps").classes("mb-2")

                    # System stats
                    with ui.card().classes("w-full p-4"):
                        ui.label("System").classes("text-lg font-semibold mb-3")
                        agent_count = len(registry.list_all())
                        ui.label(f"Status: ok").classes("text-sm text-green-600 font-medium")
                        ui.label(f"Active agents: {agent_count}").classes("text-sm text-slate-600")

                        # Count by state
                        all_agents = registry.list_all()
                        state_counts = {}
                        for a in all_agents:
                            s = a.get("state", "unknown")
                            state_counts[s] = state_counts.get(s, 0) + 1
                        if state_counts:
                            ui.separator().classes("my-2")
                            for state, count in sorted(state_counts.items()):
                                ui.label(f"  {state}: {count}").classes("text-xs text-slate-500 font-mono")

                # Right: agent details
                with ui.column().classes("w-2/3 gap-4"):
                    with ui.card().classes("w-full p-4"):
                        ui.label("Agent Status").classes("text-lg font-semibold mb-3")

                        agents = registry.list_all()
                        if not agents:
                            ui.label("No agents to monitor").classes("text-slate-400 text-sm")
                        else:
                            for agent in agents:
                                _render_agent_status(agent)

    return default_layout(content, request)


def _render_agent_status(agent: dict, depth: int = 0):
    """Render agent status line with health details."""
    path = agent.get("path", "?")
    state = agent.get("state", "?")
    spec = agent.get("spec", {})
    health = agent.get("health", {})

    state_colors = {
        "running": "text-green-600",
        "idle": "text-slate-500",
        "suspended": "text-red-500",
        "correcting": "text-orange-500",
        "spawning": "text-blue-500",
        "dead": "text-slate-400",
    }
    color = state_colors.get(state, "text-slate-500")

    prefix = "  " * depth
    with ui.row().classes("items-center gap-2 py-1"):
        ui.label(f"{prefix}{path}").classes("text-sm font-mono font-medium")
        ui.badge(state, color=state.replace("-", "")).props("outline dense")

    with ui.row().classes("gap-4 ml-4 mb-2"):
        tokens = health.get("tokens_total", 0)
        ui.label(f"Tokens: {tokens:,}").classes("text-xs text-slate-500")

        drift = health.get("drift", 0)
        drift_cls = "text-red-500" if drift > 0.3 else "text-orange-500" if drift > 0.1 else "text-slate-500"
        ui.label(f"Drift: {drift:.2f}").classes(f"text-xs {drift_cls}")

        model = spec.get("model", "").split("/")[-1]
        ui.label(model).classes("text-xs text-slate-400 font-mono")

        uptime = agent.get("uptime", "")
        if uptime:
            ui.label(f"Up: {uptime}").classes("text-xs text-slate-400")

    for child in agent.get("children", []):
        _render_agent_status(child, depth + 1)


def _render_oversight_event(event):
    """Render a single oversight event."""
    kind = event.kind if hasattr(event, "kind") else event.get("kind", "unknown")
    path = event.path if hasattr(event, "path") else event.get("path", "?")
    detail = event.detail if hasattr(event, "detail") else event.get("detail", "")
    action = event.action if hasattr(event, "action") else event.get("action", "")

    kind_colors = {
        "budget": "text-orange-600",
        "drift": "text-red-600",
        "loop": "text-yellow-600",
        "health": "text-blue-600",
    }
    color = kind_colors.get(kind, "text-slate-600")

    with ui.row().classes("items-start gap-2 py-1"):
        ui.icon("warning", size="xs").classes(color)
        with ui.column().classes("gap-0"):
            ui.label(f"{kind.upper()}: {path}").classes(f"text-sm font-medium {color}")
            if detail:
                detail_str = detail[:200] if isinstance(detail, str) else str(detail)[:200]
                ui.label(detail_str).classes("text-xs text-slate-500")
            if action:
                ui.label(f"Action: {action}").classes("text-xs text-slate-400 italic")
