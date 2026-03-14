"""Auth API endpoints — login, register, profile, Stripe webhooks."""

import logging
import os
from typing import Optional

import stripe
from fastapi import APIRouter, Form, Request, Depends, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, Response

from ..constants import TIER_FREE, TIER_BASIC, TIER_FOUNDER, TIER_CREDITS
from ..sessions import set_session, clear_session, get_user_from_request
from ..keycloak_client import (
    login_with_password, verify_token, get_user_info,
    exchange_code_for_token,
    register_user as kc_register_user, send_verify_email, send_reset_password_email,
    KEYCLOAK_URL, KEYCLOAK_REALM,
)
from ..db_client import get_db
from ..db_models import User

# Stripe configuration (optional — graceful if not set)
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

STRIPE_PRICE_ID_BASIC = os.environ.get("STRIPE_PRICE_ID_BASIC", "")
STRIPE_PRICE_ID_FOUNDER = os.environ.get("STRIPE_PRICE_ID_FOUNDER", "")

PRICE_ID_TO_TIER = {}
if STRIPE_PRICE_ID_BASIC:
    PRICE_ID_TO_TIER[STRIPE_PRICE_ID_BASIC] = {"tier": TIER_BASIC, "credits": TIER_CREDITS[TIER_BASIC]}
if STRIPE_PRICE_ID_FOUNDER:
    PRICE_ID_TO_TIER[STRIPE_PRICE_ID_FOUNDER] = {"tier": TIER_FOUNDER, "credits": TIER_CREDITS[TIER_FOUNDER]}

router = APIRouter()


async def get_current_user_id(request: Request) -> str:
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return str(user.id)


@router.post("/auth/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        token_response = login_with_password(email, password)
        if not token_response:
            return RedirectResponse(url="/login?error=invalid", status_code=303)

        access_token = token_response["access_token"]
        refresh_token = token_response["refresh_token"]

        decoded_token = verify_token(access_token)
        if not decoded_token:
            return RedirectResponse(url="/login?error=invalid", status_code=303)

        user_id = decoded_token.get("sub")
        user_email = decoded_token.get("email", email)

        with get_db() as db:
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                user_info = get_user_info(access_token)
                user = User(
                    id=user_id,
                    email=user_email,
                    full_name=user_info.get("name", "") if user_info else "",
                    credits=50,
                    tier=0,
                    tos_confirmed=False,
                )
                db.add(user)
                db.commit()
                db.refresh(user)

            if STRIPE_SECRET_KEY:
                await _sync_user_subscription_status(user.id, db)
                db.refresh(user)

            tos_confirmed = user.tos_confirmed

        redirect_url = "/agents-dashboard" if tos_confirmed else "/welcome"
        resp = RedirectResponse(url=redirect_url, status_code=303)
        set_session(resp, access_token, refresh_token, request)
        return resp

    except ConnectionError:
        return RedirectResponse(url="/login?error=connection_error", status_code=303)
    except Exception as e:
        logging.exception("Login error: %s", e)
        return RedirectResponse(url="/login?error=server", status_code=303)


@router.post("/auth/register")
async def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    full_name: str = Form(...),
    country: str = Form(...),
    organization: str = Form(None),
    title: str = Form(None),
):
    if password != confirm_password:
        return RedirectResponse(url="/register?error=mismatch", status_code=303)
    if len(password) < 8:
        return RedirectResponse(url="/register?error=weak_password", status_code=303)

    try:
        user_id = await kc_register_user(
            email=email, password=password, full_name=full_name,
            country=country, organization=organization or "", title=title or "",
        )
        if not user_id:
            return RedirectResponse(url="/register?error=exists", status_code=303)

        await send_verify_email(user_id)

        with get_db() as db:
            user = User(
                id=user_id, email=email, full_name=full_name,
                country=country, organization=organization, title=title,
                credits=50, tier=0, tos_confirmed=False,
            )
            db.add(user)
            db.commit()

        return RedirectResponse(url="/login?status=registered", status_code=303)

    except Exception as e:
        logging.exception("Registration error: %s", e)
        return RedirectResponse(url="/register?error=server", status_code=303)


@router.get("/auth/callback", response_class=HTMLResponse, name="google_callback")
async def google_callback(request: Request, code: Optional[str] = None, error: Optional[str] = None):
    if error or not code:
        return RedirectResponse(url="/login?error=oauth_failed", status_code=303)
    try:
        redirect_uri = str(request.url_for("google_callback"))
        token_response = exchange_code_for_token(code, redirect_uri)
        if not token_response:
            return RedirectResponse(url="/login?error=oauth_failed", status_code=303)

        access_token = token_response["access_token"]
        refresh_token = token_response["refresh_token"]
        decoded_token = verify_token(access_token)
        if not decoded_token:
            return RedirectResponse(url="/login?error=oauth_failed", status_code=303)

        user_id = decoded_token.get("sub")
        user_email = decoded_token.get("email")

        with get_db() as db:
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                user_info = get_user_info(access_token)
                user = User(
                    id=user_id, email=user_email,
                    full_name=user_info.get("name", "") if user_info else "",
                    credits=50, tier=0, tos_confirmed=False,
                )
                db.add(user)
                db.commit()
                db.refresh(user)

            redirect_url = "/agents-dashboard" if user.tos_confirmed else "/welcome"
            resp = RedirectResponse(url=redirect_url, status_code=303)
            set_session(resp, access_token, refresh_token, request)
            return resp
    except Exception as e:
        logging.exception("OAuth callback error: %s", e)
        return RedirectResponse(url="/login?error=server", status_code=303)


@router.post("/auth/update-profile", name="update_profile")
async def update_profile(
    request: Request,
    full_name: str = Form(...),
    country: str = Form(...),
    organization: str = Form(None),
    title: str = Form(None),
):
    user = get_user_from_request(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    try:
        with get_db() as db:
            db_user = db.query(User).filter(User.id == user.id).first()
            if db_user:
                db_user.full_name = full_name
                db_user.country = country
                db_user.organization = organization
                db_user.title = title
                db.commit()
        return RedirectResponse(url="/account-settings?status=updated", status_code=303)
    except Exception:
        return RedirectResponse(url="/account-settings?error=failed", status_code=303)


@router.post("/auth/confirm-tos", name="confirm_tos")
async def confirm_tos(user_id: str = Depends(get_current_user_id)):
    try:
        with get_db() as db:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                user.tos_confirmed = True
                db.commit()
        return RedirectResponse(url="/agents-dashboard", status_code=303)
    except Exception:
        return RedirectResponse(url="/welcome?error=tos_agreement_failed", status_code=303)


@router.get("/auth/logout")
async def logout(request: Request):
    resp = RedirectResponse(url="/login", status_code=303)
    clear_session(resp, request)
    return resp


# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------


def _update_user_subscription(user_id: str, new_tier: int, stripe_customer_id: str, credits_to_set: Optional[int] = None):
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return
        user.tier = new_tier
        user.stripe_customer_id = stripe_customer_id
        if credits_to_set is not None:
            user.credits = credits_to_set
        db.commit()


@router.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_SECRET_KEY:
        return Response(status_code=501, content="Stripe not configured")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig_header, secret=STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return Response(status_code=400)

    if event["type"] == "checkout.session.completed":
        session_from_event = event["data"]["object"]
        try:
            session = stripe.checkout.Session.retrieve(session_from_event.get("id"), expand=["line_items"])
            user_id = session.get("client_reference_id")
            if not user_id:
                return Response(status_code=400)
            price_id = session.line_items.data[0].price.id
            if price_id not in PRICE_ID_TO_TIER:
                return Response(status_code=400)
            new_tier_info = PRICE_ID_TO_TIER[price_id]
            _update_user_subscription(user_id, new_tier_info["tier"], session.get("customer"), new_tier_info["credits"])
        except Exception:
            logging.exception("Error processing Stripe webhook")
            return Response(status_code=500)

    elif event["type"] in ["customer.subscription.updated", "customer.subscription.deleted"]:
        subscription = event["data"]["object"]
        stripe_customer_id = subscription.get("customer")
        if not stripe_customer_id:
            return Response(status_code=400)

        if event["type"] == "customer.subscription.deleted" or subscription.get("status") == "canceled":
            new_tier = TIER_FREE
        else:
            price_id = subscription.get("items", {}).get("data", [{}])[0].get("price", {}).get("id")
            new_tier = PRICE_ID_TO_TIER.get(price_id, {}).get("tier", TIER_FREE)

        with get_db() as db:
            user = db.query(User).filter(User.stripe_customer_id == stripe_customer_id).first()
            if user:
                user.tier = new_tier
                db.commit()

    elif event["type"] == "invoice.paid":
        invoice = event["data"]["object"]
        stripe_customer_id = invoice.get("customer")
        if not invoice.get("subscription") or not stripe_customer_id:
            return Response(status_code=200)
        price_id = None
        if invoice.get("lines") and invoice["lines"].get("data"):
            price = invoice["lines"]["data"][0].get("price")
            if price:
                price_id = price.get("id")
        if price_id in PRICE_ID_TO_TIER:
            credits_to_add = PRICE_ID_TO_TIER[price_id]["credits"]
            with get_db() as db:
                user = db.query(User).filter(User.stripe_customer_id == stripe_customer_id).first()
                if user:
                    user.credits = credits_to_add
                    db.commit()

    return Response(status_code=200)


async def _sync_user_subscription_status(user_id, db):
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return
        if not user.stripe_customer_id:
            if user.tier > 0:
                user.tier = 0
                db.commit()
            return
        subscriptions = stripe.Subscription.list(customer=user.stripe_customer_id, status="active", limit=1)
        if subscriptions.data:
            price_id = subscriptions.data[0].get("items", {}).get("data", [{}])[0].get("price", {}).get("id")
            stripe_tier_info = PRICE_ID_TO_TIER.get(price_id)
            if stripe_tier_info and user.tier != stripe_tier_info["tier"]:
                user.tier = stripe_tier_info["tier"]
                db.commit()
        else:
            if user.tier > 0:
                user.tier = 0
                db.commit()
    except Exception as e:
        logging.error("Failed to sync subscription: %s", e)


@router.post("/auth/cancel-subscription", name="cancel_subscription")
async def cancel_subscription(user_id: str = Depends(get_current_user_id)):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=501, detail="Stripe not configured")
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.stripe_customer_id:
            raise HTTPException(status_code=404, detail="No Stripe customer")
        subscriptions = stripe.Subscription.list(customer=user.stripe_customer_id, status="active", limit=1)
        if not subscriptions.data:
            raise HTTPException(status_code=404, detail="No active subscription")
        stripe.Subscription.modify(subscriptions.data[0].id, cancel_at_period_end=True)
        return {"message": "Cancellation scheduled"}


@router.post("/auth/delete-account", name="delete_account")
async def delete_account(request: Request, user_id: str = Depends(get_current_user_id)):
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404)
        if user.stripe_customer_id and STRIPE_SECRET_KEY:
            try:
                subs = stripe.Subscription.list(customer=user.stripe_customer_id, status="active", limit=1)
                if subs.data:
                    stripe.Subscription.delete(subs.data[0].id)
            except Exception:
                logging.exception("Stripe error during account deletion")
        db.delete(user)
        db.commit()
    resp = Response(status_code=200, content='{"status": "ok"}', media_type="application/json")
    clear_session(resp, request)
    return resp


@router.post("/auth/forgot-password")
async def forgot_password(email: str = Form(...)):
    await send_reset_password_email(email)
    return RedirectResponse(url="/forgot-password?status=sent", status_code=303)
