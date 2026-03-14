"""Welcome page — shown after first login, requires ToS acceptance."""

from typing import Optional
from fastapi import Request
from starlette.responses import Response, RedirectResponse
from nicegui import ui
from ..sessions import get_user_from_request
from ..layouts.public import render as public_layout


def render_welcome(request: Request) -> Optional[Response]:
    user = get_user_from_request(request)
    if not user:
        return RedirectResponse("/login")
    if user.tos_confirmed:
        return RedirectResponse("/agents-dashboard")

    def content():
        ui.label("Welcome to Auton").classes("text-2xl font-bold mb-4")
        ui.label(
            "Auton is a control plane for autonomous AI agents. "
            "Before you get started, please accept our terms of service."
        ).classes("text-sm text-slate-600 mb-6")

        with ui.element("form") \
                .props('method="post" action="/auth/confirm-tos"') \
                .classes("w-full column items-stretch gap-4"):
            ui.button("I Accept — Let's Go", color="primary") \
                .props('type="submit" unelevated').classes("w-full")

    return public_layout(content)
