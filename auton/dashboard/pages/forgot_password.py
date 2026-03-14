"""Forgot password page."""

from typing import Optional
from fastapi import Request
from starlette.responses import Response
from nicegui import ui
from ..layouts.public import render as public_layout


def render_forgot_password(request: Request) -> Optional[Response]:
    def content():
        ui.label("Reset Password").classes("text-xl font-semibold mb-6 text-center")

        with ui.element("form") \
                .props('method="post" action="/auth/forgot-password"') \
                .classes("w-full column items-stretch gap-4"):

            ui.input("Email") \
                .props('name="email" type="email" outlined dense').classes("w-full")

            ui.button("Send Reset Link", color="primary") \
                .props('type="submit" unelevated').classes("w-full mt-2")

        with ui.row().classes("w-full justify-center mt-6 text-sm"):
            ui.link("Back to login", "/login").classes("text-primary")

        status = request.query_params.get("status")
        if status == "sent":
            ui.label("If an account exists with that email, a reset link has been sent.") \
                .classes("text-green-600 text-sm mt-4 text-center w-full")

    return public_layout(content)
