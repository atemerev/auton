"""Public layout for unauthenticated pages (login, register)."""

import time
from typing import Callable, Optional
from nicegui import ui

_LOGO_VERSION = int(time.time())  # cache-bust on server restart


def render(content: Callable, footer: Optional[Callable] = None) -> None:
    ui.add_body_html('<style>body { background-color: #f1f5f9; }</style>')

    with ui.column().classes("w-full min-h-screen items-center pt-20"):
        with ui.card().classes("w-full max-w-md mx-auto p-8 text-center"):
            ui.image(f"/static/dashboard/logo.svg?v={_LOGO_VERSION}").classes("w-full max-w-[280px] h-auto mx-auto mb-6")
            content()

        if footer:
            footer()
