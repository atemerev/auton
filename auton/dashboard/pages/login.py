"""Login page."""

from typing import Optional
from fastapi import Request
from starlette.responses import Response, RedirectResponse
from nicegui import ui
from ..sessions import get_user_from_request
from ..layouts.public import render as public_layout
from ..keycloak_client import KEYCLOAK_URL, KEYCLOAK_REALM, KEYCLOAK_CLIENT_ID


def render_login(request: Request, version: str = "") -> Optional[Response]:
    if get_user_from_request(request):
        return RedirectResponse("/agents-dashboard")

    def content():
        with ui.element("form") \
                .props('method="post" action="/auth/login"') \
                .classes("w-full column items-stretch gap-4"):

            ui.input("Email") \
                .props('name="email" type="email" autocomplete="username" outlined dense') \
                .classes("w-full")

            ui.input("Password", password=True, password_toggle_button=True) \
                .props('name="password" type="password" autocomplete="current-password" outlined dense') \
                .classes("w-full")

            with ui.row().classes("w-full justify-end -mt-3"):
                ui.link("Forgot password?", "/forgot-password").classes("text-sm text-primary")

            ui.button("Sign in", color="primary") \
                .props('type="submit" unelevated class=full-width') \
                .classes("w-full mt-2")

        # OAuth via Keycloak
        base_url = f"{request.url.scheme}://{request.url.netloc}"
        oauth_url = (
            f"{KEYCLOAK_URL}realms/{KEYCLOAK_REALM}/protocol/openid-connect/auth"
            f"?client_id={KEYCLOAK_CLIENT_ID}"
            f"&redirect_uri={base_url}/auth/callback"
            f"&response_type=code&scope=openid%20email%20profile"
        )
        with ui.row().classes("w-full justify-center mt-4"):
            with ui.link(target=oauth_url).classes("no-underline w-full"):
                ui.button("Continue with Google", icon="login") \
                    .props("outline color=grey-8").classes("w-full")

        with ui.row().classes("w-full justify-center items-center mt-6 text-sm gap-1"):
            ui.label("Don't have an account?")
            ui.link("Sign up", "/register").classes("text-primary font-medium")

        # Error messages
        error = request.query_params.get("error")
        error_messages = {
            "invalid": "Authentication failed. Check your email/password.",
            "connection_error": "Cannot connect to authentication service.",
            "server": "Something went wrong. Please try again.",
            "oauth_failed": "OAuth authentication failed.",
        }
        if error and error in error_messages:
            ui.label(error_messages[error]).classes("text-red-500 text-sm mt-4 text-center w-full")

        status = request.query_params.get("status")
        status_messages = {
            "registered": "Registration successful! Check your email to verify.",
            "reset_success": "Password reset. Please log in.",
        }
        if status and status in status_messages:
            ui.label(status_messages[status]).classes("text-green-600 text-sm mt-4 text-center w-full")

    def footer():
        if version:
            with ui.row().classes("w-full justify-center mt-4"):
                ui.label(version).classes("text-xs text-slate-400")

    return public_layout(content, footer=footer)
