# NiceGUI brand override — global, no-flash CSS injection.
# Call setup_brand() once before any @ui.page definitions are imported.

from nicegui import ui
import functools
import inspect
from typing import Optional

_PATCHED = False
_BRAND_STYLE = ""

PRIMARY = "#4338CA"  # Indigo-700 — Auton brand color (matches logo)


def _build_css(
    primary: str,
    secondary: Optional[str] = None,
    accent: Optional[str] = None,
    remove_ripple: bool = True,
) -> str:
    rules = []

    def add(name: str, value: Optional[str]):
        if not value:
            return
        rules.append(f".bg-{name} {{ background-color: {value} !important; border-color: {value} !important; }}")
        rules.append(f".text-{name} {{ color: {value} !important; }}")
        rules.append(f".q-btn.bg-{name} .q-btn__content {{ color: white !important; }}")
        rules.append(f".q-btn.q-btn--outline.text-{name} {{ color: {value} !important; border-color: {value} !important; }}")
        rules.append(f".q-btn.q-btn--flat.text-{name} {{ color: {value} !important; }}")

    add("primary", primary)
    add("secondary", secondary)
    add("accent", accent)

    if primary:
        rules.append(f":root {{ --q-primary: {primary} !important; }}")

    if remove_ripple:
        rules.append(".q-ripple-container { display: none !important; }")

    return '<style id="brand-override">\n' + "\n".join(rules) + "\n</style>"


def setup_brand(
    *,
    primary: str = PRIMARY,
    secondary: Optional[str] = None,
    accent: Optional[str] = None,
    remove_ripple: bool = True,
) -> None:
    """Install a global page wrapper that injects brand CSS FIRST on every page."""
    global _PATCHED, _BRAND_STYLE
    _BRAND_STYLE = _build_css(primary, secondary, accent, remove_ripple)

    # Store color kwargs for ui.colors() call
    _color_kwargs = {"primary": primary}
    if secondary:
        _color_kwargs["secondary"] = secondary
    if accent:
        _color_kwargs["accent"] = accent

    if _PATCHED:
        return

    _original_page = ui.page

    def _inject_brand_first():
        ui.colors(**_color_kwargs)  # set Quasar CSS variables properly
        ui.html(_BRAND_STYLE)
        ui.add_head_html('<link rel="icon" type="image/svg+xml" href="/static/dashboard/favicon.svg">')

    def _page_with_brand(path: str, *d_args, **d_kwargs):
        def decorator(fn):
            @_original_page(path, *d_args, **d_kwargs)
            @functools.wraps(fn)
            def wrapped(*args, **kwargs):
                _inject_brand_first()
                return fn(*args, **kwargs)
            wrapped.__signature__ = inspect.signature(fn)
            return wrapped
        return decorator

    ui.page = _page_with_brand
    _PATCHED = True
