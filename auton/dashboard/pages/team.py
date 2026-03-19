"""Team dashboard — hire persistent agent employees via a vacancy/hiring flow."""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import Request
from starlette.responses import Response, RedirectResponse
from nicegui import ui

from litellm import acompletion

from ..sessions import get_user_and_tokens
from ..constants import TIER_FREE, TIER_FOUNDER, TIER_NAMES
from ..style import PRIMARY
from ..layouts import default as default_layout

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
    "email": {
        "label": "Email",
        "icon": "email",
        "description": "Send and read emails, manage inbox",
        "tools": ["send_email", "read_email", "list_emails"],
    },
    "slack": {
        "label": "Slack",
        "icon": "chat",
        "description": "Send and read Slack messages",
        "tools": ["send_slack_message", "read_slack_messages", "list_slack_channels"],
    },
}

# ---------------------------------------------------------------------------
# Pre-defined role templates
# ---------------------------------------------------------------------------

ROLE_TEMPLATES = [
    {
        "emoji": "🔍",
        "title": "Lead Generator",
        "description": "Find and qualify potential customers, research companies, and build prospect lists",
        "vacancy_text": (
            "I need a lead generation specialist who can research companies in target markets, "
            "find decision-makers and their contact information, qualify leads based on ICP criteria, "
            "and produce structured prospect lists with enrichment data."
        ),
        "suggested_bundles": ["web_research", "files", "email", "slack"],
    },
    {
        "emoji": "💰",
        "title": "Fundraiser",
        "description": "Research investors, prepare outreach materials, and track fundraising pipeline",
        "vacancy_text": (
            "I need a fundraising research assistant who can identify relevant investors and VCs, "
            "research their portfolio and investment thesis, prepare personalized outreach drafts, "
            "and maintain a structured pipeline of fundraising conversations."
        ),
        "suggested_bundles": ["web_research", "files", "email", "slack"],
    },
    {
        "emoji": "💻",
        "title": "Software Engineer",
        "description": "Write code, fix bugs, implement features, and manage development tasks",
        "vacancy_text": (
            "I need a software engineer who can write clean, production-quality code, "
            "debug issues, implement new features, write tests, and follow best practices. "
            "They should be comfortable with multiple languages and frameworks."
        ),
        "suggested_bundles": ["code", "files", "email", "slack"],
    },
    {
        "emoji": "📊",
        "title": "Market Researcher",
        "description": "Analyze competitors, track market trends, and produce intelligence reports",
        "vacancy_text": (
            "I need a market research analyst who can monitor competitors, "
            "analyze pricing strategies, track industry trends and news, "
            "and produce weekly competitive intelligence reports."
        ),
        "suggested_bundles": ["web_research", "files", "email", "slack"],
    },
    {
        "emoji": "📝",
        "title": "Content Writer",
        "description": "Write blog posts, social media content, newsletters, and marketing copy",
        "vacancy_text": (
            "I need a content writer who can produce engaging blog posts, social media content, "
            "email newsletters, and marketing copy. They should adapt tone to different audiences "
            "and maintain brand consistency across channels."
        ),
        "suggested_bundles": ["web_research", "files", "email", "slack"],
    },
    {
        "emoji": "🛡️",
        "title": "Security Analyst",
        "description": "Monitor for vulnerabilities, review code for security issues, and track threats",
        "vacancy_text": (
            "I need a security analyst who can scan codebases for vulnerabilities, "
            "monitor security advisories, review dependencies for known CVEs, "
            "and produce security assessment reports with remediation recommendations."
        ),
        "suggested_bundles": ["code", "web_research", "files", "email", "slack"],
    },
]

STATUS_COLORS = {
    "active": "#22c55e",
    "waiting_for_input": "#3b82f6",
    "suspended": "#f97316",
    "archived": "#9ca3af",
}

STATUS_LABELS = {
    "active": "Active",
    "waiting_for_input": "Waiting for Input",
    "suspended": "Suspended",
    "archived": "Archived",
}

# ---------------------------------------------------------------------------
# Face generation via Recraft API
# ---------------------------------------------------------------------------

FACES_DIR = Path(__file__).parent.parent / "static" / "faces"
RECRAFT_API_KEY = os.environ.get("RECRAFT_API_KEY", "")


async def _generate_face(name: str, title: str, candidate_id: str) -> str | None:
    """Generate a professional illustrated portrait using Recraft API."""
    if not RECRAFT_API_KEY:
        logger.warning("RECRAFT_API_KEY not set, skipping face generation")
        return None

    prompt = (
        f"Professional illustrated portrait of a person named {name}, "
        f"who works as a {title}. Clean solid color background, "
        f"friendly confident expression, modern business casual style, "
        f"digital illustration, high quality, suitable for a professional profile picture."
    )

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://external.api.recraft.ai/v1/images/generations",
                headers={"Authorization": f"Bearer {RECRAFT_API_KEY}"},
                json={
                    "prompt": prompt,
                    "model": "recraftv4",
                    "n": 1,
                    "size": "1024x1024",
                    "response_format": "url",
                },
            )
            if resp.status_code != 200:
                logger.error(f"Recraft face gen failed ({resp.status_code}): {resp.text[:200]}")
                return None

            data = resp.json()
            image_url = data["data"][0]["url"]

            # Download and save locally
            img_resp = await client.get(image_url)
            if img_resp.status_code != 200:
                return None

            FACES_DIR.mkdir(parents=True, exist_ok=True)
            face_path = FACES_DIR / f"{candidate_id}.png"
            face_path.write_bytes(img_resp.content)
            return f"/static/dashboard/faces/{candidate_id}.png"
    except Exception as e:
        logger.error(f"Face generation failed for {name}: {e}")
        return None

# ---------------------------------------------------------------------------
# Candidate generation prompt
# ---------------------------------------------------------------------------

CANDIDATE_PROMPT = """\
You are a creative HR assistant for an AI agent platform. The user has written a position description for a persistent AI employee. Generate exactly 3 candidate profiles that could fill this role. Each candidate should have a distinct personality, background, and approach.

Position description:
{vacancy}

Respond with ONLY a JSON array of 3 objects, each with these fields:
- "name": a realistic full name (diverse backgrounds, varied gender and ethnicity)
- "title": a concise job title (3-5 words)
- "personality": 1-2 sentences describing their work style and personality
- "strengths": array of 3-4 short strength keywords
- "appearance": a brief description of their appearance for portrait generation (e.g. "young woman with dark curly hair, warm brown eyes, wearing a navy blazer")
- "avatar_emoji": a single emoji that represents their role
- "system_prompt": a detailed system prompt for the AI agent (2-3 paragraphs, written in second person: "You are...")
- "suggested_bundles": array of tool bundle keys from: {bundles}

Return ONLY valid JSON, no markdown fences or explanation."""


def _bundles_description() -> str:
    return ", ".join(f'"{k}" ({v["label"]})' for k, v in TOOL_BUNDLES.items())


async def _generate_candidates(vacancy: str) -> list[dict]:
    """Call LLM to generate 3 candidate profiles, then generate face images."""
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
        if not isinstance(candidates, list) or len(candidates) < 1:
            return []
        candidates = candidates[:3]

        # Generate face images concurrently for all candidates
        async def gen_face_for(cand, idx):
            cand_id = f"cand-{uuid.uuid4().hex[:8]}"
            cand["_candidate_id"] = cand_id
            appearance = cand.get("appearance", "")
            face_prompt_desc = appearance or cand.get("title", "professional")
            avatar_url = await _generate_face(
                cand.get("name", f"Candidate {idx+1}"),
                face_prompt_desc,
                cand_id,
            )
            cand["avatar_url"] = avatar_url

        await asyncio.gather(*(gen_face_for(c, i) for i, c in enumerate(candidates)))
        return candidates
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
# Page renderer — uses default layout (no sidebar)
# ---------------------------------------------------------------------------

def render_team_dashboard(request: Request) -> Optional[Response]:
    """Team page using default navbar layout with card grid."""

    # Load employees
    db = _get_db()
    employees: list[dict] = []

    if db and db._conn:
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

    selected_id = request.query_params.get("employee")

    def content():
        vacancy_dialog = _build_vacancy_dialog()

        with ui.column().classes("w-full max-w-6xl mx-auto p-6 gap-6"):
            # Page header
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Team").classes("text-2xl font-bold text-slate-800")
                ui.button("Hire New Member", icon="person_add",
                          on_click=lambda: vacancy_dialog.open()) \
                    .props("unelevated no-caps color=primary")

            if not employees:
                # Empty state
                with ui.column().classes("w-full items-center py-20"):
                    ui.icon("groups", size="xl").classes("text-slate-200 mb-4")
                    ui.label("No team members yet").classes("text-xl text-slate-400 mb-2")
                    ui.label("Hire your first AI employee to get started") \
                        .classes("text-sm text-slate-400 mb-6")
                    ui.button("Hire New Member", icon="person_add",
                              on_click=lambda: vacancy_dialog.open()) \
                        .props("unelevated no-caps color=primary size=lg")
            elif selected_id:
                _render_employee_detail(selected_id, employees, vacancy_dialog)
            else:
                # Employee card grid
                with ui.row().classes("w-full gap-4 flex-wrap"):
                    for emp in employees:
                        _render_employee_card(emp)

    return default_layout.render(content, request)


# ---------------------------------------------------------------------------
# Employee card (grid view)
# ---------------------------------------------------------------------------

def _render_avatar(emp_or_cand: dict, size: str = "48px"):
    """Render avatar: face image if available, emoji fallback."""
    avatar_url = emp_or_cand.get("avatar_url")
    if avatar_url:
        ui.image(avatar_url).style(
            f"width: {size}; height: {size}; border-radius: 50%; object-fit: cover; flex-shrink: 0"
        )
    else:
        emoji = emp_or_cand.get("avatar_emoji", "🤖")
        font_size = f"{int(size.replace('px', '')) // 2}px" if "px" in size else "24px"
        ui.label(emoji).style(
            f"font-size: {font_size}; width: {size}; height: {size}; "
            f"display: flex; align-items: center; justify-content: center; flex-shrink: 0"
        )


def _render_employee_card(emp: dict):
    """Render an employee as a clickable card."""
    eid = emp.get("id", "")
    status = emp.get("status", "active")
    color = STATUS_COLORS.get(status, "#94a3b8")
    status_label = STATUS_LABELS.get(status, status.capitalize())

    with ui.card().classes(
        "p-4 cursor-pointer hover:shadow-lg transition-all border border-slate-100"
    ).style("width: 280px").on("click", lambda e=eid: ui.navigate.to(f"/team?employee={e}")):
        with ui.row().classes("items-center gap-3 mb-3"):
            _render_avatar(emp, "56px")
            with ui.column().classes("gap-0 min-w-0"):
                ui.label(emp.get("name", "Unknown")).classes("text-sm font-bold text-slate-800 truncate")
                ui.label(emp.get("title", "")).classes("text-xs text-slate-500 truncate")

        with ui.row().classes("items-center gap-2 mb-3"):
            ui.html(f'<div style="width:8px;height:8px;border-radius:50%;background:{color}"></div>')
            ui.label(status_label).classes("text-xs text-slate-500")

        strengths = emp.get("strengths", [])
        if strengths:
            with ui.row().classes("gap-1 flex-wrap"):
                for s in strengths[:3]:
                    ui.badge(s).props("outline").classes("text-teal-600").style("font-size: 10px")


# ---------------------------------------------------------------------------
# Employee detail panel
# ---------------------------------------------------------------------------

def _render_employee_detail(employee_id: str, employees: list[dict], vacancy_dialog):
    """Render full detail for a selected employee."""
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

    # Back link
    ui.button("Back to Team", icon="arrow_back",
              on_click=lambda: ui.navigate.to("/team")) \
        .props("flat no-caps dense").classes("mb-4 text-slate-500")

    # Header
    with ui.row().classes("w-full items-start justify-between mb-6"):
        with ui.row().classes("items-center gap-4"):
            _render_avatar(emp, "80px")
            with ui.column().classes("gap-1"):
                ui.label(emp.get("name", "?")).classes("text-2xl font-bold")
                ui.label(emp.get("title", "")).classes("text-sm text-slate-500")
                with ui.row().classes("items-center gap-2 mt-1"):
                    status_label = STATUS_LABELS.get(status, status.capitalize())
                    ui.badge(status_label.upper()).style(f"background-color: {color}; color: white")
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
# Vacancy / Hiring dialog — with pre-defined role templates
# ---------------------------------------------------------------------------

def _build_vacancy_dialog():
    """Build a multi-step dialog: role templates -> describe -> candidates -> onboard."""

    dialog = ui.dialog().props("persistent")
    vacancy_text = {"value": ""}
    candidates_data = {"value": []}
    selected_candidate = {"value": None}
    selected_bundles = {"value": set()}

    with dialog:
        with ui.card().style("width: 800px; max-width: 90vw; max-height: 90vh; overflow-y: auto"):
            # Header
            with ui.row().classes("w-full items-center justify-between px-6 py-4 border-b border-slate-200"):
                dialog_title = ui.label("Choose a Role").classes("text-xl font-bold text-slate-800")
                ui.button(icon="close", on_click=dialog.close).props("flat round dense")

            # ── Step 0: Role templates ──
            step0 = ui.column().classes("w-full px-6 py-4 gap-4")
            with step0:
                ui.label(
                    "Pick a pre-defined role or create a custom position."
                ).classes("text-sm text-slate-600")

                with ui.grid(columns=2).classes("w-full gap-3"):
                    for tmpl in ROLE_TEMPLATES:
                        def make_select_role(t=tmpl):
                            def select():
                                vacancy_text["value"] = t["vacancy_text"]
                                selected_bundles["value"] = set(t.get("suggested_bundles", []))
                                vacancy_input.value = t["vacancy_text"]
                                step0.set_visibility(False)
                                step1.set_visibility(True)
                                dialog_title.set_text("Describe the Position")
                            return select

                        with ui.card().classes(
                            "p-4 cursor-pointer border-2 border-transparent "
                            "hover:border-teal-300 hover:shadow-md transition-all"
                        ).on("click", make_select_role()):
                            ui.label(tmpl["emoji"]).classes("text-3xl mb-2")
                            ui.label(tmpl["title"]).classes("text-sm font-bold text-slate-800")
                            ui.label(tmpl["description"]).classes("text-xs text-slate-500")

                # Custom role button
                with ui.row().classes("w-full justify-center mt-2"):
                    def go_custom():
                        vacancy_input.value = ""
                        vacancy_text["value"] = ""
                        step0.set_visibility(False)
                        step1.set_visibility(True)
                        dialog_title.set_text("Describe the Position")

                    ui.button("Custom Role", icon="edit",
                              on_click=go_custom) \
                        .props("flat no-caps").classes("text-slate-500")

            # ── Step 1: Describe position ──
            step1 = ui.column().classes("w-full px-6 py-4 gap-4")
            step1.set_visibility(False)
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

                with ui.row().classes("w-full justify-between gap-2"):
                    def back_to_roles():
                        step0.set_visibility(True)
                        step1.set_visibility(False)
                        dialog_title.set_text("Choose a Role")

                    ui.button("Back", icon="arrow_back", on_click=back_to_roles) \
                        .props("flat no-caps")

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
                        dialog_title.set_text("Review Candidates")

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
                        dialog_title.set_text("Describe the Position")

                    ui.button("Back", icon="arrow_back", on_click=back_to_step1) \
                        .props("flat no-caps")

                    def proceed_to_onboard():
                        if selected_candidate["value"] is None:
                            ui.notify("Please select a candidate first", type="warning")
                            return
                        cand = selected_candidate["value"]
                        # Merge LLM-suggested bundles with any pre-selected from role template
                        suggested = cand.get("suggested_bundles", [])
                        merged = selected_bundles["value"] | set(b for b in suggested if b in TOOL_BUNDLES)
                        selected_bundles["value"] = merged
                        _populate_onboarding(onboard_container, cand, selected_bundles)
                        step1.set_visibility(False)
                        step2.set_visibility(False)
                        step3.set_visibility(True)
                        dialog_title.set_text("Onboard")

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
                        dialog_title.set_text("Review Candidates")

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

                        # Copy face image from candidate ID to employee ID
                        emp_id = f"emp-{uuid.uuid4().hex[:8]}"
                        avatar_url = cand.get("avatar_url")
                        if avatar_url and cand.get("_candidate_id"):
                            src = FACES_DIR / f"{cand['_candidate_id']}.png"
                            if src.exists():
                                dst = FACES_DIR / f"{emp_id}.png"
                                dst.write_bytes(src.read_bytes())
                                avatar_url = f"/static/dashboard/faces/{emp_id}.png"

                        profile = {
                            "id": emp_id,
                            "name": cand["name"],
                            "title": cand.get("title", ""),
                            "personality": cand.get("personality", ""),
                            "strengths": cand.get("strengths", []),
                            "avatar_emoji": cand.get("avatar_emoji", "🤖"),
                            "avatar_url": avatar_url,
                            "status": "waiting_for_input",
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
                    _render_avatar(cand, "64px")
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
            _render_avatar(candidate, "64px")
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
