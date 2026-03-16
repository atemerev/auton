"""Tests for the artifact system: models, tools, DB, lifecycle, API."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from auton.api import app, registry, _publish_event
from auton.models import (
    AgentNode,
    AgentSpec,
    AgentState,
    ArtifactRecord,
    ArtifactSpec,
    ArtifactStatus,
    IdleReason,
    SpawnRequest,
)
from auton.tools.workspace_tools import make_publish_artifact
from auton.workspace import (
    cleanup_workspace,
    ensure_workspace,
    get_workspace_path,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_registry():
    """Clean up registry and workspaces after each test."""
    yield
    for name in list(registry._roots.keys()):
        try:
            registry.terminate(name, cascade=True)
        except Exception:
            pass
    registry._roots.clear()
    registry._executors.clear()
    for name in [
        "test-art", "test-art-parent", "test-publish", "test-idle-art",
        "test-api-art", "test-expected",
    ]:
        cleanup_workspace(name)


def _spawn_no_executor(req: SpawnRequest, parent_path=None):
    """Spawn an agent without starting an executor."""
    saved = registry.publish_event
    registry.publish_event = None
    try:
        return registry.spawn(req, parent_path=parent_path)
    finally:
        registry.publish_event = saved


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


def test_artifact_spec_creation():
    """ArtifactSpec can be created with defaults."""
    spec = ArtifactSpec(name="report", file_path="report.md")
    assert spec.name == "report"
    assert spec.file_path == "report.md"
    assert spec.mime_type == "text/markdown"
    assert spec.tags == []


def test_artifact_record_creation():
    """ArtifactRecord generates UUID and timestamps."""
    record = ArtifactRecord(
        name="dossier",
        file_path="dossier.md",
        agent_id="agent-1",
        agent_path="agent-1",
    )
    assert len(record.id) == 36  # UUID format
    assert record.status == ArtifactStatus.EXPECTED
    assert record.file_size == 0


def test_artifact_status_enum():
    """ArtifactStatus has expected values."""
    assert ArtifactStatus.EXPECTED == "expected"
    assert ArtifactStatus.PUBLISHED == "published"
    assert ArtifactStatus.MISSING == "missing"


def test_agent_spec_expected_artifacts():
    """AgentSpec accepts expected_artifacts."""
    spec = AgentSpec(
        name="Test",
        system_prompt="Test.",
        goal="Test",
        expected_artifacts=[
            ArtifactSpec(name="report", file_path="report.md"),
            ArtifactSpec(name="data", file_path="data.json", mime_type="application/json"),
        ],
    )
    assert len(spec.expected_artifacts) == 2
    assert spec.expected_artifacts[0].name == "report"
    assert spec.expected_artifacts[1].mime_type == "application/json"


def test_agent_node_artifacts_list():
    """AgentNode has an artifacts list."""
    spec = AgentSpec(name="Test", system_prompt="Test.", goal="Test")
    node = AgentNode(id="test", spec=spec)
    assert node.artifacts == []


def test_artifact_count_in_to_dict():
    """to_dict includes artifact_count."""
    spec = AgentSpec(name="Test", system_prompt="Test.", goal="Test")
    node = AgentNode(id="test", spec=spec)
    node.artifacts.append(ArtifactRecord(
        name="report", file_path="report.md",
        agent_id="test", agent_path="test",
    ))
    d = node.to_dict()
    assert d["artifact_count"] == 1


# ---------------------------------------------------------------------------
# Publish artifact tool tests
# ---------------------------------------------------------------------------


def test_publish_artifact_success():
    """publish_artifact registers a workspace file."""
    registry.publish_event = _publish_event
    registry.db = None

    spec = AgentSpec(name="Test", system_prompt="Test.", goal="Test")
    node = _spawn_no_executor(SpawnRequest(id="test-publish", spec=spec))

    # Create a file in workspace
    ws = ensure_workspace("test-publish")
    (ws / "report.md").write_text("# Report\nFindings here.")

    fn = make_publish_artifact("test-publish", "test-publish", registry)
    result = json.loads(fn("report.md", "My Report", "Detailed findings", "text/markdown"))

    assert result["status"] == "OK"
    assert result["name"] == "My Report"
    assert result["file_size"] > 0
    assert "artifact_id" in result

    # Check it was added to node
    assert len(node.artifacts) == 1
    assert node.artifacts[0].status == ArtifactStatus.PUBLISHED
    assert node.artifacts[0].name == "My Report"


def test_publish_artifact_missing_file():
    """publish_artifact returns error if file doesn't exist."""
    registry.publish_event = _publish_event
    registry.db = None

    spec = AgentSpec(name="Test", system_prompt="Test.", goal="Test")
    _spawn_no_executor(SpawnRequest(id="test-publish", spec=spec))
    ensure_workspace("test-publish")

    fn = make_publish_artifact("test-publish", "test-publish", registry)
    result = json.loads(fn("nonexistent.md", "Missing"))

    assert result["status"] == "error"
    assert "not found" in result["message"].lower()


def test_publish_artifact_matches_expected():
    """Publishing a file that matches an expected artifact updates it."""
    registry.publish_event = _publish_event
    registry.db = None

    spec = AgentSpec(
        name="Test", system_prompt="Test.", goal="Test",
        expected_artifacts=[ArtifactSpec(name="report", file_path="report.md")],
    )
    node = _spawn_no_executor(SpawnRequest(id="test-publish", spec=spec))

    # Seed expected artifacts (normally done by executor)
    expected = ArtifactRecord(
        name="report", file_path="report.md",
        agent_id="test-publish", agent_path="test-publish",
        status=ArtifactStatus.EXPECTED,
    )
    node.artifacts.append(expected)

    # Create and publish the file
    ws = ensure_workspace("test-publish")
    (ws / "report.md").write_text("# Report")

    fn = make_publish_artifact("test-publish", "test-publish", registry)
    result = json.loads(fn("report.md", "Final Report", "Complete findings"))

    assert result["status"] == "OK"
    # Should update existing, not create new
    assert len(node.artifacts) == 1
    assert node.artifacts[0].status == ArtifactStatus.PUBLISHED
    assert node.artifacts[0].name == "Final Report"


def test_publish_artifact_path_escape():
    """publish_artifact rejects paths that escape workspace."""
    registry.publish_event = _publish_event
    registry.db = None

    spec = AgentSpec(name="Test", system_prompt="Test.", goal="Test")
    _spawn_no_executor(SpawnRequest(id="test-publish", spec=spec))
    ensure_workspace("test-publish")

    fn = make_publish_artifact("test-publish", "test-publish", registry)
    result = json.loads(fn("../../etc/passwd", "Escape"))

    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# DB tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_save_and_get_artifact():
    """Artifacts can be saved and retrieved by ID."""
    from auton.db import Database

    db = Database(":memory:")
    await db.init()

    record = ArtifactRecord(
        name="test-report",
        file_path="report.md",
        agent_id="agent-1",
        agent_path="agent-1",
        description="A test report",
        tags=["test", "report"],
    )
    await db.save_artifact(record.model_dump())

    loaded = await db.get_artifact(record.id)
    assert loaded is not None
    assert loaded["name"] == "test-report"
    assert loaded["file_path"] == "report.md"
    assert loaded["tags"] == ["test", "report"]
    assert loaded["status"] == "expected"

    await db.close()


@pytest.mark.asyncio
async def test_db_list_artifacts_filtered():
    """list_artifacts supports filtering by agent_id, status, tag."""
    from auton.db import Database

    db = Database(":memory:")
    await db.init()

    for i, status in enumerate(["expected", "published", "published"]):
        record = ArtifactRecord(
            name=f"art-{i}",
            file_path=f"file-{i}.md",
            agent_id="agent-1" if i < 2 else "agent-2",
            agent_path="agent-1" if i < 2 else "agent-2",
            status=ArtifactStatus(status),
            tags=["research"] if i == 0 else [],
        )
        await db.save_artifact(record.model_dump())

    # All
    all_arts = await db.list_artifacts()
    assert len(all_arts) == 3

    # By agent
    agent1 = await db.list_artifacts(agent_id="agent-1")
    assert len(agent1) == 2

    # By status
    published = await db.list_artifacts(status="published")
    assert len(published) == 2

    # By tag
    tagged = await db.list_artifacts(tag="research")
    assert len(tagged) == 1

    await db.close()


@pytest.mark.asyncio
async def test_db_update_artifact_status():
    """Artifact status can be updated."""
    from auton.db import Database

    db = Database(":memory:")
    await db.init()

    record = ArtifactRecord(
        name="test",
        file_path="test.md",
        agent_id="a",
        agent_path="a",
        status=ArtifactStatus.EXPECTED,
    )
    await db.save_artifact(record.model_dump())
    await db.update_artifact_status(record.id, "published")

    loaded = await db.get_artifact(record.id)
    assert loaded["status"] == "published"

    await db.close()


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


def test_idle_transition_marks_missing_artifacts():
    """When an agent goes IDLE, expected artifacts without files are marked MISSING."""
    spec = AgentSpec(name="Test", system_prompt="Test.", goal="Test")
    node = AgentNode(id="test-idle-art", spec=spec)
    node.state = AgentState.RUNNING

    # Add expected artifact (no file on disk)
    node.artifacts.append(ArtifactRecord(
        name="report", file_path="report.md",
        agent_id="test-idle-art", agent_path="test-idle-art",
        status=ArtifactStatus.EXPECTED,
    ))

    # Simulate the completeness check (normally done by executor)
    from auton.workspace import get_workspace_path
    ensure_workspace("test-idle-art")
    for artifact in node.artifacts:
        if artifact.status == ArtifactStatus.EXPECTED:
            ws = get_workspace_path("test-idle-art")
            target = ws / artifact.file_path
            if target.exists():
                artifact.status = ArtifactStatus.PUBLISHED
            else:
                artifact.status = ArtifactStatus.MISSING

    assert node.artifacts[0].status == ArtifactStatus.MISSING


def test_idle_transition_auto_promotes_existing_file():
    """Expected artifact is auto-promoted to PUBLISHED if file exists on disk."""
    spec = AgentSpec(name="Test", system_prompt="Test.", goal="Test")
    node = AgentNode(id="test-idle-art", spec=spec)
    node.state = AgentState.RUNNING

    # Create workspace and file
    ws = ensure_workspace("test-idle-art")
    (ws / "report.md").write_text("# My Report")

    node.artifacts.append(ArtifactRecord(
        name="report", file_path="report.md",
        agent_id="test-idle-art", agent_path="test-idle-art",
        status=ArtifactStatus.EXPECTED,
    ))

    # Simulate the completeness check
    for artifact in node.artifacts:
        if artifact.status == ArtifactStatus.EXPECTED:
            target = ws / artifact.file_path
            if target.exists():
                artifact.status = ArtifactStatus.PUBLISHED
                artifact.file_size = target.stat().st_size

    assert node.artifacts[0].status == ArtifactStatus.PUBLISHED
    assert node.artifacts[0].file_size > 0


# ---------------------------------------------------------------------------
# Coordination tool tests
# ---------------------------------------------------------------------------


def test_check_child_status_includes_artifacts():
    """check_child_status includes artifact info when child has artifacts."""
    from auton.tools.coordination import make_check_child_status

    registry.publish_event = _publish_event
    registry.db = None

    spec = AgentSpec(name="Parent", system_prompt="P.", goal="P")
    parent = _spawn_no_executor(SpawnRequest(id="test-art-parent", spec=spec))

    child_spec = AgentSpec(name="Child", system_prompt="C.", goal="C")
    child = _spawn_no_executor(
        SpawnRequest(id="child-1", spec=child_spec),
        parent_path="test-art-parent",
    )
    child.artifacts.append(ArtifactRecord(
        name="dossier", file_path="dossier.md",
        agent_id="child-1", agent_path="test-art-parent/child-1",
        status=ArtifactStatus.PUBLISHED,
    ))
    child.artifacts.append(ArtifactRecord(
        name="data", file_path="data.json",
        agent_id="child-1", agent_path="test-art-parent/child-1",
        status=ArtifactStatus.MISSING,
    ))

    check_fn = make_check_child_status("test-art-parent", registry)
    result = json.loads(check_fn("test-art-parent/child-1"))

    assert result["status"] == "OK"
    assert "artifacts" in result
    assert len(result["artifacts"]["published"]) == 1
    assert result["artifacts"]["published"][0]["name"] == "dossier"
    assert result["artifacts"]["missing"] == ["data"]
    assert result["artifacts"]["total"] == 2


def test_list_children_includes_artifact_counts():
    """list_children includes artifact counts for each child."""
    from auton.tools.coordination import make_list_children

    registry.publish_event = _publish_event
    registry.db = None

    spec = AgentSpec(name="Parent", system_prompt="P.", goal="P")
    parent = _spawn_no_executor(SpawnRequest(id="test-art-parent", spec=spec))

    child_spec = AgentSpec(name="Child", system_prompt="C.", goal="C")
    child = _spawn_no_executor(
        SpawnRequest(id="child-1", spec=child_spec),
        parent_path="test-art-parent",
    )
    child.artifacts.append(ArtifactRecord(
        name="report", file_path="report.md",
        agent_id="child-1", agent_path="test-art-parent/child-1",
        status=ArtifactStatus.PUBLISHED,
    ))

    list_fn = make_list_children("test-art-parent", "test-art-parent", registry)
    result = json.loads(list_fn())

    assert result["status"] == "OK"
    assert result["children"][0]["artifact_count"] == 1
    assert result["children"][0]["artifacts_published"] == 1


# ---------------------------------------------------------------------------
# Initial message tests
# ---------------------------------------------------------------------------


def test_initial_message_includes_expected_artifacts():
    """Initial message mentions expected artifacts when declared."""
    from auton.executor import AgentExecutor

    spec = AgentSpec(
        name="Test", system_prompt="Test.", goal="Do the task.",
        expected_artifacts=[
            ArtifactSpec(name="Report", file_path="report.md"),
        ],
    )
    node = AgentNode(id="test-expected", spec=spec)
    node.state = AgentState.RUNNING

    executor = AgentExecutor(
        node=node,
        publish_event=lambda *a: None,
    )
    msg = executor._build_initial_message()
    assert "Expected deliverables" in msg
    assert "Report (report.md)" in msg
    assert "publish_artifact" in msg


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_artifacts_endpoint():
    """GET /agents/{path}/artifacts returns agent artifacts."""
    registry.publish_event = _publish_event
    registry.db = None

    spec = AgentSpec(name="Test", system_prompt="Test.", goal="Test")
    node = _spawn_no_executor(SpawnRequest(id="test-api-art", spec=spec))
    node.artifacts.append(ArtifactRecord(
        name="report", file_path="report.md",
        agent_id="test-api-art", agent_path="test-api-art",
        status=ArtifactStatus.PUBLISHED,
    ))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/agents/test-api-art/artifacts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["artifacts"][0]["name"] == "report"
        assert data["artifacts"][0]["status"] == "published"


@pytest.mark.asyncio
async def test_artifacts_list_endpoint():
    """GET /artifacts returns artifacts from DB."""
    from auton.db import Database

    db = Database(":memory:")
    await db.init()
    registry.db = db
    registry.publish_event = _publish_event

    record = ArtifactRecord(
        name="test-report",
        file_path="report.md",
        agent_id="agent-1",
        agent_path="agent-1",
        status=ArtifactStatus.PUBLISHED,
    )
    await db.save_artifact(record.model_dump())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/artifacts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["artifacts"][0]["name"] == "test-report"

    await db.close()
    registry.db = None


@pytest.mark.asyncio
async def test_artifact_get_by_id():
    """GET /artifacts/{id} returns artifact metadata."""
    from auton.db import Database

    db = Database(":memory:")
    await db.init()
    registry.db = db
    registry.publish_event = _publish_event

    record = ArtifactRecord(
        name="test-report",
        file_path="report.md",
        agent_id="agent-1",
        agent_path="agent-1",
    )
    await db.save_artifact(record.model_dump())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/artifacts/{record.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "test-report"

        # 404 for nonexistent
        resp = await client.get("/artifacts/nonexistent-id")
        assert resp.status_code == 404

    await db.close()
    registry.db = None


@pytest.mark.asyncio
async def test_artifact_content_endpoint():
    """GET /artifacts/{id}/content serves the file."""
    from auton.db import Database

    db = Database(":memory:")
    await db.init()
    registry.db = db
    registry.publish_event = _publish_event

    # Create workspace and file
    ws = ensure_workspace("test-api-art")
    (ws / "report.md").write_text("# Test Report\nContent here.")

    record = ArtifactRecord(
        name="test-report",
        file_path="report.md",
        agent_id="test-api-art",
        agent_path="test-api-art",
        status=ArtifactStatus.PUBLISHED,
    )
    await db.save_artifact(record.model_dump())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/artifacts/{record.id}/content")
        assert resp.status_code == 200
        assert "# Test Report" in resp.text

    await db.close()
    registry.db = None


@pytest.mark.asyncio
async def test_artifacts_filter_by_status():
    """GET /artifacts?status=published filters correctly."""
    from auton.db import Database

    db = Database(":memory:")
    await db.init()
    registry.db = db
    registry.publish_event = _publish_event

    for status in ["expected", "published", "missing"]:
        record = ArtifactRecord(
            name=f"art-{status}",
            file_path=f"{status}.md",
            agent_id="agent-1",
            agent_path="agent-1",
            status=ArtifactStatus(status),
        )
        await db.save_artifact(record.model_dump())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/artifacts?status=published")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["artifacts"][0]["name"] == "art-published"

    await db.close()
    registry.db = None
