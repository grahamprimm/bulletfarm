"""Request/response models for the worker service.

Pure data definitions — no side effects, no business logic.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class TaskPhase(str, Enum):
    """Lifecycle phases for an agent task."""

    PENDING = "Pending"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    INCOMPLETE = "Incomplete"
    FAILED = "Failed"
    WAITING_FOR_PR = "WaitingForPR"
    MERGED = "Merged"
    CLOSED = "Closed"


class TaskRequest(BaseModel):
    """Inbound task from the K8s operator."""

    task_id: str
    agent_ref: str
    repository: str
    prompt: str
    target_branch: str = "agent-changes"
    skills: list[str] = []
    is_retry: bool = False
    pr_url: str = ""


class TaskStatus(BaseModel):
    """Current status of a running or completed task."""

    task_id: str
    phase: TaskPhase
    progress: int = 0
    message: str = ""
    pull_request_url: str = ""
    rate_limited: bool = False
    pr_state: str = ""
    incomplete_reason: str = ""


class PRStatusResponse(BaseModel):
    """Response from /tasks/{id}/pr-status."""

    pr_url: str = ""
    state: str = ""
    merged: bool = False
    draft: bool = False


class GraduateResponse(BaseModel):
    """Response from /tasks/{id}/graduate — memory lifecycle on PR close/merge."""

    task_memory_deleted: bool = False
    shared_memory_updated: bool = False
    pr_state: str = ""


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = "healthy"
    version: str = "0.1.0"
