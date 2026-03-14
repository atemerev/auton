"""Keycloak OIDC authentication client."""

import os
import logging
from typing import Optional, Dict, Any

from keycloak import KeycloakOpenID
from keycloak.exceptions import KeycloakConnectionError
from jose import jwt as jose_jwt
import httpx
import requests

KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL", "http://localhost:8080")
KEYCLOAK_REALM = os.environ.get("KEYCLOAK_REALM", "auton")
KEYCLOAK_CLIENT_ID = os.environ.get("KEYCLOAK_CLIENT_ID", "auton-app")
KEYCLOAK_CLIENT_SECRET = os.environ.get("KEYCLOAK_CLIENT_SECRET", "")

# Ensure trailing slash for proper urljoin() behavior
if not KEYCLOAK_URL.endswith("/"):
    KEYCLOAK_URL = KEYCLOAK_URL + "/"

keycloak_openid = KeycloakOpenID(
    server_url=KEYCLOAK_URL,
    client_id=KEYCLOAK_CLIENT_ID,
    realm_name=KEYCLOAK_REALM,
    client_secret_key=KEYCLOAK_CLIENT_SECRET,
    verify=True,
)


def get_keycloak_public_key() -> str:
    return (
        "-----BEGIN PUBLIC KEY-----\n"
        f"{keycloak_openid.public_key()}\n"
        "-----END PUBLIC KEY-----"
    )


def verify_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        decoded_token = jose_jwt.decode(
            token,
            get_keycloak_public_key(),
            algorithms=["RS256"],
            options={"verify_signature": True, "verify_aud": False, "verify_exp": True},
        )
        return decoded_token
    except Exception as e:
        logging.debug("Token verification failed: %s", e)
        return None


def get_user_info(token: str) -> Optional[Dict[str, Any]]:
    try:
        return keycloak_openid.userinfo(token)
    except Exception as e:
        logging.warning("Failed to get user info: %s", e)
        return None


def login_with_password(username: str, password: str) -> Optional[Dict[str, Any]]:
    try:
        token_response = keycloak_openid.token(username, password)
        logging.info("Login successful for %s", username)
        return token_response
    except (KeycloakConnectionError, httpx.ConnectError, httpx.TimeoutException,
            requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        logging.error("Cannot connect to Keycloak: %s", e)
        raise ConnectionError(f"Cannot connect to authentication service: {e}")
    except Exception as e:
        logging.warning("Login failed for %s: %s", username, e)
        return None


def refresh_token(refresh_token_str: str) -> Optional[Dict[str, Any]]:
    try:
        return keycloak_openid.refresh_token(refresh_token_str)
    except Exception as e:
        logging.debug("Token refresh failed: %s", e)
        return None


def logout(refresh_token_str: str) -> bool:
    try:
        keycloak_openid.logout(refresh_token_str)
        return True
    except Exception:
        return False


def get_authorization_url(redirect_uri: str, state: Optional[str] = None) -> str:
    return keycloak_openid.auth_url(
        redirect_uri=redirect_uri,
        scope="openid email profile",
        state=state,
    )


def exchange_code_for_token(code: str, redirect_uri: str) -> Optional[Dict[str, Any]]:
    try:
        return keycloak_openid.token(
            grant_type="authorization_code",
            code=code,
            redirect_uri=redirect_uri,
        )
    except Exception as e:
        logging.warning("Code exchange failed: %s", e)
        return None


async def register_user(
    email: str, password: str, full_name: str = "",
    country: str = "", organization: str = "", title: str = "",
) -> Optional[str]:
    try:
        async with httpx.AsyncClient() as client:
            token_response = await client.post(
                f"{KEYCLOAK_URL}realms/master/protocol/openid-connect/token",
                data={
                    "username": "admin",
                    "password": "admin",
                    "grant_type": "password",
                    "client_id": "admin-cli",
                },
            )
            if token_response.status_code != 200:
                logging.error("Failed to get admin token: %s", token_response.text)
                return None

            admin_token = token_response.json()["access_token"]
            name_parts = full_name.split(" ", 1)
            first_name = name_parts[0] if name_parts else ""
            last_name = name_parts[1] if len(name_parts) > 1 else ""

            create_response = await client.post(
                f"{KEYCLOAK_URL}admin/realms/{KEYCLOAK_REALM}/users",
                headers={
                    "Authorization": f"Bearer {admin_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "username": email,
                    "email": email,
                    "firstName": first_name,
                    "lastName": last_name,
                    "enabled": True,
                    "emailVerified": False,
                    "credentials": [{"type": "password", "value": password, "temporary": False}],
                    "attributes": {
                        "country": [country] if country else [],
                        "organization": [organization] if organization else [],
                        "title": [title] if title else [],
                    },
                },
            )

            if create_response.status_code == 201:
                location = create_response.headers.get("Location", "")
                user_id = location.split("/")[-1] if location else None
                logging.info("Created user %s in Keycloak", email)
                return user_id
            elif create_response.status_code == 409:
                logging.warning("User %s already exists", email)
                return None
            else:
                logging.error("Registration failed: %s %s", create_response.status_code, create_response.text)
                return None
    except Exception as e:
        logging.error("Registration error: %s", e)
        return None


async def send_verify_email(user_id: str) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            token_response = await client.post(
                f"{KEYCLOAK_URL}realms/master/protocol/openid-connect/token",
                data={"username": "admin", "password": "admin", "grant_type": "password", "client_id": "admin-cli"},
            )
            if token_response.status_code != 200:
                return False
            admin_token = token_response.json()["access_token"]
            response = await client.put(
                f"{KEYCLOAK_URL}admin/realms/{KEYCLOAK_REALM}/users/{user_id}/send-verify-email",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            return response.status_code in [200, 204]
    except Exception as e:
        logging.error("Error sending verification email: %s", e)
        return False


async def send_reset_password_email(email: str) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            token_response = await client.post(
                f"{KEYCLOAK_URL}realms/master/protocol/openid-connect/token",
                data={"username": "admin", "password": "admin", "grant_type": "password", "client_id": "admin-cli"},
            )
            if token_response.status_code != 200:
                return False
            admin_token = token_response.json()["access_token"]
            search_response = await client.get(
                f"{KEYCLOAK_URL}admin/realms/{KEYCLOAK_REALM}/users",
                headers={"Authorization": f"Bearer {admin_token}"},
                params={"email": email, "exact": "true"},
            )
            if search_response.status_code != 200:
                return False
            users = search_response.json()
            if not users:
                return True  # Don't leak user existence
            user_id = users[0]["id"]
            reset_response = await client.put(
                f"{KEYCLOAK_URL}admin/realms/{KEYCLOAK_REALM}/users/{user_id}/execute-actions-email",
                headers={"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"},
                json=["UPDATE_PASSWORD"],
            )
            return reset_response.status_code in [200, 204]
    except Exception as e:
        logging.error("Error sending password reset: %s", e)
        return False
