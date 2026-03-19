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
# OpenRouter top models (cached)
# ---------------------------------------------------------------------------

_MODEL_CACHE: dict = {"models": [], "fetched_at": 0}
_TOP_PROVIDERS = {"anthropic", "openai", "google", "deepseek", "mistralai", "x-ai", "meta-llama"}


def _fetch_top_models() -> list[dict]:
    """Fetch and cache top LLM models from OpenRouter. Returns list of {id, name, ctx, price}."""
    import time
    now = time.time()
    if _MODEL_CACHE["models"] and now - _MODEL_CACHE["fetched_at"] < 3600:
        return _MODEL_CACHE["models"]

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return []

    try:
        import httpx as _httpx
        resp = _httpx.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        if resp.status_code != 200:
            return _MODEL_CACHE["models"]

        data = resp.json()
        results = []
        seen = set()
        # Only include flagship models worth offering as agent backends
        _CURATED = {
            "anthropic/claude-opus-4.6", "anthropic/claude-sonnet-4.6",
            "anthropic/claude-opus-4.5", "anthropic/claude-sonnet-4.5",
            "anthropic/claude-haiku-4.5",
            "anthropic/claude-sonnet-4", "anthropic/claude-opus-4",
            "openai/gpt-5", "openai/gpt-5-mini",
            "openai/gpt-4.1", "openai/gpt-4.1-mini",
            "google/gemini-2.5-pro", "google/gemini-2.5-flash",
            "google/gemini-3-pro-preview", "google/gemini-3-flash-preview",
            "deepseek/deepseek-r1", "deepseek/deepseek-v3.2",
            "x-ai/grok-4", "x-ai/grok-3",
            "meta-llama/llama-4-maverick",
            "mistralai/mistral-large",
        }
        for m in data.get("data", []):
            mid = m.get("id", "")
            if mid not in _CURATED:
                continue
            arch = m.get("architecture", {})
            if "text" not in arch.get("output_modalities", []):
                continue
            price = float(m.get("pricing", {}).get("prompt", "0"))
            ctx = m.get("context_length", 0)
            results.append({
                "id": f"openrouter/{mid}",
                "name": m.get("name", mid),
                "ctx": ctx,
                "price": price,
            })

        results.sort(key=lambda x: (x["id"].split("/")[1], -x["ctx"]))
        _MODEL_CACHE["models"] = results
        _MODEL_CACHE["fetched_at"] = now
        return results
    except Exception as e:
        logger.warning(f"Failed to fetch OpenRouter models: {e}")
        return _MODEL_CACHE["models"]


# ---------------------------------------------------------------------------
# Face generation via OpenRouter (Gemini image model)
# ---------------------------------------------------------------------------

FACES_DIR = Path(__file__).parent.parent / "static" / "faces"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
FACE_MODEL = os.environ.get("AUTON_FACE_MODEL", "google/gemini-2.5-flash-image")


async def _generate_face(name: str, appearance: str, candidate_id: str) -> str | None:
    """Generate a realistic portrait photo via OpenRouter Gemini image model."""
    if not OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY not set, skipping face generation")
        return None

    prompt = (
        f"Generate a photorealistic professional headshot portrait photograph. "
        f"The person: {appearance}. "
        f"Natural soft lighting, clean neutral background, high quality, "
        f"looks like a real LinkedIn profile photo. Just the image, no text."
    )

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": FACE_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if resp.status_code != 200:
                logger.error(f"Face gen failed ({resp.status_code}): {resp.text[:200]}")
                return None

            data = resp.json()
            msg = data["choices"][0]["message"]

            # OpenRouter returns images in message.images[]
            images = msg.get("images", [])
            if not images:
                logger.warning("Face gen returned no images")
                return None

            image_url = images[0].get("image_url", {}).get("url", "")
            if not image_url.startswith("data:image/"):
                logger.warning(f"Unexpected image format: {image_url[:60]}")
                return None

            # Decode base64 image data and compress to JPEG
            import base64
            from io import BytesIO
            from PIL import Image

            b64_data = image_url.split(",", 1)[1]
            img_bytes = base64.b64decode(b64_data)

            FACES_DIR.mkdir(parents=True, exist_ok=True)
            face_path = FACES_DIR / f"{candidate_id}.jpg"

            img = Image.open(BytesIO(img_bytes)).convert("RGB")
            img.thumbnail((512, 512), Image.LANCZOS)
            # Binary search for quality that fits under 200KB
            lo, hi = 20, 92
            while lo < hi:
                mid = (lo + hi + 1) // 2
                buf = BytesIO()
                img.save(buf, "JPEG", quality=mid)
                if buf.tell() <= 200_000:
                    lo = mid
                else:
                    hi = mid - 1
            buf = BytesIO()
            img.save(buf, "JPEG", quality=lo)
            face_path.write_bytes(buf.getvalue())
            logger.info(f"Face saved: {face_path.name} ({buf.tell():,} bytes, q={lo})")
            return f"/static/dashboard/faces/{candidate_id}.jpg"
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

    # Load persistent agents (employees) from agents table
    employees: list[dict] = []

    try:
        from ..db_client import SessionLocal
        from sqlalchemy import text as sa_text
        from auton.db import _STATE_TO_STATUS
        with SessionLocal() as session:
            result = session.execute(
                sa_text(
                    "SELECT id, spec_json, state, profile_json FROM agents "
                    "WHERE profile_json IS NOT NULL "
                    "AND state NOT IN ('dead', 'terminating') "
                    "ORDER BY created_at DESC"
                )
            )
            for row in result.fetchall():
                profile = json.loads(row[3]) if isinstance(row[3], str) else row[3]
                spec = json.loads(row[1]) if isinstance(row[1], str) else row[1]
                profile["id"] = row[0]
                profile["spec"] = spec
                profile["status"] = _STATE_TO_STATUS.get(row[2], row[2])
                employees.append(profile)
    except Exception as e:
        logger.debug(f"Employee load failed: {e}")

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
            # Cadence badge
            from auton.models import Cadence
            cadence_val = emp.get("cadence", Cadence.NONE.value)
            cadence_label = Cadence(cadence_val).label if cadence_val in [c.value for c in Cadence] else cadence_val
            ui.badge(cadence_label, color="grey-4").classes("text-slate-500").style("font-size: 10px")

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
            if status != "archived":
                ui.button("Add Task", icon="add_task",
                          on_click=lambda e=emp: _open_add_task_dialog(e)) \
                    .props("unelevated no-caps color=primary")

            if status == "active":
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

    # Budget & Model card
    monthly_budget = emp.get("monthly_budget_usd", 0)
    budget_spent = emp.get("budget_spent_usd", 0)
    model_id = spec.get("model", "")
    model_short = model_id.split("/")[-1] if model_id else "not set"
    max_tokens = spec.get("max_tokens") or 0
    from .agents import _format_tokens

    with ui.card().classes("w-full p-4 mb-4"):
        with ui.row().classes("w-full items-center justify-between mb-2"):
            ui.label("Budget & Model").classes("text-sm font-semibold text-slate-700")
            ui.button("Edit", icon="edit",
                      on_click=lambda e=emp: _open_edit_dialog(e)) \
                .props("flat dense no-caps size=sm").classes("text-teal-600")
        with ui.row().classes("gap-6 flex-wrap"):
            with ui.column().classes("gap-0"):
                ui.label("Model").classes("text-xs text-slate-400")
                ui.label(model_short).classes("text-sm font-medium")
            with ui.column().classes("gap-0"):
                ui.label("Monthly Budget").classes("text-xs text-slate-400")
                if monthly_budget:
                    ui.label(f"${monthly_budget:,.0f}/mo").classes("text-sm font-medium")
                else:
                    ui.label("Unlimited").classes("text-sm font-medium text-slate-400")
            with ui.column().classes("gap-0"):
                ui.label("Spent This Period").classes("text-xs text-slate-400")
                if monthly_budget:
                    pct = (budget_spent / monthly_budget * 100) if monthly_budget else 0
                    color = "text-red-600" if pct > 90 else "text-orange-500" if pct > 70 else "text-slate-700"
                    ui.label(f"${budget_spent:,.2f} ({pct:.0f}%)").classes(f"text-sm font-medium {color}")
                else:
                    ui.label(f"${budget_spent:,.2f}").classes("text-sm font-medium")
            with ui.column().classes("gap-0"):
                ui.label("Token Budget / Task").classes("text-xs text-slate-400")
                ui.label(_format_tokens(max_tokens) if max_tokens else "Unlimited") \
                    .classes("text-sm font-medium")
            with ui.column().classes("gap-0"):
                ui.label("Max Turns").classes("text-xs text-slate-400")
                ui.label(str(spec.get("max_turns", 30))).classes("text-sm font-medium")
            with ui.column().classes("gap-0"):
                ui.label("Temperature").classes("text-xs text-slate-400")
                ui.label(f"{spec.get('temperature', 0.7):.1f}").classes("text-sm font-medium")

    # Schedule card
    from auton.models import Cadence
    cadence_val = emp.get("cadence", Cadence.NONE.value)
    cadence_label = Cadence(cadence_val).label if cadence_val in [c.value for c in Cadence] else cadence_val
    work_start = emp.get("work_time_start", "09:00")
    work_end = emp.get("work_time_end", "17:00")

    with ui.card().classes("w-full p-4 mb-4"):
        with ui.row().classes("w-full items-center justify-between mb-2"):
            ui.label("Schedule").classes("text-sm font-semibold text-slate-700")
            ui.button("Edit", icon="edit",
                      on_click=lambda e=emp: _open_edit_dialog(e)) \
                .props("flat dense no-caps size=sm").classes("text-teal-600")
        with ui.row().classes("gap-6 flex-wrap"):
            with ui.column().classes("gap-0"):
                ui.label("Cadence").classes("text-xs text-slate-400")
                ui.label(cadence_label).classes("text-sm font-medium")
            with ui.column().classes("gap-0"):
                ui.label("Work Time").classes("text-xs text-slate-400")
                ui.label(f"{work_start} – {work_end}").classes("text-sm font-medium")

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

    # Tasks card
    tasks: list[dict] = []
    try:
        from ..db_client import SessionLocal
        from sqlalchemy import text as sa_text
        with SessionLocal() as session:
            result = session.execute(
                sa_text(
                    "SELECT id, agent_id, definition, deliverables, status, "
                    "session_id, created_at, started_at, completed_at "
                    "FROM tasks WHERE agent_id = :agent_id "
                    "ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, "
                    "created_at ASC"
                ),
                {"agent_id": employee_id},
            )
            for row in result.fetchall():
                deliverables = row[3]
                tasks.append({
                    "id": row[0],
                    "definition": row[2],
                    "deliverables": json.loads(deliverables) if isinstance(deliverables, str) else deliverables,
                    "status": row[4],
                    "session_id": row[5],
                    "created_at": row[6],
                    "started_at": row[7],
                    "completed_at": row[8],
                })
    except Exception as e:
        logger.debug(f"Task load failed: {e}")

    TASK_STATUS_COLORS = {
        "queued": "#94a3b8",
        "active": "#3b82f6",
        "completed": "#22c55e",
    }
    TASK_STATUS_LABELS = {
        "queued": "Queued",
        "active": "Active",
        "completed": "Completed",
    }
    FORMAT_ICONS = {
        "MD": "description",
        "CSV": "table_chart",
        "PDF": "picture_as_pdf",
        "Excel": "grid_on",
        "Word": "article",
        "Custom": "tune",
    }

    queued_tasks = [t for t in tasks if t["status"] == "queued"]
    running_tasks = [t for t in tasks if t["status"] == "active"]
    active_tasks = queued_tasks + running_tasks

    with ui.card().classes("w-full p-4 mb-4"):
        with ui.row().classes("w-full items-center justify-between mb-2"):
            count_label = f"{len(active_tasks)}" if active_tasks else "0"
            ui.label(f"Tasks ({count_label} pending)").classes("text-sm font-semibold text-slate-700")
            with ui.row().classes("gap-1"):
                # Start / Stop buttons
                if running_tasks:
                    async def do_stop(e=emp):
                        await _stop_active_tasks(e)
                        ui.navigate.reload()
                    ui.button("Stop", icon="stop",
                              on_click=do_stop) \
                        .props("flat dense no-caps size=sm").classes("text-red-500")
                elif queued_tasks:
                    async def do_start(e=emp):
                        started = await _start_next_task(e)
                        if started:
                            ui.notify("Task started", type="positive")
                        else:
                            ui.notify("Could not start task", type="warning")
                        ui.navigate.reload()
                    ui.button("Start", icon="play_arrow",
                              on_click=do_start) \
                        .props("flat dense no-caps size=sm").classes("text-green-600")

                ui.button("Add Task", icon="add_task",
                          on_click=lambda e=emp: _open_add_task_dialog(e)) \
                    .props("flat dense no-caps size=sm").classes("text-teal-600")

        if not tasks:
            ui.label("No tasks — add one to get started").classes("text-xs text-slate-400")
        else:
            for task in tasks:
                t_status = task["status"]
                t_color = TASK_STATUS_COLORS.get(t_status, "#94a3b8")
                with ui.card().classes("w-full p-3 mb-2").style("border-left: 3px solid " + t_color):
                    with ui.row().classes("w-full items-start justify-between gap-2"):
                        with ui.column().classes("flex-grow gap-1 min-w-0"):
                            ui.label(task["definition"]).classes(
                                "text-sm text-slate-700"
                                + (" line-through text-slate-400" if t_status == "completed" else "")
                            ).style("white-space: pre-wrap")
                            # Deliverables
                            deliverables = task.get("deliverables", [])
                            if deliverables:
                                with ui.row().classes("gap-2 flex-wrap mt-1"):
                                    for d in deliverables:
                                        fmt = d.get("format", "MD")
                                        icon = FORMAT_ICONS.get(fmt, "description")
                                        with ui.row().classes("items-center gap-1 bg-slate-50 rounded px-2 py-1"):
                                            ui.icon(icon, size="xs").classes("text-slate-400")
                                            ui.label(d.get("description", fmt)).classes("text-xs text-slate-500 truncate").style("max-width: 200px")
                                            ui.badge(fmt).props("outline").classes("text-slate-400").style("font-size: 9px")
                        with ui.column().classes("items-end gap-1 flex-shrink-0"):
                            ui.badge(TASK_STATUS_LABELS.get(t_status, t_status).upper()) \
                                .style(f"background-color: {t_color}; color: white; font-size: 10px")
                            if t_status == "queued":
                                async def do_delete(tid=task["id"]):
                                    db = _get_db()
                                    if db:
                                        await db.delete_task(tid)
                                    ui.navigate.reload()
                                ui.button(icon="close", on_click=do_delete) \
                                    .props("flat round dense size=xs").classes("text-slate-400")

    # System Prompt (collapsible)
    with ui.expansion("System Prompt", icon="psychology").classes("w-full mb-4").props("dense"):
        ui.label(spec.get("system_prompt", "N/A")).classes("text-xs font-mono text-slate-600 whitespace-pre-wrap")


# ---------------------------------------------------------------------------
# Edit employee settings dialog
# ---------------------------------------------------------------------------

def _open_edit_dialog(emp: dict):
    """Open dialog to edit budget, model, schedule, and other parameters."""
    spec = emp.get("spec", {})
    models = _fetch_top_models()
    from auton.models import Cadence

    dialog = ui.dialog()

    with dialog:
        with ui.card().classes("w-full max-w-xl p-6"):
            ui.label(f"Edit {emp.get('name', 'Employee')}") \
                .classes("text-lg font-bold mb-4")

            # Model selector
            current_model = spec.get("model", "openrouter/anthropic/claude-sonnet-4-6")
            model_options = {m["id"]: f"{m['name']} ({m['ctx']//1000}k, ${m['price']*1e6:.1f}/M)" for m in models}
            if current_model not in model_options:
                model_options[current_model] = current_model

            model_select = ui.select(
                label="Model",
                options=model_options,
                value=current_model,
            ).classes("w-full mb-3").props("outlined dense")

            # Schedule section
            ui.label("Schedule").classes("text-sm font-semibold text-slate-700 mt-2 mb-1")

            cadence_options = {c.value: c.label for c in Cadence}
            cadence_select = ui.select(
                label="Cadence (heartbeat interval)",
                options=cadence_options,
                value=emp.get("cadence", Cadence.NONE.value),
            ).classes("w-full mb-3").props("outlined dense")

            with ui.row().classes("w-full gap-3 mb-3"):
                work_start_input = ui.input(
                    label="Work time start",
                    value=emp.get("work_time_start", "09:00"),
                ).classes("flex-1").props("outlined dense type=time")
                work_end_input = ui.input(
                    label="Work time end",
                    value=emp.get("work_time_end", "17:00"),
                ).classes("flex-1").props("outlined dense type=time")

            ui.separator().classes("mb-2")
            ui.label("Budget & Execution").classes("text-sm font-semibold text-slate-700 mb-1")

            # Budget
            monthly_budget = emp.get("monthly_budget_usd", 0)
            budget_input = ui.number(
                label="Monthly Budget (USD, 0 = unlimited)",
                value=monthly_budget,
                min=0, max=10000, step=10,
            ).classes("w-full mb-3").props("outlined dense")

            # Token budget per task
            max_tokens = spec.get("max_tokens") or 0
            tokens_input = ui.number(
                label="Token Budget Per Task (0 = unlimited)",
                value=max_tokens,
                min=0, max=1_000_000_000, step=100_000,
            ).classes("w-full mb-3").props("outlined dense")

            # Max turns per task
            max_turns = spec.get("max_turns", 30)
            turns_input = ui.number(
                label="Max Turns Per Task",
                value=max_turns,
                min=1, max=500, step=5,
            ).classes("w-full mb-3").props("outlined dense")

            # Temperature
            temperature = spec.get("temperature", 0.7)
            temp_input = ui.number(
                label="Temperature",
                value=temperature,
                min=0, max=2.0, step=0.1,
                format="%.1f",
            ).classes("w-full mb-3").props("outlined dense")

            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

                async def save_changes():
                    db = _get_db()
                    if not db:
                        ui.notify("Database not available", type="negative")
                        return

                    # Update spec
                    new_spec = dict(spec)
                    new_spec["model"] = model_select.value
                    new_spec["max_turns"] = int(turns_input.value)
                    new_spec["max_tokens"] = int(tokens_input.value) or None
                    new_spec["temperature"] = float(temp_input.value)

                    # Update profile
                    employee_data = dict(emp)
                    employee_data["spec"] = new_spec
                    employee_data["monthly_budget_usd"] = float(budget_input.value)
                    employee_data["cadence"] = cadence_select.value
                    employee_data["work_time_start"] = work_start_input.value
                    employee_data["work_time_end"] = work_end_input.value

                    await db.save_employee(employee_data)

                    # Update in-memory node if loaded in registry
                    registry = _get_registry()
                    if registry:
                        node = registry.resolve(emp.get("id", ""))
                        if node:
                            from auton.models import AgentSpec
                            node.spec = AgentSpec(**new_spec)
                            if node.profile:
                                node.profile["monthly_budget_usd"] = float(budget_input.value)
                                node.profile["cadence"] = cadence_select.value
                                node.profile["work_time_start"] = work_start_input.value
                                node.profile["work_time_end"] = work_end_input.value

                    dialog.close()
                    ui.notify("Settings saved", type="positive")
                    ui.navigate.reload()

                ui.button("Save", icon="save", on_click=save_changes) \
                    .props("unelevated no-caps color=primary")

    dialog.open()


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

                        from auton.models import AgentSpec
                        spec = AgentSpec(
                            name=cand["name"],
                            description=cand.get("title", ""),
                            system_prompt=cand.get("system_prompt", "You are a helpful assistant."),
                            goal="Awaiting task assignment",
                            tools=tools,
                            model="openrouter/anthropic/claude-sonnet-4-6",
                            max_turns=50,
                            max_tokens=500_000_000,
                        )

                        # Copy face image from candidate ID to employee ID
                        emp_id = f"emp-{uuid.uuid4().hex[:8]}"
                        avatar_url = cand.get("avatar_url")
                        if avatar_url and cand.get("_candidate_id"):
                            cand_id = cand["_candidate_id"]
                            # Try jpg first (new format), then png (legacy)
                            for ext in ("jpg", "png"):
                                src = FACES_DIR / f"{cand_id}.{ext}"
                                if src.exists():
                                    dst = FACES_DIR / f"{emp_id}.{ext}"
                                    dst.write_bytes(src.read_bytes())
                                    avatar_url = f"/static/dashboard/faces/{emp_id}.{ext}"
                                    break

                        profile = {
                            "id": emp_id,
                            "name": cand["name"],
                            "title": cand.get("title", ""),
                            "personality": cand.get("personality", ""),
                            "strengths": cand.get("strengths", []),
                            "avatar_emoji": cand.get("avatar_emoji", "🤖"),
                            "avatar_url": avatar_url,
                            "appearance": cand.get("appearance", ""),
                            "status": "waiting_for_input",
                            "tool_bundles": bundles,
                            "spec": spec.model_dump(),
                            "hired_at": now,
                            "last_active": None,
                            "monthly_budget_usd": 200.0,
                            "budget_spent_usd": 0.0,
                            "budget_period_start": now,
                            "cadence": "none",
                            "work_time_start": "09:00",
                            "work_time_end": "17:00",
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
# Deliverable format options
# ---------------------------------------------------------------------------

DELIVERABLE_FORMATS = ["MD", "CSV", "PDF", "Excel", "Word", "Custom"]


# ---------------------------------------------------------------------------
# Task execution helpers
# ---------------------------------------------------------------------------

async def _start_next_task(agent: dict) -> bool:
    """Pick the next queued task for an agent and spawn a session.

    Returns True if a task was started.
    """
    db = _get_db()
    registry = _get_registry()
    if not db or not registry:
        return False

    agent_id = agent.get("id", "")
    queued = await db.list_tasks(agent_id, status="queued")
    if not queued:
        return False

    task = queued[0]

    # Build goal from task definition + deliverables
    goal = task["definition"]
    deliverables = task.get("deliverables", [])
    if deliverables:
        lines = [f"- {d['description']} ({d['format']})" for d in deliverables]
        goal += "\n\nExpected deliverables:\n" + "\n".join(lines)

    from auton.models import AgentSpec, SpawnRequest

    spec_data = agent.get("spec", {})
    spec = AgentSpec(**{**spec_data, "goal": goal})
    spec.metadata = {**spec.metadata, "agent_id": agent_id, "task_id": task["id"]}

    req = SpawnRequest(spec=spec)
    try:
        node = registry.spawn(req)
        await db.update_task_status(task["id"], "active", session_id=node.id)

        # Update last_active
        emp_data = await db.get_employee(agent_id)
        if emp_data:
            emp_data["last_active"] = datetime.now(timezone.utc).isoformat()
            await db.save_employee(emp_data)

        return True
    except Exception as e:
        logger.error(f"Failed to start task {task['id']}: {e}")
        return False


async def _stop_active_tasks(agent: dict) -> None:
    """Stop all active task sessions for an agent."""
    db = _get_db()
    registry = _get_registry()
    if not db or not registry:
        return

    agent_id = agent.get("id", "")
    active = await db.list_tasks(agent_id, status="active")
    for task in active:
        session_id = task.get("session_id")
        if session_id:
            try:
                registry.terminate(session_id)
            except Exception:
                pass
        # Requeue the task
        await db.update_task_status(task["id"], "queued")


# ---------------------------------------------------------------------------
# Add Task dialog
# ---------------------------------------------------------------------------

def _open_add_task_dialog(agent: dict):
    """Open a dialog to add a task with deliverables to a persistent agent."""

    dialog = ui.dialog()
    deliverables_list: list[dict] = []
    deliverable_rows: list = []

    with dialog:
        with ui.card().classes("w-full max-w-xl p-6"):
            ui.label(f"Add Task — {agent.get('name', '?')}") \
                .classes("text-lg font-bold mb-4")

            task_input = ui.textarea(
                label="Task definition",
                placeholder="Describe what needs to be done...",
            ).classes("w-full mb-4").props("outlined autogrow rows=4")

            # Deliverables section
            ui.label("Deliverables").classes("text-sm font-semibold text-slate-700 mb-2")
            deliverables_container = ui.column().classes("w-full gap-2 mb-3")

            def add_deliverable_row(desc: str = "", fmt: str = "MD"):
                entry = {"description": desc, "format": fmt}
                deliverables_list.append(entry)
                idx = len(deliverables_list) - 1

                with deliverables_container:
                    row = ui.row().classes("w-full items-center gap-2")
                    deliverable_rows.append(row)
                    with row:
                        desc_input = ui.input(
                            placeholder="What to deliver...",
                            value=desc,
                        ).classes("flex-grow").props("outlined dense")

                        fmt_select = ui.select(
                            options=DELIVERABLE_FORMATS,
                            value=fmt,
                        ).classes("w-28").props("outlined dense")

                        def make_update_desc(i):
                            def update(e):
                                deliverables_list[i]["description"] = e.value
                            return update

                        def make_update_fmt(i):
                            def update(e):
                                deliverables_list[i]["format"] = e.value
                            return update

                        desc_input.on("update:model-value", make_update_desc(idx))
                        fmt_select.on("update:model-value", make_update_fmt(idx))

                        def make_remove(i, r):
                            def remove():
                                deliverables_list[i] = None  # mark as removed
                                r.set_visibility(False)
                            return remove

                        ui.button(icon="close", on_click=make_remove(idx, row)) \
                            .props("flat round dense size=sm").classes("text-slate-400")

            ui.button("Add deliverable", icon="add",
                      on_click=lambda: add_deliverable_row()) \
                .props("flat dense no-caps size=sm").classes("text-teal-600 mb-2")

            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

                async def submit_task():
                    definition = task_input.value.strip()
                    if not definition:
                        ui.notify("Please describe the task", type="warning")
                        return

                    # Collect non-removed deliverables
                    final_deliverables = [
                        d for d in deliverables_list
                        if d is not None and d.get("description", "").strip()
                    ]

                    db = _get_db()
                    if not db:
                        ui.notify("Database not available", type="negative")
                        return

                    agent_id = agent.get("id", "")
                    await db.create_task(agent_id, definition, final_deliverables)

                    # Auto-start if no active task is running
                    active = await db.list_tasks(agent_id, status="active")
                    if not active:
                        started = await _start_next_task(agent)
                        if started:
                            ui.notify("Task added and started", type="positive")
                        else:
                            ui.notify("Task queued", type="positive")
                    else:
                        ui.notify("Task queued — agent is busy", type="info")

                    dialog.close()
                    ui.navigate.reload()

                ui.button("Add Task", icon="add_task", on_click=submit_task) \
                    .props("unelevated no-caps color=primary")

    dialog.open()
