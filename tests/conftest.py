"""Shared fixtures for auton tests."""

import os
import pytest


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure tests don't depend on real API keys."""
    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")


@pytest.fixture(autouse=True)
def reset_globals():
    """Reset global state between tests."""
    from auton.api import _observers, registry

    registry._roots.clear()
    registry._executors.clear()
    _observers.clear()

    yield

    # Cleanup after test
    registry._roots.clear()
    registry._executors.clear()
    _observers.clear()
