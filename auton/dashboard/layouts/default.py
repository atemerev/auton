"""Default layout for authenticated pages — header with navigation."""

import logging
from typing import Callable, Optional
from fastapi import Request
from starlette.responses import Response, RedirectResponse
from nicegui import ui
from ..constants import TIER_FREE, TIER_FOUNDER, TIER_NAMES
from ..sessions import get_user_and_tokens


def render(content: Callable, request: Request) -> Optional[Response]:
    user, toks = get_user_and_tokens(request)
    if not user or not toks:
        return RedirectResponse("/login")

    if not user.tos_confirmed and request.url.path != "/welcome":
        return RedirectResponse("/welcome")

    email = user.email
    current_path = request.url.path

    with ui.header().classes("bg-white justify-between text-slate-800 items-center"):
        with ui.row().classes("items-center gap-6"):
            with ui.link(target="/"):
                ui.label("AUTON").classes("text-xl font-bold").style("color: #16203C")

            with ui.row().classes("items-center gap-2"):
                nav_items = [
                    ("/agents-dashboard", "Agents", "smart_toy"),
                    ("/monitoring", "Monitoring", "monitor_heart"),
                    ("/account-settings", "Settings", "settings"),
                ]
                for path, label, icon in nav_items:
                    with ui.link(target=path).classes("no-underline"):
                        btn = ui.button(label, icon=icon).props("flat dense no-caps")
                        if current_path == path:
                            btn.style("border-bottom: 2px solid #1DE0C8; border-radius: 0")

        with ui.row().classes("items-center gap-4"):
            current_tier = user.tier
            current_credits = user.credits
            current_tier_name = TIER_NAMES.get(current_tier, "Free")

            with ui.row().classes("items-center gap-2 text-sm"):
                ui.label(f"{current_tier_name}").classes("text-slate-600 font-medium")
                ui.separator().props("vertical inset")
                with ui.row().classes("items-center gap-1"):
                    ui.label("Credits:").classes("text-slate-600")
                    if current_tier == TIER_FOUNDER:
                        ui.html('<span class="font-bold text-xl" style="color: #1DE0C8;">∞</span>')
                    else:
                        ui.label(str(current_credits)).classes("text-slate-600")

            if current_tier == TIER_FREE:
                with ui.link(target="/account-settings?tab=subscriptions").classes("no-underline"):
                    ui.button("Upgrade", color="primary").props("dense unelevated")

            with ui.link(target="/auth/logout").classes("no-underline"):
                ui.button("Logout", icon="logout", color="primary").props("flat dense")

    content()

    return None
