"""BulletFarm Worker — FastAPI entry point.

Receives task instructions from the K8s operator, runs a LangChain agent
with GitHub tools, stores results in Elasticsearch, and reports status.

Supports auto-start from TASK_PAYLOAD env var (operator injects this)
and manual task submission via POST /tasks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import BackgroundTasks, FastAPI, HTTPException

from src.config import WorkerConfig
from src.github_tools import GitHubClient
from src.memory import MemoryStore
from src.models import (
    GraduateResponse,
    HealthResponse,
    PRStatusResponse,
    TaskPhase,
    TaskRequest,
    TaskStatus,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("src.memory").setLevel(logging.DEBUG)
logging.getLogger("src.agent").setLevel(logging.INFO)

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory task status registry
# ---------------------------------------------------------------------------
_task_statuses: dict[str, TaskStatus] = {}


def _build_dependencies(config: WorkerConfig) -> dict[str, Any]:
    """Wire up all service dependencies from config."""
    from elasticsearch import Elasticsearch

    from src.agent import create_agent
    from src.github_tools import create_github_client
    from src.memory import create_memory_store

    es_client: Elasticsearch = Elasticsearch(config.elasticsearch_url)
    memory_store: MemoryStore = create_memory_store(es_client)
    github_tools: GitHubClient = create_github_client(config.github_token)
    agent: dict[str, Any] = create_agent(config, github_tools, memory_store)

    return {"agent": agent, "github": github_tools, "memory": memory_store}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: initialise dependencies and auto-start task."""
    config: WorkerConfig = WorkerConfig()
    deps: dict[str, Any] = _build_dependencies(config)
    app.state.deps = deps
    app.state.config = config
    logger.info("Worker started on port %s", config.worker_port)

    # Auto-start task from TASK_PAYLOAD env var (injected by operator)
    task_payload: str | None = os.environ.get("TASK_PAYLOAD")
    if task_payload:
        try:
            payload: dict[str, Any] = json.loads(task_payload)
            request: TaskRequest = TaskRequest(
                task_id=payload["task_id"],
                agent_ref=payload["agent_ref"],
                repository=payload["repository"],
                prompt=payload.get("prompt", payload.get("description", "")),
                target_branch=payload.get("target_branch", "agent-changes"),
                skills=payload.get("skills", []),
                is_retry=payload.get("is_retry", False),
                pr_url=payload.get("pr_url", ""),
            )
            status: TaskStatus = TaskStatus(task_id=request.task_id, phase=TaskPhase.RUNNING)
            _task_statuses[request.task_id] = status
            logger.info("Auto-starting task from TASK_PAYLOAD: %s", request.task_id)

            asyncio.create_task(_execute_task(request))
        except Exception as exc:
            logger.exception("Failed to parse TASK_PAYLOAD: %s", exc)

    yield
    logger.info("Worker shutting down")


app: FastAPI = FastAPI(
    title="BulletFarm Worker",
    description="AI agent task processing service",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Liveness / readiness probe for Kubernetes."""
    return HealthResponse()


@app.post("/tasks", response_model=TaskStatus, status_code=202)
async def create_task(
    request: TaskRequest,
    background_tasks: BackgroundTasks,
) -> TaskStatus:
    """Accept a new agent task and process it in the background."""
    status: TaskStatus = TaskStatus(task_id=request.task_id, phase=TaskPhase.RUNNING)
    _task_statuses[request.task_id] = status
    background_tasks.add_task(_execute_task, request)
    return status


@app.get("/tasks/{task_id}/status", response_model=TaskStatus)
async def get_task_status(task_id: str) -> TaskStatus:
    """Poll the current status of a task (used by operator reconcile loop)."""
    status: TaskStatus | None = _task_statuses.get(task_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return status


@app.post("/tasks/{task_id}/finalize", response_model=TaskStatus)
async def finalize_task(task_id: str) -> TaskStatus:
    """Mark a completed task's PR as ready for review.

    Called by the operator after it sees phase=Succeeded.
    The agent marks the PR ready but NEVER merges or closes it.
    """
    status: TaskStatus | None = _task_statuses.get(task_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    if status.phase != TaskPhase.SUCCEEDED:
        raise HTTPException(
            status_code=400,
            detail=f"Task {task_id} is in phase {status.phase}, not Succeeded",
        )

    if status.pull_request_url:
        try:
            github_tools: GitHubClient = app.state.deps["github"]
            github_tools["mark_pr_ready"](status.pull_request_url)
            status.message = "PR marked ready for review"
            logger.info("Finalized task %s, PR ready: %s", task_id, status.pull_request_url)
        except Exception as exc:
            logger.exception("Failed to finalize PR for task %s", task_id)
            status.message = f"Finalize error: {str(exc)}"[:500]

    return status


@app.get("/tasks/{task_id}/pr-status", response_model=PRStatusResponse)
async def get_pr_status(task_id: str) -> PRStatusResponse:
    """Check the current GitHub PR state for a task.

    Called by the operator to detect when a human merges or closes the PR.
    """
    status: TaskStatus | None = _task_statuses.get(task_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    if not status.pull_request_url:
        return PRStatusResponse()

    github_tools: GitHubClient = app.state.deps["github"]
    pr_info: dict[str, Any] = github_tools["get_pr_status"](status.pull_request_url)
    return PRStatusResponse(
        pr_url=status.pull_request_url,
        state=pr_info["state"],
        merged=pr_info["merged"],
        draft=pr_info["draft"],
    )


@app.post("/tasks/{task_id}/graduate", response_model=GraduateResponse)
async def graduate_task(task_id: str) -> GraduateResponse:
    """Graduate task memory to shared memory and delete task-specific memory.

    Called by the operator when a human merges or closes the PR.
    """
    status: TaskStatus | None = _task_statuses.get(task_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    pr_state: str = "unknown"
    if status.pull_request_url:
        github_tools: GitHubClient = app.state.deps["github"]
        pr_info: dict[str, Any] = github_tools["get_pr_status"](status.pull_request_url)
        pr_state = pr_info["state"]

    memory: MemoryStore = app.state.deps["memory"]
    graduated: bool = memory["graduate_to_shared"](task_id, pr_state)

    logger.info("Graduated task %s: pr_state=%s graduated=%s", task_id, pr_state, graduated)

    return GraduateResponse(
        task_memory_deleted=graduated,
        shared_memory_updated=graduated,
        pr_state=pr_state,
    )


# ---------------------------------------------------------------------------
# Background processing
# ---------------------------------------------------------------------------

_RATE_LIMIT_MARKERS: list[str] = [
    "rate limit", "429", "too many requests", "quota exceeded", "rate_limit",
]


def _is_rate_limit_error(exc: Exception) -> bool:
    """Detect if an exception is a rate limit error from OpenAI or GitHub."""
    msg: str = str(exc).lower()
    return any(marker in msg for marker in _RATE_LIMIT_MARKERS)


async def _execute_task(request: TaskRequest) -> None:
    """Run the LangChain agent for a task and update status on completion."""
    try:
        agent: dict[str, Any] = app.state.deps["agent"]
        result: TaskStatus = await agent["run_task"](request, _update_progress)
        _task_statuses[request.task_id] = result
    except Exception as exc:
        logger.exception("Unhandled error in task %s", request.task_id)
        is_rl: bool = _is_rate_limit_error(exc)
        _task_statuses[request.task_id] = TaskStatus(
            task_id=request.task_id,
            phase=TaskPhase.FAILED,
            message=f"Internal error: {str(exc)}"[:500],
            rate_limited=is_rl,
        )


def _update_progress(task_id: str, progress: int, message: str = "") -> None:
    """Callback for the agent to report incremental progress."""
    status: TaskStatus | None = _task_statuses.get(task_id)
    if status:
        status.progress = min(progress, 99)
        if message:
            status.message = message
