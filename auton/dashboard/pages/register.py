"""Registration page."""

from typing import Optional
from fastapi import Request
from starlette.responses import Response
from nicegui import ui
import pycountry
from ..layouts.public import render as public_layout
from ..keycloak_client import KEYCLOAK_URL, KEYCLOAK_REALM, KEYCLOAK_CLIENT_ID


def render_register(request: Request) -> Optional[Response]:
    def content():
        countries = {
            country.alpha_2: country.name
            for country in sorted(pycountry.countries, key=lambda c: c.name)
        }
        model = {"country_code": None}

        ui.label("Create an Account").classes("text-xl font-semibold mb-6 text-center")

        with ui.element("form") \
                .props('method="post" action="/auth/register"') \
                .classes("w-full column items-stretch gap-4"):

            ui.input("Full Name", validation={"Required": lambda v: v and v.strip()}) \
                .props('name="full_name" autocomplete="name" outlined dense').classes("w-full")

            ui.input("Email", validation={"Valid email required": lambda v: v and "@" in v}) \
                .props('name="email" type="email" autocomplete="email" outlined dense').classes("w-full")

            ui.select(countries, label="Country", with_input=True,
                      validation={"Required": lambda v: v is not None}) \
                .props("outlined dense").classes("w-full") \
                .bind_value(model, "country_code")

            ui.input().props('name="country"').style("display: none").bind_value_from(model, "country_code")

            ui.input("Organization (Optional)") \
                .props('name="organization" outlined dense').classes("w-full")

            ui.input("Title (Optional)") \
                .props('name="title" outlined dense').classes("w-full")

            ui.input("Password", password=True, password_toggle_button=True) \
                .props('name="password" autocomplete="new-password" outlined dense').classes("w-full")

            ui.input("Confirm Password", password=True, password_toggle_button=True) \
                .props('name="confirm_password" autocomplete="new-password" outlined dense').classes("w-full")

            ui.button("Register", color="primary") \
                .props('type="submit" unelevated class=full-width').classes("w-full mt-2")

        # OAuth
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
            ui.label("Already have an account?")
            ui.link("Sign in", "/login").classes("text-primary font-medium")

        error = request.query_params.get("error")
        error_messages = {
            "mismatch": "Passwords do not match.",
            "weak_password": "Password too weak (min 8 characters).",
            "exists": "A user with this email already exists.",
            "server": "An unexpected error occurred.",
        }
        if error and error in error_messages:
            ui.label(error_messages[error]).classes("text-red-500 text-sm mt-4 text-center w-full")

    return public_layout(content)
