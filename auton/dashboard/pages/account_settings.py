"""Account settings — profile, API keys, subscription management."""

import os
import logging
from typing import Optional
from urllib.parse import quote
from datetime import datetime

import stripe
from fastapi import Request
from starlette.responses import Response, RedirectResponse
from nicegui import ui
import pycountry

from ..constants import TIER_FREE, TIER_BASIC, TIER_FOUNDER, TIER_NAMES, TIER_CREDITS
from ..layouts.default import render as default_layout
from ..sessions import get_user_from_request
from ..db_client import get_db
from ..db_models import User, ApiKey
from ..style import PRIMARY

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def render_account_settings(request: Request) -> Optional[Response]:
    user = get_user_from_request(request)
    if not user:
        return RedirectResponse("/login")

    user_id = str(user.id)
    email = user.email
    user_properties = {
        "full_name": user.full_name,
        "country": user.country,
        "organization": user.organization,
        "title": user.title,
        "tier": user.tier,
        "credits": user.credits,
        "stripe_customer_id": user.stripe_customer_id,
    }

    # Load API keys
    api_keys_data = []
    with get_db() as db:
        keys = db.query(ApiKey).filter(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc()).all()
        for k in keys:
            api_keys_data.append({
                "id": k.id, "key_prefix": k.key_prefix, "name": k.name,
                "created_at": str(k.created_at), "last_used_at": str(k.last_used_at) if k.last_used_at else None,
                "expires_at": str(k.expires_at) if k.expires_at else None, "is_active": k.is_active,
            })

    # Load Stripe subscription details
    subscription_details = None
    if STRIPE_SECRET_KEY and user_properties.get("stripe_customer_id"):
        try:
            subscriptions = stripe.Subscription.list(
                customer=user_properties["stripe_customer_id"], status="active", limit=1
            )
            if subscriptions.data:
                sub = subscriptions.data[0]
                cancel_at_period_end = sub.get("cancel_at_period_end", False)
                sub_items_data = sub.get("items", {}).get("data")
                period_end_ts = sub_items_data[0].get("current_period_end") if sub_items_data else None
                if period_end_ts:
                    subscription_details = {
                        "cancel_at_period_end": cancel_at_period_end,
                        "current_period_end": datetime.fromtimestamp(period_end_ts).strftime("%B %d, %Y"),
                    }
        except Exception as e:
            logging.warning("Could not fetch Stripe details: %s", e)

    def content():
        # Handle query param notifications
        async def handle_query_params():
            if request.query_params.get("status") == "updated":
                ui.notification("Settings updated!", type="positive")
            elif request.query_params.get("error") == "failed":
                ui.notification("Failed to update settings.", type="negative")

        ui.timer(0.1, handle_query_params, once=True)

        with ui.column().classes("w-full max-w-4xl mx-auto p-4 sm:p-8"):
            with ui.tabs().props('align="left"').classes("w-full") as tabs:
                account_tab = ui.tab("Account")
                api_keys_tab = ui.tab("API Keys")
                subscription_tab = ui.tab("Subscription")

            initial_tab = (
                subscription_tab if request.query_params.get("tab") == "subscriptions"
                else api_keys_tab if request.query_params.get("tab") == "api-keys"
                else account_tab
            )

            with ui.tab_panels(tabs, value=initial_tab, animated=False).classes("w-full bg-transparent mt-4"):
                # --- Account Tab ---
                with ui.tab_panel(account_tab):
                    with ui.card().classes("w-full p-8"):
                        ui.label("Profile Information").classes("text-xl font-medium mb-4")
                        countries = {
                            c.alpha_2: c.name for c in sorted(pycountry.countries, key=lambda c: c.name)
                        }
                        model = {"country_code": user_properties.get("country")}

                        with ui.element("form").props('method="post" action="/auth/update-profile"') \
                                .classes("w-full flex flex-col gap-4"):
                            ui.input("Full Name", value=user_properties.get("full_name", "")) \
                                .props('name="full_name" outlined dense').classes("w-full")
                            ui.select(countries, label="Country", with_input=True) \
                                .props("outlined dense").classes("w-full") \
                                .bind_value(model, "country_code")
                            ui.input().props('name="country"').style("display: none") \
                                .bind_value_from(model, "country_code")
                            ui.input("Organization", value=user_properties.get("organization", "")) \
                                .props('name="organization" outlined dense').classes("w-full")
                            ui.input("Title", value=user_properties.get("title", "")) \
                                .props('name="title" outlined dense').classes("w-full")
                            ui.button("Save Changes", color="primary").props('type="submit"').classes("w-full mt-2")

                    with ui.card().classes("w-full p-8 mt-6 border-2 border-red-500"):
                        ui.label("Delete Account").classes("text-xl font-medium text-red-600 mb-2")
                        ui.label("This is irreversible. All data will be permanently deleted.").classes("text-sm")
                        ui.button("Delete My Account", on_click=lambda: delete_dialog.open(), color="negative") \
                            .classes("w-full mt-4")

                # --- API Keys Tab ---
                with ui.tab_panel(api_keys_tab):
                    with ui.card().classes("w-full p-6"):
                        ui.label("API Keys").classes("text-xl font-medium mb-2")
                        ui.label(
                            "Use API keys to authenticate with the Auton agent API. "
                            "Keys use the prefix 'atn_' for identification."
                        ).classes("text-sm text-slate-500 mb-4")

                        ui.button("Generate New Key", icon="add", on_click=lambda: generate_key_dialog.open(),
                                  color="primary").props("unelevated").classes("mb-4")

                        keys_container = ui.column().classes("w-full gap-2")

                        def render_keys():
                            keys_container.clear()
                            with keys_container:
                                if not api_keys_data:
                                    ui.label("No API keys yet.").classes("text-slate-400 text-sm")
                                else:
                                    for key in api_keys_data:
                                        _render_api_key_row(key, request)

                        render_keys()

                # --- Subscription Tab ---
                with ui.tab_panel(subscription_tab):
                    current_tier = user_properties.get("tier", TIER_FREE)
                    current_credits = user_properties.get("credits", 0)
                    current_tier_name = TIER_NAMES.get(current_tier, "Free")

                    with ui.card().classes("w-full p-6 mb-6"):
                        with ui.row().classes("w-full justify-between items-center text-lg"):
                            with ui.row().classes("items-baseline gap-2"):
                                ui.label(f"Plan: {current_tier_name}")
                                if subscription_details:
                                    label = "cancels" if subscription_details["cancel_at_period_end"] else "renews"
                                    ui.label(f"({label} on {subscription_details['current_period_end']})") \
                                        .classes("text-sm text-slate-500")
                            with ui.row().classes("items-center gap-2"):
                                ui.label("Credits:")
                                if current_tier == TIER_FOUNDER:
                                    ui.html('<span class="font-bold text-xl" style="color: #4338CA;">∞</span>')
                                else:
                                    ui.label(str(current_credits))

                    if STRIPE_SECRET_KEY:
                        link_basic = os.environ.get("STRIPE_PAYMENT_LINK_BASIC", "")
                        link_founder = os.environ.get("STRIPE_PAYMENT_LINK_FOUNDER", "")

                        if link_basic or link_founder:
                            safe_email = quote(email)
                            is_cancellation_pending = subscription_details and subscription_details.get("cancel_at_period_end")

                            with ui.grid(columns=2).classes("w-full gap-6"):
                                if link_basic:
                                    stripe_link_basic = f"{link_basic}?client_reference_id={user_id}&prefilled_email={safe_email}"
                                    _render_plan_card(
                                        "Basic", "CHF 15", f"{TIER_CREDITS[TIER_BASIC]} credits/month",
                                        current_tier, TIER_BASIC, stripe_link_basic,
                                        is_cancellation_pending, confirm_cancel_dialog,
                                    )
                                if link_founder:
                                    stripe_link_founder = f"{link_founder}?client_reference_id={user_id}&prefilled_email={safe_email}"
                                    _render_plan_card(
                                        "Founder", "CHF 80",
                                        "Unlimited credits, early access, founder badge",
                                        current_tier, TIER_FOUNDER, stripe_link_founder,
                                        is_cancellation_pending, confirm_cancel_dialog,
                                    )
                    else:
                        ui.label("Stripe integration not configured.").classes("text-slate-400 text-sm")

        # --- Dialogs ---
        with ui.dialog() as generate_key_dialog, ui.card().classes("p-6"):
            ui.label("Generate API Key").classes("text-lg font-medium mb-4")
            key_name_input = ui.input("Key Name (optional)").props("outlined dense").classes("w-full mb-4")

            async def do_generate_key():
                import secrets
                import hashlib

                api_key = f"atn_{secrets.token_urlsafe(48)}"
                key_hash = hashlib.sha256(api_key.encode()).hexdigest()
                key_prefix = api_key[:12]

                with get_db() as db:
                    new_key = ApiKey(
                        user_id=user_id, key_hash=key_hash, key_prefix=key_prefix,
                        name=key_name_input.value or None, is_active=True,
                    )
                    db.add(new_key)
                    db.commit()

                api_keys_data.insert(0, {
                    "id": new_key.id, "key_prefix": key_prefix, "name": key_name_input.value,
                    "created_at": "just now", "last_used_at": None, "expires_at": None, "is_active": True,
                })
                generate_key_dialog.close()

                # Show the key in a new dialog
                with ui.dialog() as show_key_dialog, ui.card().classes("p-6"):
                    ui.label("Your API Key").classes("text-lg font-medium mb-2")
                    ui.label("Save this key now. You won't see it again!").classes("text-red-500 text-sm mb-4")
                    ui.input(value=api_key).props("outlined dense readonly").classes("w-full font-mono mb-4")
                    ui.button("Copy", icon="content_copy",
                              on_click=lambda: ui.run_javascript(f'navigator.clipboard.writeText("{api_key}")')) \
                        .props("flat dense")
                    ui.button("Done", on_click=lambda: (show_key_dialog.close(), ui.navigate.reload())) \
                        .props("flat dense")
                show_key_dialog.open()

            ui.button("Generate", on_click=do_generate_key, color="primary").props("unelevated")

        with ui.dialog() as confirm_cancel_dialog, ui.card().classes("p-6"):
            ui.label("Cancel Subscription?").classes("text-lg font-medium")
            ui.label("Your subscription remains active until the end of the billing period.").classes("text-sm mb-4")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Nevermind", on_click=confirm_cancel_dialog.close).props("flat")

                async def do_cancel():
                    try:
                        import httpx
                        async with httpx.AsyncClient() as client:
                            url = f"{request.url.scheme}://{request.url.netloc}/auth/cancel-subscription"
                            resp = await client.post(url, cookies=request.cookies)
                            if resp.status_code == 200:
                                ui.notification("Subscription cancelled.", type="positive")
                                ui.navigate.reload()
                            else:
                                ui.notification("Failed to cancel.", type="negative")
                    except Exception as e:
                        ui.notification(f"Error: {e}", type="negative")
                    confirm_cancel_dialog.close()

                ui.button("Yes, Cancel", on_click=do_cancel, color="negative")

        with ui.dialog() as delete_dialog, ui.card().classes("p-6"):
            ui.label("Delete Account").classes("text-xl font-medium")
            ui.label("This will permanently delete your account. Type DELETE to confirm.") \
                .classes("text-red-600 text-sm mb-4")
            confirm_input = ui.input(placeholder="DELETE").classes("w-full")

            async def do_delete():
                if confirm_input.value != "DELETE":
                    ui.notification('Type "DELETE" to confirm.', type="negative")
                    return
                try:
                    import httpx
                    async with httpx.AsyncClient() as client:
                        url = f"{request.url.scheme}://{request.url.netloc}/auth/delete-account"
                        resp = await client.post(url, cookies=request.cookies)
                        if resp.status_code == 200:
                            ui.navigate.to("/login?message=account_deleted")
                        else:
                            ui.notification("Failed to delete account.", type="negative")
                except Exception as e:
                    ui.notification(f"Error: {e}", type="negative")

            with ui.row().classes("w-full justify-end gap-2 mt-4"):
                ui.button("Cancel", on_click=delete_dialog.close).props("flat")
                del_btn = ui.button("Confirm Deletion", on_click=do_delete, color="negative")
                del_btn.bind_enabled_from(confirm_input, "value", lambda v: v == "DELETE")

    return default_layout(content, request)


def _render_api_key_row(key: dict, request: Request):
    """Render a single API key row."""
    with ui.card().classes("w-full p-3"):
        with ui.row().classes("w-full justify-between items-center"):
            with ui.column().classes("gap-0"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(key["key_prefix"] + "...").classes("font-mono text-sm")
                    if key["name"]:
                        ui.label(key["name"]).classes("text-sm text-slate-500")
                    if key["is_active"]:
                        ui.badge("Active", color="green").props("outline")
                    else:
                        ui.badge("Disabled", color="red").props("outline")
                ui.label(f"Created: {key['created_at']}").classes("text-xs text-slate-400")

            with ui.row().classes("gap-1"):
                async def toggle(kid=key["id"]):
                    import httpx
                    async with httpx.AsyncClient() as client:
                        url = f"{request.url.scheme}://{request.url.netloc}/api/keys/{kid}/toggle"
                        await client.patch(url, cookies=request.cookies)
                    ui.navigate.reload()

                async def delete(kid=key["id"]):
                    import httpx
                    async with httpx.AsyncClient() as client:
                        url = f"{request.url.scheme}://{request.url.netloc}/api/keys/{kid}"
                        await client.delete(url, cookies=request.cookies)
                    ui.navigate.reload()

                toggle_icon = "toggle_off" if key["is_active"] else "toggle_on"
                ui.button(icon=toggle_icon, on_click=toggle).props("flat dense")
                ui.button(icon="delete", on_click=delete).props("flat dense color=red")


def _render_plan_card(name, price, features, current_tier, plan_tier, stripe_link,
                      is_cancellation_pending, cancel_dialog):
    """Render a subscription plan card."""
    is_current = current_tier == plan_tier
    ring = f"ring-4 ring-[{PRIMARY}]" if is_current else ""

    with ui.card().classes(f"w-full flex flex-col p-6 {ring}"):
        ui.label(f"{name} Plan").classes("text-xl font-semibold")
        ui.label(price).classes("text-3xl font-bold mt-2")
        ui.label("per month").classes("text-sm text-slate-500 -mt-1")
        ui.separator().classes("my-4")
        ui.label(f"✓ {features}").classes("text-base")
        ui.space()

        if is_current and not is_cancellation_pending:
            ui.button("Cancel Subscription", on_click=lambda: cancel_dialog.open()) \
                .props("flat dense color=grey no-caps").classes("mt-6 self-center")
        elif current_tier == TIER_FREE or is_cancellation_pending:
            with ui.link(target=stripe_link, new_tab=True).classes("no-underline w-full"):
                ui.button("Subscribe").props("color=primary").classes("w-full mt-6")
