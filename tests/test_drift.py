"""Tests for LLM judge drift detection."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auton.drift import (
    EMA_ALPHA,
    judge_drift,
    summarize_recent_activity,
)


# ---------------------------------------------------------------------------
# summarize_recent_activity
# ---------------------------------------------------------------------------


def test_summarize_empty_messages():
    assert summarize_recent_activity([]) == ""


def test_summarize_skips_user_messages():
    messages = [
        {"role": "user", "content": "do something"},
        {"role": "assistant", "content": "I will research this topic"},
    ]
    result = summarize_recent_activity(messages)
    assert "do something" not in result
    assert "research this topic" in result


def test_summarize_includes_assistant_and_tool():
    messages = [
        {"role": "assistant", "content": "Searching for data"},
        {"role": "tool", "content": '{"result": "found 5 items"}', "name": "web_search"},
        {"role": "assistant", "content": "I found 5 items"},
    ]
    result = summarize_recent_activity(messages)
    assert "[Assistant] Searching for data" in result
    assert "[Result: web_search]" in result
    assert "[Assistant] I found 5 items" in result


def test_summarize_includes_tool_calls_without_content():
    """Assistant messages with tool_calls but no text content."""
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "web_search", "arguments": '{"query": "test"}'}}
            ],
        },
    ]
    result = summarize_recent_activity(messages)
    assert "[Tool Call: web_search]" in result
    assert '"query": "test"' in result


def test_summarize_truncates_long_content():
    long_content = "x" * 1000
    messages = [{"role": "assistant", "content": long_content}]
    result = summarize_recent_activity(messages)
    # [Assistant] prefix + 400 chars
    assert len(result) == len("[Assistant] ") + 400


def test_summarize_respects_window():
    messages = [
        {"role": "assistant", "content": f"message {i}"} for i in range(20)
    ]
    result = summarize_recent_activity(messages, n=3)
    assert "message 17" in result
    assert "message 18" in result
    assert "message 19" in result
    assert "message 0" not in result


def test_summarize_skips_empty_content():
    messages = [
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": "real content"},
    ]
    result = summarize_recent_activity(messages)
    assert result == "[Assistant] real content"


# ---------------------------------------------------------------------------
# judge_drift — judge model interaction
# ---------------------------------------------------------------------------


def _mock_judge_response(coherence: float, assessment: str = "test"):
    """Create a mock acompletion response for the judge."""
    import json
    mock = MagicMock()
    mock.choices = [MagicMock()]
    mock.choices[0].message.content = json.dumps({
        "coherence": coherence,
        "assessment": assessment,
    })
    return mock


@pytest.mark.asyncio
async def test_judge_no_activity():
    """No messages → returns previous coherence unchanged."""
    coherence, drift, assessment = await judge_drift(
        "research competitors", [], prev_coherence=0.8
    )
    assert coherence == 0.8
    assert drift == pytest.approx(0.2)
    assert "No activity" in assessment


@pytest.mark.asyncio
async def test_judge_high_coherence():
    """Judge says agent is on track → high coherence, low drift."""
    messages = [{"role": "assistant", "content": "Researching competitor pricing"}]
    mock_resp = _mock_judge_response(0.95, "Agent is directly working on the goal")

    with patch("auton.drift.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        coherence, drift, assessment = await judge_drift(
            "research competitors", messages, prev_coherence=1.0
        )
        # EMA: 0.3 * 0.95 + 0.7 * 1.0 = 0.985
        assert coherence > 0.9
        assert drift < 0.1
        assert "on the goal" in assessment


@pytest.mark.asyncio
async def test_judge_low_coherence():
    """Judge says agent drifted → low coherence, high drift."""
    messages = [{"role": "assistant", "content": "Let me write a poem about cats"}]
    mock_resp = _mock_judge_response(0.1, "Agent is completely off-track")

    with patch("auton.drift.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        coherence, drift, assessment = await judge_drift(
            "research competitors", messages, prev_coherence=1.0
        )
        # EMA: 0.3 * 0.1 + 0.7 * 1.0 = 0.73
        assert coherence == pytest.approx(0.73, abs=0.01)
        assert drift == pytest.approx(0.27, abs=0.01)


@pytest.mark.asyncio
async def test_judge_api_failure():
    """Judge API failure → returns previous values (graceful degradation)."""
    messages = [{"role": "assistant", "content": "doing something"}]

    with patch("auton.drift.acompletion", new_callable=AsyncMock, side_effect=Exception("API down")):
        coherence, drift, assessment = await judge_drift(
            "research competitors", messages, prev_coherence=0.7
        )
        assert coherence == 0.7
        assert drift == pytest.approx(0.3)
        assert "unavailable" in assessment.lower()


@pytest.mark.asyncio
async def test_judge_invalid_json():
    """Judge returns non-JSON → graceful degradation."""
    messages = [{"role": "assistant", "content": "working"}]
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = "I think the agent is doing fine"

    with patch("auton.drift.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        coherence, drift, assessment = await judge_drift(
            "research competitors", messages, prev_coherence=0.8
        )
        assert coherence == 0.8  # Previous value preserved
        assert "parse error" in assessment.lower()


@pytest.mark.asyncio
async def test_judge_markdown_code_block():
    """Judge wraps JSON in markdown code block → still parsed."""
    messages = [{"role": "assistant", "content": "working on task"}]
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = '```json\n{"coherence": 0.8, "assessment": "good"}\n```'

    with patch("auton.drift.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        coherence, drift, assessment = await judge_drift(
            "do the thing", messages, prev_coherence=1.0
        )
        # EMA: 0.3 * 0.8 + 0.7 * 1.0 = 0.94
        assert coherence == pytest.approx(0.94, abs=0.01)
        assert assessment == "good"


# ---------------------------------------------------------------------------
# EMA smoothing behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ema_sustained_drift_triggers_suspension():
    """Sustained low coherence from judge → coherence drops below threshold."""
    messages = [{"role": "assistant", "content": "off topic rambling"}]
    mock_resp = _mock_judge_response(0.1, "completely off track")

    coherence = 1.0
    with patch("auton.drift.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        # Simulate multiple consecutive drift checks
        for i in range(5):
            coherence, drift, _ = await judge_drift(
                "research competitors", messages, prev_coherence=coherence
            )

    # After 5 checks at coherence=0.1:
    # EMA converges: 1.0 → 0.73 → 0.541 → 0.409 → 0.316 → 0.251
    assert coherence < 0.3  # Below coherence threshold → oversight suspends
    assert drift > 0.7


@pytest.mark.asyncio
async def test_ema_recovery():
    """Agent comes back on track → coherence recovers over time."""
    messages = [{"role": "assistant", "content": "back to work"}]
    mock_resp = _mock_judge_response(1.0, "back on track")

    coherence = 0.3  # Starting from drifted state
    with patch("auton.drift.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        for i in range(3):
            coherence, drift, _ = await judge_drift(
                "research competitors", messages, prev_coherence=coherence
            )

    # After 3 checks at coherence=1.0:
    # EMA: 0.3 → 0.51 → 0.657 → 0.76
    assert coherence > 0.7  # Recovered above drift threshold


@pytest.mark.asyncio
async def test_ema_single_blip_doesnt_trigger():
    """Single off-topic response doesn't trigger suspension."""
    messages = [{"role": "assistant", "content": "tangent"}]
    mock_resp = _mock_judge_response(0.1, "off topic")

    with patch("auton.drift.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        coherence, drift, _ = await judge_drift(
            "research competitors", messages, prev_coherence=1.0
        )

    # EMA: 0.3 * 0.1 + 0.7 * 1.0 = 0.73
    assert coherence > 0.4  # Still above drift threshold (0.4)
    assert coherence > 0.3  # Still above coherence threshold (0.3)


@pytest.mark.asyncio
async def test_judge_clamps_coherence():
    """Judge returning out-of-range values get clamped."""
    messages = [{"role": "assistant", "content": "working"}]
    mock_resp = _mock_judge_response(1.5, "very good")  # Over 1.0

    with patch("auton.drift.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        coherence, drift, _ = await judge_drift(
            "goal", messages, prev_coherence=0.5
        )
        assert 0.0 <= coherence <= 1.0
        assert 0.0 <= drift <= 1.0


@pytest.mark.asyncio
async def test_judge_uses_configured_model(monkeypatch):
    """Judge model is configurable via env var."""
    monkeypatch.setenv("AUTON_JUDGE_MODEL", "openrouter/google/gemini-flash-2.0")
    messages = [{"role": "assistant", "content": "working"}]
    mock_resp = _mock_judge_response(0.8, "ok")

    with patch("auton.drift.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock_call:
        await judge_drift("goal", messages)
        call_kwargs = mock_call.call_args
        assert call_kwargs.kwargs["model"] == "openrouter/google/gemini-flash-2.0"
