"""Tests for the LangChain agent module.

Uses mock dependencies to verify wiring without calling real APIs.
StructuredTool.from_function requires real callables (not MagicMock)
because it introspects type hints to build the tool schema.
"""

from unittest.mock import MagicMock

from src.models import TaskPhase, TaskRequest


def _fake_get_repo_info(repo_name: str) -> dict:
    """Stub: returns static repo metadata."""
    return {
        "name": repo_name,
        "default_branch": "main",
        "description": "Test repo",
    }


def _fake_create_draft_pr(
    repo_name: str,
    head: str,
    base: str,
    title: str,
    body: str,
) -> dict:
    """Stub: returns a fake PR response."""
    return {"url": "https://github.com/org/repo/pull/1", "number": 1}


def _fake_history(agent_ref: str, limit: int = 10) -> list:
    """Stub: returns empty task history."""
    return []


def _fake_store(task_id: str, result: dict) -> dict:
    """Stub: returns a fake indexed document."""
    return {"id": "doc-1", "index": "task_memory"}


class TestCreateAgent:
    """Verify agent factory wiring with stub dependencies."""

    def test_create_agent_returns_run_task(self) -> None:
        """create_agent() returns a dict with a 'run_task' callable."""
        # Arrange — use real functions (not MagicMock) so StructuredTool
        # can introspect type hints for schema generation.
        mock_config = MagicMock()
        mock_config.llm_model = "gpt-4"
        mock_config.openai_api_key = "test-key"

        github_tools = {
            "get_repo_info": _fake_get_repo_info,
            "create_draft_pr": _fake_create_draft_pr,
        }

        memory_store = {
            "store": _fake_store,
            "history": _fake_history,
        }

        # Act
        from src.agent import create_agent

        agent = create_agent(mock_config, github_tools, memory_store)

        # Assert
        assert "run_task" in agent
        assert callable(agent["run_task"])

    def test_task_request_model_used_by_agent(self) -> None:
        """TaskRequest can be constructed for agent consumption."""
        request = TaskRequest(
            task_id="t-agent-01",
            agent_ref="test-agent",
            repository="org/repo",
            prompt="Fix the tests",
        )
        assert request.task_id == "t-agent-01"
        assert request.target_branch == "agent-changes"
