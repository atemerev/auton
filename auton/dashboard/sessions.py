"""Session management — file-based token storage with Keycloak integration."""

import logging
import threading
from typing import Dict, Optional, Tuple
from fastapi import Request, Response
import uuid
import json
import os

from .keycloak_client import verify_token, refresh_token as kc_refresh_token, get_user_info
from .db_client import get_db
from .db_models import User

SESSION_FILE = ".sessions.json"
SESSION_COOKIE = "sid"


def _load_sessions() -> Dict[str, Dict[str, str]]:
    if not os.path.exists(SESSION_FILE):
        return {}
    try:
        with open(SESSION_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        logging.warning("Could not load sessions from %s. Starting fresh.", SESSION_FILE)
        return {}


def _save_sessions():
    with open(SESSION_FILE, "w") as f:
        json.dump(SESSIONS, f, indent=2)


SESSIONS: Dict[str, Dict[str, str]] = _load_sessions()
SESSION_LOCKS: Dict[str, threading.Lock] = {sid: threading.Lock() for sid in SESSIONS}


def set_session(resp: Response, access: str, refresh: str, request: Request) -> None:
    sid = str(uuid.uuid4())
    SESSIONS[sid] = {"access": access, "refresh": refresh}
    SESSION_LOCKS[sid] = threading.Lock()
    _save_sessions()

    is_secure = request.url.scheme == "https"
    resp.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax", secure=is_secure, path="/")


def clear_session(resp: Response, request: Request) -> None:
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        SESSIONS.pop(sid, None)
        SESSION_LOCKS.pop(sid, None)
        _save_sessions()
    resp.delete_cookie(SESSION_COOKIE, path="/")


def get_user_and_tokens(request: Request) -> Tuple[Optional[User], Optional[Dict[str, str]]]:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None, None

    lock = SESSION_LOCKS.setdefault(sid, threading.Lock())

    with lock:
        tokens = SESSIONS.get(sid)
        if not tokens:
            return None, None

        try:
            decoded_token = verify_token(tokens["access"])

            if not decoded_token:
                logging.info("Access token expired, attempting refresh for session %s", sid)
                refresh_response = kc_refresh_token(tokens["refresh"])

                if refresh_response:
                    tokens["access"] = refresh_response["access_token"]
                    tokens["refresh"] = refresh_response["refresh_token"]
                    SESSIONS[sid] = tokens
                    _save_sessions()
                    decoded_token = verify_token(tokens["access"])
                else:
                    SESSIONS.pop(sid, None)
                    SESSION_LOCKS.pop(sid, None)
                    _save_sessions()
                    return None, None

            if not decoded_token:
                SESSIONS.pop(sid, None)
                SESSION_LOCKS.pop(sid, None)
                _save_sessions()
                return None, None

            user_id = decoded_token.get("sub")
            if not user_id:
                return None, None

            with get_db() as db:
                user = db.query(User).filter(User.id == user_id).first()

                if not user:
                    logging.info("User %s not in database, creating record", user_id)
                    user_info = get_user_info(tokens["access"])
                    user = User(
                        id=user_id,
                        email=decoded_token.get("email", user_info.get("email", "") if user_info else ""),
                        full_name=user_info.get("name", "") if user_info else "",
                        credits=50,
                        tier=0,
                        tos_confirmed=False,
                    )
                    db.add(user)
                    db.commit()
                    db.refresh(user)

                if user:
                    # Eagerly load attributes before session closes
                    _ = user.id, user.email, user.full_name, user.credits
                    _ = user.tier, user.tos_confirmed, user.stripe_customer_id
                    _ = user.created_at, user.updated_at, user.country
                    _ = user.organization, user.title
                    db.expunge(user)

                return user, tokens

        except Exception as e:
            logging.warning("get_user_and_tokens failed for session %s: %s", sid, e)
            SESSIONS.pop(sid, None)
            SESSION_LOCKS.pop(sid, None)
            _save_sessions()
            return None, None


def tokens_from_request(request: Request) -> Optional[Dict[str, str]]:
    sid = request.cookies.get(SESSION_COOKIE)
    return SESSIONS.get(sid) if sid else None


def get_user_from_request(request: Request) -> Optional[User]:
    user, _ = get_user_and_tokens(request)
    return user
