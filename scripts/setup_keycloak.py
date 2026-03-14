"""Set up Keycloak user for auton dashboard."""

import httpx
import sys

KC_URL = "http://localhost:8080"

# Get admin token
r = httpx.post(
    f"{KC_URL}/realms/master/protocol/openid-connect/token",
    data={"username": "admin", "password": "admin", "grant_type": "password", "client_id": "admin-cli"},
)
if r.status_code != 200:
    print(f"Failed to get admin token: {r.status_code} {r.text}")
    sys.exit(1)

admin_token = r.json()["access_token"]
headers = {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}

# Create user
r = httpx.post(
    f"{KC_URL}/admin/realms/auton/users",
    headers=headers,
    json={
        "username": "sorhed@gmail.com",
        "email": "sorhed@gmail.com",
        "firstName": "Alexander",
        "lastName": "Temerev",
        "enabled": True,
        "emailVerified": True,
        "credentials": [{"type": "password", "value": "ChangeMe!!", "temporary": False}],
    },
)

if r.status_code == 201:
    user_id = r.headers.get("Location", "").split("/")[-1]
    print(f"User created: {user_id}")
elif r.status_code == 409:
    print("User already exists")
else:
    print(f"Failed: {r.status_code} {r.text}")
    sys.exit(1)

# Verify login works
r = httpx.post(
    f"{KC_URL}/realms/auton/protocol/openid-connect/token",
    data={
        "username": "sorhed@gmail.com",
        "password": "ChangeMe!!",
        "grant_type": "password",
        "client_id": "auton-app",
        "client_secret": "NQIrFmIzCzzc20Xi4UO0orJhUfqXYL2Z",
    },
)
if r.status_code == 200:
    print("Login verification: SUCCESS")
    token_data = r.json()
    print(f"  Access token length: {len(token_data['access_token'])}")
    print(f"  Refresh token length: {len(token_data['refresh_token'])}")
else:
    print(f"Login verification FAILED: {r.status_code} {r.text}")
