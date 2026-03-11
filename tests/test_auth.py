"""Tests for API key authentication."""

import pytest
from httpx import ASGITransport, AsyncClient

from auton.api import app
from auton.auth import get_valid_keys


# ---------------------------------------------------------------------------
# Key parsing
# ---------------------------------------------------------------------------


def test_no_keys_configured(monkeypatch):
    monkeypatch.delenv("AUTON_API_KEY", raising=False)
    assert get_valid_keys() == set()


def test_single_key(monkeypatch):
    monkeypatch.setenv("AUTON_API_KEY", "my-secret-key")
    assert get_valid_keys() == {"my-secret-key"}


def test_multiple_keys(monkeypatch):
    monkeypatch.setenv("AUTON_API_KEY", "key1,key2,key3")
    assert get_valid_keys() == {"key1", "key2", "key3"}


def test_keys_stripped(monkeypatch):
    monkeypatch.setenv("AUTON_API_KEY", " key1 , key2 , ")
    assert get_valid_keys() == {"key1", "key2"}


# ---------------------------------------------------------------------------
# Middleware integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_always_public(monkeypatch):
    """Health endpoint never requires auth."""
    monkeypatch.setenv("AUTON_API_KEY", "secret")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_no_auth_dev_mode(monkeypatch):
    """Without AUTON_API_KEY, all endpoints are open (dev mode)."""
    monkeypatch.delenv("AUTON_API_KEY", raising=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/agents")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_auth_rejects_no_key(monkeypatch):
    """With AUTON_API_KEY set, requests without key get 401."""
    monkeypatch.setenv("AUTON_API_KEY", "secret")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/agents")
        assert resp.status_code == 401
        assert "API key" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_auth_rejects_wrong_key(monkeypatch):
    """Wrong key gets 401."""
    monkeypatch.setenv("AUTON_API_KEY", "secret")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/agents", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_accepts_x_api_key(monkeypatch):
    """Valid X-API-Key header passes auth."""
    monkeypatch.setenv("AUTON_API_KEY", "secret")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/agents", headers={"X-API-Key": "secret"})
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_auth_accepts_bearer(monkeypatch):
    """Valid Authorization: Bearer header passes auth."""
    monkeypatch.setenv("AUTON_API_KEY", "secret")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/agents", headers={"Authorization": "Bearer secret"})
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_auth_multiple_keys(monkeypatch):
    """Any of the configured keys should work."""
    monkeypatch.setenv("AUTON_API_KEY", "key1,key2")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp1 = await client.get("/agents", headers={"X-API-Key": "key1"})
        resp2 = await client.get("/agents", headers={"X-API-Key": "key2"})
        resp3 = await client.get("/agents", headers={"X-API-Key": "key3"})
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp3.status_code == 401
