"""Tests for Pydantic request/response models.

Covers validation, defaults, and enum behaviour.
"""

import pytest

from src.models import HealthResponse, TaskPhase, TaskRequest, TaskStatus


# ---------------------------------------------------------------------------
# TaskPhase enum
# ---------------------------------------------------------------------------


class TestTaskPhase:
    """TaskPhase enum values and string representation."""

    def test_pending_value(self) -> None:
        assert TaskPhase.PENDING.value == "Pending"

    def test_running_value(self) -> None:
        assert TaskPhase.RUNNING.value == "Running"

    def test_succeeded_value(self) -> None:
        assert TaskPhase.SUCCEEDED.value == "Succeeded"

    def test_failed_value(self) -> None:
        assert TaskPhase.FAILED.value == "Failed"

    def test_is_string_subclass(self) -> None:
        assert isinstance(TaskPhase.PENDING, str)


# ---------------------------------------------------------------------------
# TaskRequest
# ---------------------------------------------------------------------------


class TestTaskRequest:
    """TaskRequest model validation and defaults."""

    def test_valid_request(self) -> None:
        req = TaskRequest(
            task_id="t-001",
            agent_ref="agent-alpha",
            repository="org/repo",
            prompt="Fix the bug",
        )
        assert req.task_id == "t-001"
        assert req.agent_ref == "agent-alpha"
        assert req.repository == "org/repo"
        assert req.prompt == "Fix the bug"

    def test_default_target_branch(self) -> None:
        req = TaskRequest(
            task_id="t-002",
            agent_ref="agent-beta",
            repository="org/repo",
            prompt="Add feature",
        )
        assert req.target_branch == "agent-changes"

    def test_custom_target_branch(self) -> None:
        req = TaskRequest(
            task_id="t-003",
            agent_ref="agent-gamma",
            repository="org/repo",
            prompt="Refactor",
            target_branch="feature/custom",
        )
        assert req.target_branch == "feature/custom"

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(Exception):
            TaskRequest(task_id="t-004", agent_ref="a")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# TaskStatus
# ---------------------------------------------------------------------------


class TestTaskStatus:
    """TaskStatus model validation and defaults."""

    def test_defaults(self) -> None:
        status = TaskStatus(task_id="t-010", phase=TaskPhase.PENDING)
        assert status.message == ""
        assert status.pull_request_url == ""

    def test_full_status(self) -> None:
        status = TaskStatus(
            task_id="t-011",
            phase=TaskPhase.SUCCEEDED,
            message="Done",
            pull_request_url="https://github.com/org/repo/pull/42",
        )
        assert status.phase == TaskPhase.SUCCEEDED
        assert status.pull_request_url == "https://github.com/org/repo/pull/42"


# ---------------------------------------------------------------------------
# HealthResponse
# ---------------------------------------------------------------------------


class TestHealthResponse:
    """HealthResponse model defaults."""

    def test_defaults(self) -> None:
        resp = HealthResponse()
        assert resp.status == "healthy"
        assert resp.version == "0.1.0"

    def test_serialization(self) -> None:
        data = HealthResponse().model_dump()
        assert data == {"status": "healthy", "version": "0.1.0"}
