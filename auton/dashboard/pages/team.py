"""Team dashboard — hire persistent agent employees via a vacancy/hiring flow."""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import Request
from starlette.responses import Response, RedirectResponse
from nicegui import ui

from litellm import acompletion

from ..sessions import get_user_and_tokens
from ..constants import TIER_FREE, TIER_FOUNDER, TIER_NAMES
from ..style import PRIMARY

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool bundles — friendly names mapping to real tool names
# ---------------------------------------------------------------------------

TOOL_BUNDLES = {
    "web_research": {
        "label": "Web Research",
        "icon": "travel_explore",
        "description": "Search the web and fetch pages",
        "tools": ["web_search", "fetch_webpage"],
    },
    "code": {
        "label": "Code & Shell",
        "icon": "terminal",
        "description": "Write code, execute shell commands",
        "tools": ["write_file", "read_file", "list_files", "shell_exec"],
    },
    "files": {
        "label": "File Management",
        "icon": "folder",
        "description": "Read and write workspace files",
        "tools": ["write_file", "read_file", "list_files"],
    },
    "coordination": {
        "label": "Team Coordination",
        "icon": "groups",
        "description": "Spawn and manage sub-agents",
        "tools": ["spawn_child", "message_agent", "list_children", "check_child_status"],
    },
}

STATUS_COLORS = {
    "active": "#22c55e",
    "suspended": "#f97316",
    "archived": "#9ca3af",
}

# ---------------------------------------------------------------------------
# Candidate generation prompt
# ---------------------------------------------------------------------------

CANDIDATE_PROMPT = """\
You are a creative HR assistant for an AI agent platform. The user has written a position description for a persistent AI employee. Generate exactly 3 candidate profiles that could fill this role. Each candidate should have a distinct personality and approach.

Position description:
{vacancy}

Respond with ONLY a JSON array of 3 objects, each with these fields:
- "name": a realistic full name (diverse backgrounds)
- "title": a concise job title (3-5 words)
- "personality": 1-2 sentences describing their work style and personality
- "strengths": array of 3-4 short strength keywords
- "avatar_emoji": a single emoji that represents their role
- "system_prompt": a detailed system prompt for the AI agent (2-3 paragraphs, written in second person: "You are...")
- "suggested_bundles": array of tool bundle keys from: {bundles}

Return ONLY valid JSON, no markdown fences or explanation."""


def _bundles_description() -> str:
    return ", ".join(f'"{k}" ({v["label"]})' for k, v in TOOL_BUNDLES.items())


async def _generate_candidates(vacancy: str) -> list[dict]:
    """Call LLM to generate 3 candidate profiles from a vacancy description."""
    prompt = CANDIDATE_PROMPT.format(
        vacancy=vacancy,
        bundles=_bundles_description(),
    )
    try:
        response = await acompletion(
            model="openrouter/anthropic/claude-sonnet-4-6",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,
            max_tokens=4000,
        )
        raw = response.choices[0].message.content.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        candidates = json.loads(raw)
        if isinstance(candidates, list) and len(candidates) >= 1:
            return candidates[:3]
    except Exception as e:
        logger.error(f"Candidate generation failed: {e}")
    return []


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_db():
    registry = _get_registry()
    return registry.db if registry else None


def _get_registry():
    from auton.api import registry
    return registry


def _bundles_to_tools(bundle_keys: list[str]) -> list[str]:
    """Expand bundle keys into a flat, deduplicated list of tool names."""
    tools = []
    seen = set()
    for key in bundle_keys:
        bundle = TOOL_BUNDLES.get(key)
        if bundle:
            for t in bundle["tools"]:
                if t not in seen:
                    tools.append(t)
                    seen.add(t)
    return tools


# ---------------------------------------------------------------------------
# Page renderer
# ---------------------------------------------------------------------------

def render_team_dashboard(request: Request) -> Optional[Response]:
    """Full-viewport layout: employee sidebar + detail panel."""

    # Auth guard
    user, toks = get_user_and_tokens(request)
    if not user or not toks:
        return RedirectResponse("/login")
    if not user.tos_confirmed and request.url.path != "/welcome":
        return RedirectResponse("/welcome")

    selected_id = request.query_params.get("employee")
    current_path = request.url.path

    # Load employees synchronously via run_until_complete on the existing loop
    db = _get_db()
    employees: list[dict] = []

    async def _load():
        nonlocal employees
        if db:
            employees = await db.load_employees()

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # NiceGUI runs inside an async context; use background task approach
            import concurrent.futures
            fut = asyncio.ensure_future(_load())
            # We can't block here, so we'll load via a synchronous fallback
        else:
            loop.run_until_complete(_load())
    except Exception:
        pass

    # Try a synchronous DB read as fallback
    if not employees and db and db._conn:
        try:
            import sqlite3
            sync_conn = sqlite3.connect(db.path)
            cursor = sync_conn.execute(
                "SELECT profile_json FROM employees WHERE status != 'archived' ORDER BY created_at DESC"
            )
            employees = [json.loads(row[0]) for row in cursor.fetchall()]
            sync_conn.close()
        except Exception as e:
            logger.debug(f"Sync employee load fallback: {e}")

    if not selected_id and employees:
        selected_id = employees[0].get("id")

    # Styling
    ui.colors(primary=PRIMARY)
    ui.add_head_html("""
    <style>
        body { margin: 0 !important; padding: 0 !important; }
        .nicegui-content { padding: 0 !important; margin: 0 !important; gap: 0 !important; overflow: hidden; }
        .q-page { padding: 0 !important; }
    </style>
    """)

    # Build the vacancy dialog (must be in page context, before layout references it)
    vacancy_dialog = _build_vacancy_dialog()

    with ui.row().classes("w-screen gap-0 flex-nowrap").style("height: 100vh; overflow: hidden"):

        # ════════════════════════════════
        # LEFT SIDEBAR
        # ════════════════════════════════
        with ui.column().classes("gap-0 bg-slate-50 border-r border-slate-200 flex-shrink-0") \
                .style("width: 280px; min-width: 280px; height: 100vh"):

            # Branding
            with ui.row().classes("items-center gap-3 px-5 py-4 border-b border-slate-200"):
                with ui.link(target="/").classes("no-underline flex items-center gap-2"):
                    ui.image("/static/dashboard/favicon.svg").classes("w-7 h-7")
                    ui.label("AUTON").classes("text-xl font-bold").style("color: #16203C")

            # Team label + hire button
            with ui.row().classes("w-full justify-between items-center px-4 py-2 border-b border-slate-100"):
                ui.label("Team").classes("text-xs font-semibold text-slate-400 uppercase tracking-wider")
                with ui.row().classes("gap-1"):
                    ui.button(icon="refresh", on_click=lambda: ui.navigate.reload()) \
                        .props("flat dense round size=xs").classes("text-slate-400")
                    ui.button(icon="person_add", on_click=lambda: vacancy_dialog.open()) \
                        .props("flat dense round size=xs").classes("text-teal-600") \
                        .tooltip("Open a new position")

            # Employee list
            with ui.column().classes("gap-0 overflow-y-auto flex-grow"):
                if not employees:
                    with ui.column().classes("w-full items-center py-12 px-4"):
                        ui.icon("group_add", size="lg").classes("text-slate-300 mb-2")
                        ui.label("No team members yet").classes("text-slate-400 text-sm mb-3")
                        ui.button("Open Position", icon="person_add",
                                  on_click=lambda: vacancy_dialog.open()) \
                            .props("unelevated no-caps color=primary")
                else:
                    for emp in employees:
                        _render_sidebar_employee(emp, selected_id)

            # Bottom — user info
            with ui.column().classes("px-4 py-3 border-t border-slate-200 flex-shrink-0"):
                tier_name = TIER_NAMES.get(user.tier, "Free")
                with ui.row().classes("items-center gap-2 text-xs text-slate-500"):
                    ui.icon("account_circle", size="xs")
                    ui.label(user.email).classes("truncate")
                with ui.row().classes("items-center gap-2 text-xs text-slate-500 mt-1"):
                    ui.label(tier_name).classes("font-medium")
                    ui.label("·")
                    if user.tier == TIER_FOUNDER:
                        ui.html('<span class="font-bold" style="color: #1DE0C8;">&infin;</span> credits')
                    else:
                        ui.label(f"{user.credits} credits")

        # ════════════════════════════════
        # RIGHT PANEL
        # ════════════════════════════════
        with ui.column().classes("flex-grow gap-0").style("height: 100vh; overflow: hidden; min-width: 0"):

            # Top navbar
            with ui.row().classes(
                "w-full items-center justify-between px-6 bg-white border-b border-slate-200 flex-shrink-0"
            ).style("height: 56px; min-height: 56px"):

                with ui.row().classes("items-center gap-2"):
                    nav_items = [
                        ("/agents-dashboard", "Agents", "smart_toy"),
                        ("/team", "Team", "badge"),
                        ("/monitoring", "Monitoring", "monitor_heart"),
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

            # Detail panel
            with ui.column().classes("flex-grow overflow-y-auto p-6 bg-white"):
                if selected_id:
                    _render_employee_detail(selected_id, employees)
                else:
                    with ui.column().classes("w-full h-full items-center justify-center"):
                        ui.icon("badge", size="xl").classes("text-slate-200 mb-4")
                        ui.label("Open a position to hire your first team member") \
                            .classes("text-xl text-slate-400 text-center")
                        ui.button("Open Position", icon="person_add",
                                  on_click=lambda: vacancy_dialog.open()) \
                            .props("unelevated no-caps color=primary size=lg").classes("mt-4")

    return None


# ---------------------------------------------------------------------------
# Sidebar employee item
# ---------------------------------------------------------------------------

def _render_sidebar_employee(emp: dict, selected_id: str):
    """Render a single employee in the sidebar."""
    eid = emp.get("id", "")
    status = emp.get("status", "active")
    color = STATUS_COLORS.get(status, "#94a3b8")
    is_selected = eid == selected_id

    bg = "bg-teal-50" if is_selected else "hover:bg-slate-100"
    border = "border-l-[3px] border-teal-500" if is_selected else "border-l-[3px] border-transparent"

    def select_emp(e=eid):
        ui.navigate.to(f"/team?employee={e}")

    with ui.element("div").classes(f"w-full cursor-pointer {bg} {border} py-2.5 px-3") \
            .on("click", select_emp):
        with ui.row().classes("items-center gap-3 w-full flex-nowrap"):
            # Avatar emoji
            ui.label(emp.get("avatar_emoji", "🤖")).classes("text-2xl flex-shrink-0")
            with ui.column().classes("gap-0 min-w-0 flex-grow overflow-hidden"):
                ui.label(emp.get("name", "Unknown")).classes("text-sm font-medium text-slate-800 truncate")
                with ui.row().classes("items-center gap-1"):
                    ui.html(f'<div style="width:6px;height:6px;border-radius:50%;background:{color};flex-shrink:0"></div>')
                    ui.label(emp.get("title", "")).classes("text-xs text-slate-400 truncate")


# ---------------------------------------------------------------------------
# Employee detail panel
# ---------------------------------------------------------------------------

def _render_employee_detail(employee_id: str, employees: list[dict]):
    """Render full detail panel for a selected employee."""
    emp = None
    for e in employees:
        if e.get("id") == employee_id:
            emp = e
            break

    if not emp:
        ui.label(f"Employee not found: {employee_id}").classes("text-red-500")
        return

    status = emp.get("status", "active")
    color = STATUS_COLORS.get(status, "#94a3b8")
    spec = emp.get("spec", {})

    # Header
    with ui.row().classes("w-full items-start justify-between mb-6"):
        with ui.row().classes("items-center gap-4"):
            ui.label(emp.get("avatar_emoji", "🤖")).classes("text-5xl")
            with ui.column().classes("gap-1"):
                ui.label(emp.get("name", "?")).classes("text-2xl font-bold")
                ui.label(emp.get("title", "")).classes("text-sm text-slate-500")
                with ui.row().classes("items-center gap-2 mt-1"):
                    ui.badge(status.upper()).style(f"background-color: {color}; color: white")
                    if emp.get("hired_at"):
                        try:
                            hired = datetime.fromisoformat(emp["hired_at"])
                            ui.label(f"Hired {hired.strftime('%b %d, %Y')}").classes("text-xs text-slate-400")
                        except Exception:
                            pass

        # Action buttons
        with ui.row().classes("gap-2 flex-shrink-0"):
            if status == "active":
                ui.button("Assign Task", icon="assignment",
                          on_click=lambda e=emp: _open_assign_task_dialog(e)) \
                    .props("unelevated no-caps color=primary")

                async def do_suspend(eid=employee_id):
                    db = _get_db()
                    if db:
                        await db.update_employee_status(eid, "suspended")
                    ui.navigate.reload()

                ui.button("Suspend", icon="pause", on_click=do_suspend) \
                    .props("flat dense no-caps").classes("text-orange-600")

            elif status == "suspended":
                async def do_activate(eid=employee_id):
                    db = _get_db()
                    if db:
                        await db.update_employee_status(eid, "active")
                    ui.navigate.reload()

                ui.button("Reactivate", icon="play_arrow", on_click=do_activate) \
                    .props("flat dense no-caps").classes("text-green-600")

            async def do_archive(eid=employee_id):
                db = _get_db()
                if db:
                    await db.update_employee_status(eid, "archived")
                ui.navigate.reload()

            ui.button("Archive", icon="archive", on_click=do_archive) \
                .props("flat dense no-caps").classes("text-slate-500")

    # Personality & Strengths
    with ui.card().classes("w-full p-4 mb-4"):
        ui.label("Profile").classes("text-sm font-semibold text-slate-700 mb-2")
        ui.label(emp.get("personality", "")).classes("text-sm text-slate-600 mb-3")

        strengths = emp.get("strengths", [])
        if strengths:
            with ui.row().classes("gap-2 flex-wrap"):
                for s in strengths:
                    ui.badge(s).props("outline").classes("text-teal-600")

    # Tool Bundles
    bundles = emp.get("tool_bundles", [])
    with ui.card().classes("w-full p-4 mb-4"):
        ui.label("Assigned Tools").classes("text-sm font-semibold text-slate-700 mb-2")
        if bundles:
            with ui.row().classes("gap-3 flex-wrap"):
                for bk in bundles:
                    bundle = TOOL_BUNDLES.get(bk)
                    if bundle:
                        with ui.row().classes("items-center gap-2 bg-slate-50 rounded-lg px-3 py-2"):
                            ui.icon(bundle["icon"], size="sm").classes("text-teal-600")
                            with ui.column().classes("gap-0"):
                                ui.label(bundle["label"]).classes("text-sm font-medium")
                                ui.label(bundle["description"]).classes("text-xs text-slate-400")
        else:
            ui.label("No tools assigned").classes("text-xs text-slate-400")

    # Active agent sessions for this employee
    registry = _get_registry()
    all_agents = registry.list_all() if registry else []
    linked = [a for a in all_agents if a.get("spec", {}).get("metadata", {}).get("employee_id") == employee_id]

    with ui.card().classes("w-full p-4 mb-4"):
        ui.label(f"Active Sessions ({len(linked)})").classes("text-sm font-semibold text-slate-700 mb-2")
        if not linked:
            ui.label("No active sessions — assign a task to get started").classes("text-xs text-slate-400")
        else:
            for agent in linked:
                state = agent.get("state", "unknown")
                aspec = agent.get("spec", {})
                health = agent.get("health", {})
                from .agents import STATE_COLORS as AGENT_COLORS
                agent_color = AGENT_COLORS.get(state, "#94a3b8")
                with ui.row().classes("items-center gap-3 py-2 border-b border-slate-100"):
                    ui.html(f'<div style="width:8px;height:8px;border-radius:50%;background:{agent_color};flex-shrink:0"></div>')
                    ui.label(aspec.get("goal", aspec.get("name", "?"))).classes("text-sm flex-grow truncate")
                    ui.badge(state.upper()).style(f"background-color: {agent_color}; color: white; font-size: 10px")
                    tokens = health.get("tokens_total", 0)
                    if tokens:
                        from .agents import _format_tokens
                        ui.label(f"{_format_tokens(tokens)} tok").classes("text-xs text-slate-400")

    # System Prompt (collapsible)
    with ui.expansion("System Prompt", icon="psychology").classes("w-full mb-4").props("dense"):
        ui.label(spec.get("system_prompt", "N/A")).classes("text-xs font-mono text-slate-600 whitespace-pre-wrap")


# ---------------------------------------------------------------------------
# Vacancy / Hiring dialog
# ---------------------------------------------------------------------------

def _build_vacancy_dialog():
    """Build a multi-step dialog for vacancy creation. Returns the dialog (call .open() to show)."""

    dialog = ui.dialog().props("persistent")
    vacancy_text = {"value": ""}
    candidates_data = {"value": []}
    selected_candidate = {"value": None}
    selected_bundles = {"value": set()}
    # Track which step we're on: 1=describe, 2=candidates, 3=onboard
    current_step = {"value": 1}

    with dialog:
        with ui.card().style("width: 800px; max-width: 90vw; max-height: 90vh; overflow-y: auto"):
            # Header
            with ui.row().classes("w-full items-center justify-between px-6 py-4 border-b border-slate-200"):
                dialog_title = ui.label("Step 1: Describe the Position").classes("text-xl font-bold text-slate-800")
                ui.button(icon="close", on_click=dialog.close).props("flat round dense")

            # ── Step 1: Describe position ──
            step1 = ui.column().classes("w-full px-6 py-4 gap-4")
            with step1:
                ui.label(
                    "Describe the role you need filled. What should this employee do? "
                    "What skills and personality traits are important?"
                ).classes("text-sm text-slate-600")

                vacancy_input = ui.textarea(
                    placeholder="e.g., I need a marketing researcher who can find competitor pricing, "
                                "analyze market trends, monitor social media sentiment, "
                                "and produce weekly analysis reports...",
                ).classes("w-full").props("outlined autogrow rows=6")

                with ui.row().classes("w-full justify-end gap-2"):
                    generating_spinner = ui.spinner(size="sm").classes("hidden")

                    async def generate_and_next():
                        text = vacancy_input.value.strip()
                        if not text:
                            ui.notify("Please describe the position first", type="warning")
                            return
                        vacancy_text["value"] = text
                        generating_spinner.classes(remove="hidden")
                        ui.notify("Generating candidates...", type="info", timeout=3000)
                        cands = await _generate_candidates(text)
                        generating_spinner.classes(add="hidden")
                        if not cands:
                            ui.notify("Failed to generate candidates. Try again.", type="negative")
                            return
                        candidates_data["value"] = cands
                        _populate_candidates(candidate_container, cands, selected_candidate)
                        step1.set_visibility(False)
                        step2.set_visibility(True)
                        step3.set_visibility(False)
                        dialog_title.set_text("Step 2: Review Candidates")

                    ui.button("Generate Candidates", icon="auto_awesome",
                              on_click=generate_and_next) \
                        .props("unelevated no-caps color=primary")

            # ── Step 2: Review candidates ──
            step2 = ui.column().classes("w-full px-6 py-4 gap-4")
            step2.set_visibility(False)
            with step2:
                ui.label(
                    "Meet your candidates! Each has a unique personality and approach. "
                    "Click on one to select them."
                ).classes("text-sm text-slate-600")

                candidate_container = ui.column().classes("w-full gap-4")

                with ui.row().classes("w-full justify-between gap-2"):
                    def back_to_step1():
                        step1.set_visibility(True)
                        step2.set_visibility(False)
                        step3.set_visibility(False)
                        dialog_title.set_text("Step 1: Describe the Position")

                    ui.button("Back", icon="arrow_back", on_click=back_to_step1) \
                        .props("flat no-caps")

                    def proceed_to_onboard():
                        if selected_candidate["value"] is None:
                            ui.notify("Please select a candidate first", type="warning")
                            return
                        cand = selected_candidate["value"]
                        suggested = cand.get("suggested_bundles", [])
                        selected_bundles["value"] = set(b for b in suggested if b in TOOL_BUNDLES)
                        _populate_onboarding(onboard_container, cand, selected_bundles)
                        step1.set_visibility(False)
                        step2.set_visibility(False)
                        step3.set_visibility(True)
                        dialog_title.set_text("Step 3: Onboard")

                    ui.button("Proceed to Onboarding", icon="arrow_forward",
                              on_click=proceed_to_onboard) \
                        .props("unelevated no-caps color=primary")

            # ── Step 3: Onboard ──
            step3 = ui.column().classes("w-full px-6 py-4 gap-4")
            step3.set_visibility(False)
            with step3:
                ui.label(
                    "Configure your new hire's tools and budget, then welcome them aboard!"
                ).classes("text-sm text-slate-600")

                onboard_container = ui.column().classes("w-full gap-4")

                with ui.row().classes("w-full justify-between gap-2"):
                    def back_to_step2():
                        step1.set_visibility(False)
                        step2.set_visibility(True)
                        step3.set_visibility(False)
                        dialog_title.set_text("Step 2: Review Candidates")

                    ui.button("Back", icon="arrow_back", on_click=back_to_step2) \
                        .props("flat no-caps")

                    async def hire_employee():
                        cand = selected_candidate["value"]
                        if not cand:
                            return
                        bundles = list(selected_bundles["value"])
                        tools = _bundles_to_tools(bundles)
                        now = datetime.now(timezone.utc).isoformat()

                        from auton.models import AgentSpec, EmployeeProfile
                        spec = AgentSpec(
                            name=cand["name"],
                            description=cand.get("title", ""),
                            system_prompt=cand.get("system_prompt", "You are a helpful assistant."),
                            goal="Awaiting task assignment",
                            tools=tools,
                            model="openrouter/anthropic/claude-sonnet-4-6",
                            max_turns=50,
                            max_tokens=100_000,
                        )

                        profile = {
                            "id": f"emp-{uuid.uuid4().hex[:8]}",
                            "name": cand["name"],
                            "title": cand.get("title", ""),
                            "personality": cand.get("personality", ""),
                            "strengths": cand.get("strengths", []),
                            "avatar_emoji": cand.get("avatar_emoji", "🤖"),
                            "status": "active",
                            "tool_bundles": bundles,
                            "spec": spec.model_dump(),
                            "hired_at": now,
                            "last_active": None,
                        }

                        db = _get_db()
                        if db:
                            await db.save_employee(profile)

                        dialog.close()
                        ui.notify(f"Welcome aboard, {cand['name']}!", type="positive", timeout=5000)
                        ui.navigate.to(f"/team?employee={profile['id']}")

                    ui.button("Welcome Aboard!", icon="celebration",
                              on_click=hire_employee) \
                        .props("unelevated no-caps color=primary size=lg")

    return dialog


def _populate_candidates(container, candidates: list[dict], selected_ref: dict):
    """Populate the candidate container with candidate cards."""
    container.clear()
    card_elements = []

    with container:
        for i, cand in enumerate(candidates):
            card = ui.card().classes(
                "w-full p-4 cursor-pointer border-2 border-transparent hover:border-teal-300 transition-all"
            )
            card_elements.append(card)

            def make_select(idx, c, cards):
                def select():
                    selected_ref["value"] = c
                    for j, ce in enumerate(cards):
                        if j == idx:
                            ce.classes(remove="border-transparent")
                            ce.classes(add="border-teal-500 bg-teal-50")
                        else:
                            ce.classes(remove="border-teal-500 bg-teal-50")
                            ce.classes(add="border-transparent")
                return select

            card.on("click", make_select(i, cand, card_elements))

            with card:
                with ui.row().classes("items-center gap-4 mb-3"):
                    ui.label(cand.get("avatar_emoji", "🤖")).classes("text-4xl")
                    with ui.column().classes("gap-0"):
                        ui.label(cand.get("name", "Candidate")).classes("text-lg font-bold")
                        ui.label(cand.get("title", "")).classes("text-sm text-slate-500")

                ui.label(cand.get("personality", "")).classes("text-sm text-slate-600 mb-3")

                strengths = cand.get("strengths", [])
                if strengths:
                    with ui.row().classes("gap-2 flex-wrap"):
                        for s in strengths:
                            ui.badge(s).props("outline").classes("text-teal-600")


def _populate_onboarding(container, candidate: dict, bundles_ref: dict):
    """Populate the onboarding step with tool selection and settings."""
    container.clear()

    with container:
        # Candidate summary
        with ui.row().classes("items-center gap-4 mb-4 p-4 bg-teal-50 rounded-lg"):
            ui.label(candidate.get("avatar_emoji", "🤖")).classes("text-4xl")
            with ui.column().classes("gap-0"):
                ui.label(candidate.get("name", "")).classes("text-lg font-bold")
                ui.label(candidate.get("title", "")).classes("text-sm text-slate-500")

        # Tool bundles
        ui.label("Assign Tool Bundles").classes("text-sm font-semibold text-slate-700 mb-2")
        for key, bundle in TOOL_BUNDLES.items():
            checked = key in bundles_ref["value"]

            def make_toggle(k):
                def toggle(e):
                    if e.value:
                        bundles_ref["value"].add(k)
                    else:
                        bundles_ref["value"].discard(k)
                return toggle

            with ui.row().classes("items-center gap-3 py-2"):
                cb = ui.checkbox(
                    bundle["label"],
                    value=checked,
                    on_change=make_toggle(key),
                )
                ui.icon(bundle["icon"], size="sm").classes("text-teal-500")
                ui.label(bundle["description"]).classes("text-xs text-slate-400")


# ---------------------------------------------------------------------------
# Assign Task dialog
# ---------------------------------------------------------------------------

def _open_assign_task_dialog(employee: dict):
    """Open a dialog to assign a task (goal) to an employee, spawning an agent."""

    dialog = ui.dialog()

    with dialog:
        with ui.card().classes("w-full max-w-xl p-6"):
            ui.label(f"Assign Task to {employee.get('name', '?')}") \
                .classes("text-lg font-bold mb-4")

            task_input = ui.textarea(
                placeholder="Describe the task or goal for this employee...",
            ).classes("w-full mb-4").props("outlined autogrow rows=4")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

                async def assign():
                    goal = task_input.value.strip()
                    if not goal:
                        ui.notify("Please describe the task", type="warning")
                        return

                    registry = _get_registry()
                    if not registry:
                        ui.notify("Agent registry not available", type="negative")
                        return

                    from auton.models import AgentSpec, SpawnRequest

                    spec_data = employee.get("spec", {})
                    spec = AgentSpec(**{**spec_data, "goal": goal})
                    spec.metadata = {**spec.metadata, "employee_id": employee["id"]}

                    req = SpawnRequest(spec=spec)
                    try:
                        node = registry.spawn(req)
                        dialog.close()
                        ui.notify(f"Task assigned! Agent {node.id} is on it.", type="positive")

                        # Update last_active
                        db = _get_db()
                        if db:
                            emp_data = await db.get_employee(employee["id"])
                            if emp_data:
                                emp_data["last_active"] = datetime.now(timezone.utc).isoformat()
                                await db.save_employee(emp_data)

                        ui.navigate.reload()
                    except Exception as e:
                        ui.notify(f"Failed to spawn agent: {e}", type="negative")

                ui.button("Assign & Go", icon="rocket_launch", on_click=assign) \
                    .props("unelevated no-caps color=primary")

    dialog.open()
