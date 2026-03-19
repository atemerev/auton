"""Agent sessions dashboard — full-height sidebar + detail panel (Claude-style layout)."""

import json
from datetime import datetime, timezone
from typing import Optional
from fastapi import Request
from starlette.responses import Response, RedirectResponse
from nicegui import ui, app

from ..sessions import get_user_and_tokens
from ..constants import TIER_FREE, TIER_FOUNDER, TIER_NAMES
from ..style import PRIMARY

STATE_COLORS = {
    "spawning": "#3b82f6",
    "running": "#22c55e",
    "idle": "#94a3b8",
    "correcting": "#f97316",
    "suspended": "#ef4444",
    "terminating": "#ea580c",
    "dead": "#9ca3af",
}

STATE_ICONS = {
    "spawning": "rocket_launch",
    "running": "play_circle",
    "idle": "pause_circle",
    "correcting": "build",
    "suspended": "stop_circle",
    "terminating": "cancel",
    "dead": "remove_circle",
}


def _get_registry():
    from auton.api import registry
    return registry


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _format_size(n: int) -> str:
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _format_log_time(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%H:%M:%S")
    except Exception:
        return ts[:8] if len(ts) >= 8 else ts


def render_agents_dashboard(request: Request) -> Optional[Response]:
    """Full-viewport layout: permanent sidebar (left) + header+detail (right)."""

    # ── Auth guard ──
    user, toks = get_user_and_tokens(request)
    if not user or not toks:
        return RedirectResponse("/login")
    if not user.tos_confirmed and request.url.path != "/welcome":
        return RedirectResponse("/welcome")

    selected_from_url = request.query_params.get("agent")
    current_path = request.url.path

    registry = _get_registry()
    agents = registry.list_all()

    # Load employee profiles from DB and annotate agents
    employee_ids = set()
    employee_profiles = {}
    try:
        from ..db_client import SessionLocal
        from sqlalchemy import text as sa_text
        with SessionLocal() as session:
            result = session.execute(
                sa_text(
                    "SELECT id, profile_json FROM agents "
                    "WHERE profile_json IS NOT NULL AND state NOT IN ('dead', 'terminating')"
                )
            )
            for row in result.fetchall():
                eid = row[0]
                profile = json.loads(row[1]) if isinstance(row[1], str) else row[1]
                employee_ids.add(eid)
                employee_profiles[eid] = profile
    except Exception:
        pass

    # Annotate agents with profile data
    for agent in agents:
        aid = agent.get("id", "")
        if aid in employee_profiles:
            agent["_profile"] = employee_profiles[aid]
            agent["_is_employee"] = True

    # Add employee agents not yet in registry (e.g., just hired)
    registry_ids = {a.get("id") for a in agents}
    for eid, profile in employee_profiles.items():
        if eid not in registry_ids:
            agents.append({
                "id": eid, "path": eid,
                "state": "idle", "spec": profile.get("spec", {}),
                "health": {}, "children": [],
                "_profile": profile, "_is_employee": True,
            })

    # Sort: employees first, then by state
    agents.sort(key=lambda a: (0 if a.get("_is_employee") else 1, a.get("id", "")))

    selected = selected_from_url
    if not selected and agents:
        selected = agents[0].get("path")

    # Set Quasar primary color + suppress default NiceGUI padding/margin
    ui.colors(primary=PRIMARY)
    ui.add_head_html("""
    <style>
        body { margin: 0 !important; padding: 0 !important; overflow: hidden !important; }
        .nicegui-content { padding: 0 !important; margin: 0 !important; gap: 0 !important; }
        .q-page { padding: 0 !important; }
    </style>
    """)

    # ── Full-viewport layout: navbar on top, sidebar+detail below ──
    with ui.column().classes("w-screen gap-0").style("height: 100vh; overflow: hidden"):

        # ════════════════════════════════════════
        # TOP: Full-width navbar
        # ════════════════════════════════════════
        with ui.row().classes(
            "w-full items-center justify-between px-6 bg-white border-b border-slate-200 flex-shrink-0"
        ).style("height: 56px; min-height: 56px"):

            with ui.row().classes("items-center gap-3"):
                with ui.link(target="/").classes("no-underline flex items-center gap-2"):
                    ui.image("/static/dashboard/favicon.svg").classes("w-7 h-7")
                    ui.label("AUTON").classes("text-xl font-bold").style("color: #16203C")

                ui.separator().props("vertical inset").style("height: 24px")

                nav_items = [
                    ("/agents-dashboard", "Agents", "smart_toy"),
                    ("/team", "Team", "badge"),

                    ("/account-settings", "Settings", "settings"),
                ]
                for path, label, icon in nav_items:
                    with ui.link(target=path).classes("no-underline"):
                        btn = ui.button(label, icon=icon).props("flat dense no-caps")
                        if current_path == path:
                            btn.style("border-bottom: 2px solid #1DE0C8; border-radius: 0")

            with ui.row().classes("items-center gap-4"):
                tier_name = TIER_NAMES.get(user.tier, "Free")
                with ui.row().classes("items-center gap-2 text-sm"):
                    ui.label(tier_name).classes("text-slate-600 font-medium")
                    ui.separator().props("vertical inset")
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Credits:").classes("text-slate-600")
                        if user.tier == TIER_FOUNDER:
                            ui.html('<span class="font-bold text-xl" style="color: #1DE0C8;">&infin;</span>')
                        else:
                            ui.label(str(user.credits)).classes("text-slate-600")

                if user.tier == TIER_FREE:
                    with ui.link(target="/account-settings?tab=subscriptions").classes("no-underline"):
                        ui.button("Upgrade", color="primary").props("dense unelevated")

                with ui.link(target="/auth/logout").classes("no-underline"):
                    ui.button("Logout", icon="logout", color="primary").props("flat dense")

        # ════════════════════════════════════════
        # BOTTOM: Sidebar + Detail (fills remaining height)
        # ════════════════════════════════════════
        with ui.row().classes("w-full gap-0 flex-nowrap flex-grow").style("min-height: 0"):

            # ── LEFT: Agent sidebar ──
            with ui.column().classes("gap-0 bg-slate-50 border-r border-slate-200 flex-shrink-0") \
                    .style("width: 280px; min-width: 280px; height: 100%"):

                # Agents label + refresh
                with ui.row().classes("w-full justify-between items-center px-4 py-2 border-b border-slate-100"):
                    ui.label("Agents").classes("text-xs font-semibold text-slate-400 uppercase tracking-wider")
                    ui.button(icon="refresh", on_click=lambda: ui.navigate.reload()) \
                        .props("flat dense round size=xs").classes("text-slate-400")

                # Agent list (scrollable)
                with ui.column().classes("gap-0 overflow-y-auto flex-grow"):
                    if not agents:
                        with ui.column().classes("w-full items-center py-12 px-4"):
                            ui.icon("smart_toy", size="lg").classes("text-slate-300 mb-2")
                            ui.label("No agents").classes("text-slate-400 text-sm")
                    else:
                        for agent in agents:
                            _render_sidebar_item(agent, selected)

                # Bottom section — user info
                with ui.column().classes("px-4 py-3 border-t border-slate-200 flex-shrink-0"):
                    with ui.row().classes("items-center gap-2 text-xs text-slate-500"):
                        ui.icon("account_circle", size="xs")
                        ui.label(user.email).classes("truncate")

            # ── RIGHT: Detail panel ──
            with ui.column().classes("flex-grow gap-0 overflow-y-auto p-6 bg-white").style("min-width: 0"):
                if selected:
                    sel_agent = next((a for a in agents if a.get("path") == selected), None)
                    _render_detail_panel(selected, registry, agent_data=sel_agent)
                else:
                    with ui.column().classes("w-full h-full items-center justify-center"):
                        ui.icon("smart_toy", size="xl").classes("text-slate-200 mb-4")
                        ui.label("Select an agent").classes("text-xl text-slate-400")

    return None


def _render_sidebar_item(agent: dict, selected_path: str, depth: int = 0):
    """Render a single agent in the sidebar list."""
    path = agent.get("path", "")
    state = agent.get("state", "unknown")
    spec = agent.get("spec", {})
    health = agent.get("health", {})
    tokens = health.get("tokens_total", 0)
    color = STATE_COLORS.get(state, "#94a3b8")
    is_selected = path == selected_path
    is_employee = agent.get("_is_employee", False)
    profile = agent.get("_profile", {})

    bg = "bg-teal-50" if is_selected else "hover:bg-slate-100"
    border = "border-l-[3px] border-teal-500" if is_selected else "border-l-[3px] border-transparent"
    pl = 12 + depth * 12

    def select_agent(p=path):
        ui.navigate.to(f"/agents-dashboard?agent={p}")

    with ui.element("div").classes(f"w-full cursor-pointer {bg} {border} py-2.5 pr-2") \
            .style(f"padding-left: {pl}px; box-sizing: border-box") \
            .on("click", select_agent):

        with ui.row().classes("items-center gap-2 w-full flex-nowrap"):
            if is_employee:
                # Employee avatar
                avatar_url = profile.get("avatar_url")
                if avatar_url:
                    ui.image(avatar_url).style(
                        "width: 24px; height: 24px; border-radius: 50%; "
                        "object-fit: cover; flex-shrink: 0"
                    )
                else:
                    emoji = profile.get("avatar_emoji", "🤖")
                    ui.label(emoji).style(
                        "font-size: 14px; width: 24px; height: 24px; "
                        "display: flex; align-items: center; justify-content: center; flex-shrink: 0"
                    )
            else:
                # State dot
                ui.html(f'<div style="width:8px;height:8px;border-radius:50%;background:{color};flex-shrink:0"></div>')
            with ui.column().classes("gap-0 min-w-0 flex-grow overflow-hidden"):
                name = profile.get("name") or spec.get("name", agent.get("id", "?"))
                ui.label(name).classes("text-sm font-medium text-slate-800 truncate")
                with ui.row().classes("items-center gap-1"):
                    if is_employee:
                        ui.html(
                            '<span style="font-size:9px;padding:1px 4px;border-radius:3px;'
                            'background:#e0f2fe;color:#0369a1;font-weight:600">employee</span>'
                        )
                    idle_reason = agent.get("idle_reason")
                    sidebar_label = f"{state} · {_format_tokens(tokens)} tok"
                    if state == "idle" and idle_reason:
                        sidebar_label = f"{idle_reason} · {_format_tokens(tokens)} tok"
                    elif state == "idle" and is_employee:
                        sidebar_label = "available"
                    ui.label(sidebar_label).classes("text-xs text-slate-400 truncate")

    # Render children in sidebar too
    for child in agent.get("children", []):
        _render_sidebar_item(child, selected_path, depth + 1)


def _render_detail_panel(agent_path: str, registry, agent_data: dict | None = None):
    """Render the full detail panel for a selected agent."""
    node = registry.resolve(agent_path)
    is_employee = agent_data.get("_is_employee", False) if agent_data else False
    profile = agent_data.get("_profile", {}) if agent_data else {}

    if node is None and not is_employee:
        ui.label(f"Agent not found: {agent_path}").classes("text-red-500")
        return

    agent = node.to_dict() if node else agent_data or {}
    state = agent.get("state", "unknown")
    spec = agent.get("spec", {})
    health = agent.get("health", {})
    color = STATE_COLORS.get(state, "#94a3b8")

    # ── Header ──
    with ui.row().classes("w-full items-start justify-between mb-4"):
        with ui.row().classes("items-start gap-4"):
            # Employee avatar
            if is_employee:
                avatar_url = profile.get("avatar_url")
                if avatar_url:
                    ui.image(avatar_url).style(
                        "width: 56px; height: 56px; border-radius: 50%; "
                        "object-fit: cover; flex-shrink: 0"
                    )
                else:
                    emoji = profile.get("avatar_emoji", "🤖")
                    ui.label(emoji).style(
                        "font-size: 28px; width: 56px; height: 56px; "
                        "display: flex; align-items: center; justify-content: center; flex-shrink: 0"
                    )

            with ui.column().classes("gap-1"):
                name = profile.get("name") or spec.get("name", agent.get("id", "?"))
                ui.label(name).classes("text-2xl font-bold")
                with ui.row().classes("items-center gap-2"):
                    ui.label(agent.get("path", "")).classes("text-sm text-slate-400 font-mono")
                    ui.badge(state.upper()).style(f"background-color: {color}; color: white")
                    if is_employee:
                        ui.badge("EMPLOYEE").style(
                            "background-color: #e0f2fe; color: #0369a1; font-size: 10px"
                        )
                    if state == "idle" and agent.get("idle_reason"):
                        ui.label(f"({agent['idle_reason']})").classes("text-xs text-slate-500")
                    if agent.get("uptime"):
                        ui.label(f"Up: {agent['uptime']}").classes("text-xs text-slate-400")
                    if agent.get("restart_count", 0) > 0:
                        ui.label(f"Restarts: {agent['restart_count']}").classes("text-xs text-orange-500")
                if is_employee and profile.get("title"):
                    ui.label(profile["title"]).classes("text-sm text-slate-500")

        # Action buttons
        with ui.row().classes("gap-2 flex-shrink-0"):
            if state in ("running", "idle", "correcting"):
                def do_suspend(p=agent_path):
                    try:
                        registry.suspend(p)
                        ui.notify(f"Suspended {p}", type="warning")
                    except Exception as e:
                        ui.notify(str(e), type="negative")
                    ui.navigate.reload()

                def do_terminate(p=agent_path):
                    try:
                        registry.terminate(p)
                        ui.notify(f"Terminated {p}", type="info")
                    except Exception as e:
                        ui.notify(str(e), type="negative")
                    ui.navigate.reload()

                ui.button("Suspend", icon="pause", on_click=do_suspend, color="orange") \
                    .props("flat dense no-caps")
                ui.button("Terminate", icon="stop", on_click=do_terminate, color="red") \
                    .props("flat dense no-caps")

            elif state == "suspended":
                if agent.get("suspend_reason"):
                    ui.label(f"Reason: {agent['suspend_reason']}").classes("text-xs text-red-400 self-center")

                def do_resume(p=agent_path):
                    try:
                        registry.resume(p)
                        ui.notify(f"Resumed {p}", type="positive")
                    except Exception as e:
                        ui.notify(str(e), type="negative")
                    ui.navigate.reload()

                ui.button("Resume", icon="play_arrow", on_click=do_resume, color="green") \
                    .props("flat dense no-caps")

    # ── Employee profile card ──
    if is_employee:
        with ui.card().classes("w-full p-4 mb-4 bg-sky-50 border border-sky-100"):
            with ui.row().classes("w-full items-center justify-between mb-2"):
                ui.label("Employee Profile").classes("text-sm font-semibold text-sky-800")
                with ui.link(target=f"/team?employee={agent.get('id', '')}").classes("no-underline"):
                    ui.button("View in Team", icon="badge", color="primary") \
                        .props("flat dense no-caps size=sm")
            if profile.get("personality"):
                ui.label(profile["personality"]).classes("text-sm text-slate-600 mb-2")
            strengths = profile.get("strengths", [])
            if strengths:
                with ui.row().classes("gap-1 flex-wrap"):
                    for s in strengths:
                        ui.badge(s).props("outline").classes("text-sky-700").style("font-size: 10px")

    # ── Description & Goal ──
    if spec.get("description"):
        ui.label(spec["description"]).classes("text-sm text-slate-600 mb-1")
    if spec.get("goal"):
        with ui.row().classes("items-start gap-1 mb-4"):
            ui.label("Goal:").classes("text-xs font-semibold text-slate-500 flex-shrink-0")
            ui.label(spec["goal"]).classes("text-xs text-slate-500")

    # ── Health ──
    with ui.card().classes("w-full p-4 mb-4"):
        ui.label("Health").classes("text-sm font-semibold text-slate-700 mb-2")
        with ui.row().classes("gap-6 flex-wrap"):
            _metric("Tokens", f"{health.get('tokens_total', 0):,}")
            _metric("Token Rate", f"{_format_tokens(int(health.get('token_rate', 0)))}/hr")
            drift = health.get("drift", 0)
            drift_color = "text-red-600" if drift > 0.3 else "text-orange-500" if drift > 0.1 else "text-slate-700"
            _metric("Drift", f"{drift:.3f}", value_class=drift_color)
            _metric("Coherence", f"{health.get('coherence', 1):.3f}")
            _metric("Loops", str(health.get("loops_detected", 0)))
            _metric("Model", spec.get("model", "?").split("/")[-1])
            if spec.get("max_tokens"):
                _metric("Budget", f"{_format_tokens(spec['max_tokens'])} tok")
            if spec.get("max_runtime_seconds"):
                _metric("Runtime Limit", f"{spec['max_runtime_seconds']}s")

    # ── Children ──
    children = agent.get("children", [])
    with ui.card().classes("w-full p-4 mb-4"):
        ui.label(f"Children ({len(children)})").classes("text-sm font-semibold text-slate-700 mb-2")
        if not children:
            ui.label("No child agents").classes("text-xs text-slate-400")
        else:
            for child in children:
                _render_child_row(child, registry)

    # ── Recent Log ──
    log_entries = node.log[-20:] if node.log else []
    with ui.card().classes("w-full p-4 mb-4"):
        ui.label(f"Recent Log ({len(node.log)} total)").classes("text-sm font-semibold text-slate-700 mb-2")
        if not log_entries:
            ui.label("No log entries").classes("text-xs text-slate-400")
        else:
            with ui.column().classes("w-full gap-0 max-h-72 overflow-y-auto font-mono text-xs"):
                for entry in reversed(log_entries):
                    _render_log_entry(entry)

    # ── Workspace Files ──
    from auton.workspace import list_workspace_files
    uid = node.user_id if node else None
    aid = node.id if node else agent.get("id", "")
    files = list_workspace_files(aid, uid)
    with ui.card().classes("w-full p-4 mb-4"):
        ui.label(f"Workspace Files ({len(files)})").classes("text-sm font-semibold text-slate-700 mb-2")
        if not files:
            ui.label("No files in workspace").classes("text-xs text-slate-400")
        else:
            for f in files:
                _render_workspace_file(f, node.id)

    # ── Spec Details (collapsible) ──
    with ui.expansion("Full Spec", icon="code").classes("w-full mb-4").props("dense"):
        with ui.column().classes("w-full gap-1 font-mono text-xs"):
            for key in ["tools", "temperature", "max_turns", "restart", "max_restarts",
                        "supervision", "shutdown_timeout", "drift_threshold",
                        "schedule", "max_children", "max_depth", "tags"]:
                val = spec.get(key)
                if val is not None:
                    val_str = str(val)
                    if len(val_str) > 120:
                        val_str = val_str[:117] + "..."
                    ui.label(f"{key}: {val_str}").classes("text-slate-500")


def _metric(label: str, value: str, value_class: str = "text-slate-700"):
    """Render a small metric label/value pair."""
    with ui.column().classes("gap-0"):
        ui.label(label).classes("text-xs text-slate-400")
        ui.label(value).classes(f"text-sm font-medium {value_class}")


def _render_child_row(child: dict, registry, depth: int = 0):
    """Render a child agent row with state dot and basic info."""
    state = child.get("state", "unknown")
    spec = child.get("spec", {})
    health = child.get("health", {})
    color = STATE_COLORS.get(state, "#94a3b8")
    tokens = health.get("tokens_total", 0)

    ml = f"ml-{depth * 4}" if depth > 0 else ""
    with ui.row().classes(f"items-center gap-2 py-1 {ml}"):
        ui.html(f'<div style="width:8px;height:8px;border-radius:50%;background:{color};flex-shrink:0"></div>')
        ui.label(spec.get("name", child.get("id", "?"))).classes("text-sm font-medium")
        ui.badge(state, color=state).props("outline dense")
        ui.label(f"{_format_tokens(tokens)} tok").classes("text-xs text-slate-400")
        if child.get("uptime"):
            ui.label(child["uptime"]).classes("text-xs text-slate-400")

    for grandchild in child.get("children", []):
        _render_child_row(grandchild, registry, depth + 1)


def _render_log_entry(entry: dict):
    """Render a single log entry line."""
    ts = _format_log_time(entry.get("timestamp", ""))
    event_type = entry.get("type", "?")

    type_colors = {
        "state_change": "text-blue-600",
        "spawned": "text-green-600",
        "terminated": "text-red-600",
        "checkpoint": "text-purple-600",
        "correction": "text-orange-600",
        "message_received": "text-slate-600",
        "restarted": "text-yellow-600",
    }
    color = type_colors.get(event_type, "text-slate-500")

    # Build detail string
    details = []
    if event_type == "state_change":
        details.append(f"{entry.get('from', '?')} -> {entry.get('to', '?')}")
    elif "guidance" in entry:
        details.append(entry["guidance"][:80])
    elif "reason" in entry:
        details.append(str(entry["reason"]))
    elif "checkpoint_id" in entry:
        details.append(entry["checkpoint_id"])

    detail_str = " ".join(details)
    with ui.row().classes("items-baseline gap-2 py-0.5 border-b border-slate-100"):
        ui.label(ts).classes("text-slate-400 flex-shrink-0").style("width: 60px")
        ui.label(event_type).classes(f"{color} font-medium flex-shrink-0").style("width: 110px")
        if detail_str:
            ui.label(detail_str).classes("text-slate-500 truncate")


def _render_workspace_file(f: dict, agent_id: str):
    """Render a workspace file row with download link."""
    path = f["path"]
    size = _format_size(f["size"])
    download_url = f"/workspace/{agent_id}/{path}"

    # Icon based on extension
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    icon_map = {"md": "description", "txt": "article", "json": "data_object",
                "py": "code", "csv": "table_chart", "html": "web"}
    icon = icon_map.get(ext, "insert_drive_file")

    with ui.row().classes("items-center gap-3 py-1.5 px-2 rounded hover:bg-slate-50 w-full"):
        ui.icon(icon, size="xs").classes("text-slate-400")
        ui.label(path).classes("text-sm font-mono flex-grow")
        ui.label(size).classes("text-xs text-slate-400 flex-shrink-0")
        with ui.link(target=download_url, new_tab=True).classes("no-underline"):
            ui.button(icon="download", on_click=lambda: None).props("flat dense round size=xs")
