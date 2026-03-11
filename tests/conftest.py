"""Shared fixtures for auton tests."""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure tests don't depend on real API keys."""
    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.delenv("AUTON_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def mock_drift_judge(monkeypatch):
    """Mock drift judge to avoid real API calls in tests."""
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"coherence": 0.9, "assessment": "on track"}'
    monkeypatch.setattr(
        "auton.drift.acompletion",
        AsyncMock(return_value=mock_response),
    )


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
