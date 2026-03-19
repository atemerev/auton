"""Dashboard entry point — mounts NiceGUI on the existing FastAPI app."""

import logging
import os
from pathlib import Path

from nicegui import ui, app
from fastapi import Request
from fastapi.staticfiles import StaticFiles
from starlette.responses import RedirectResponse

from .style import setup_brand, PRIMARY
from .sessions import tokens_from_request

STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger(__name__)


def init_dashboard(fastapi_app):
    """Initialize the NiceGUI dashboard and mount it on the FastAPI app.

    Call this from the main auton lifespan or startup.
    """
    # Setup brand colors before any page imports
    setup_brand(primary=PRIMARY, remove_ripple=True)

    # Serve static assets (logo, favicon, etc.)
    fastapi_app.mount("/static/dashboard", StaticFiles(directory=str(STATIC_DIR)), name="dashboard-static")

    # Register API routers
    from .api.auth import router as auth_router
    from .api.api_keys import router as api_keys_router
    from .api.workspace import router as workspace_router
    app.include_router(auth_router)
    app.include_router(api_keys_router)
    app.include_router(workspace_router)

    # Import page renderers
    from .pages.login import render_login
    from .pages.register import render_register
    from .pages.welcome import render_welcome
    from .pages.forgot_password import render_forgot_password
    from .pages.agents import render_agents_dashboard
    from .pages.account_settings import render_account_settings
    from .pages.team import render_team_dashboard

    # --- Page routes ---

    @ui.page("/")
    def index(request: Request):
        return RedirectResponse("/agents-dashboard" if tokens_from_request(request) else "/login")

    @ui.page("/login")
    def login_page(request: Request):
        return render_login(request, version="Auton v0.1.0")

    @ui.page("/register")
    def register_page(request: Request):
        return render_register(request)

    @ui.page("/welcome")
    def welcome_page(request: Request):
        return render_welcome(request)

    @ui.page("/forgot-password")
    def forgot_password_page(request: Request):
        return render_forgot_password(request)

    @ui.page("/agents-dashboard")
    def agents_page(request: Request):
        return render_agents_dashboard(request)

    @ui.page("/team")
    def team_page(request: Request):
        return render_team_dashboard(request)

    @ui.page("/account-settings", reconnect_timeout=0)
    def account_settings_page(request: Request):
        return render_account_settings(request)

    # Mount NiceGUI on the FastAPI app
    ui.run_with(
        fastapi_app,
        title="Auton Dashboard",
        favicon=str(STATIC_DIR / "favicon.svg"),
        storage_secret=os.environ.get("NICEGUI_SECRET", "auton-dashboard-secret"),
    )

    logger.info("Dashboard initialized on NiceGUI")
