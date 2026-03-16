# BulletFarm Worker

Python FastAPI service that executes AI agent tasks. Receives instructions from the Kubernetes operator, runs a LangChain agent with modular tools, interacts with GitHub repositories, and stores results in Elasticsearch.

## What It Does

Each worker pod handles a single task:

1. **Clone** the target GitHub repository
2. **Create** a task branch (or checkout existing branch on retry)
3. **Run** a LangChain agent with dynamically selected tools
4. **Commit and push** changes to the branch
5. **Create a draft PR** with labels and metadata (or update existing PR on retry)
6. **Store results** in Elasticsearch (task memory + shared memory)
7. **Report status** via REST API for the operator to poll

## REST API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Liveness/readiness probe |
| `POST` | `/tasks` | Submit a new task (returns 202) |
| `GET` | `/tasks/{id}/status` | Poll task progress (used by operator) |
| `POST` | `/tasks/{id}/finalize` | Mark PR ready for review |
| `GET` | `/tasks/{id}/pr-status` | Check GitHub PR state (open/merged/closed) |
| `POST` | `/tasks/{id}/graduate` | Graduate task memory to shared memory |

## Tool System

### Core Tools (always available)

| Tool | Description |
|------|-------------|
| `read_file` | Read file contents (with truncation for large files) |
| `list_files` | Recursively list files in a directory |
| `search_shared_knowledge` | Search Elasticsearch shared memory for relevant past knowledge |

### Skill Tools (loaded per task)

| Skill Name | Tool | Description |
|------------|------|-------------|
| `code-edit` | `code_edit` | Edit existing files or create new files. To create: pass `old_content=""` |
| `testing` / `test-generator` | `test_generator` | Generate test stubs for Python (pytest) and JavaScript (Jest/supertest) |
| `documentation` / `doc-update` | `doc_update` | Update or append sections in Markdown documentation |
| `graphql` / `graphql-debug` | `graphql_debug` | Send GraphQL queries to an endpoint for debugging |

### Adding New Skills

Add a new factory function to `_build_skill_tools()` in `agent.py`:

```python
def _my_new_tool() -> StructuredTool:
    def my_tool(arg1: str, arg2: str) -> str:
        # implementation
        return "result"

    return StructuredTool.from_function(
        func=my_tool,
        name="my_tool",
        description="What this tool does. Args: arg1, arg2.",
    )
```

Then register it in the return dict:

```python
return {
    ...
    "my-skill": _my_new_tool,
}
```

No operator changes needed — the worker auto-loads skills based on the task's `skills` list.

## Configuration

All configuration via environment variables with `BULLETFARM_` prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `BULLETFARM_GITHUB_TOKEN` | (required) | GitHub personal access token |
| `BULLETFARM_OPENAI_API_KEY` | (required for OpenAI) | OpenAI API key |
| `BULLETFARM_ELASTICSEARCH_URL` | `http://elasticsearch-master:9200` | ES endpoint |
| `BULLETFARM_LLM_PROVIDER` | `openai` | `openai` or `ollama` |
| `BULLETFARM_LLM_MODEL` | `gpt-4o-mini` | LLM model name |
| `BULLETFARM_OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint |
| `BULLETFARM_WORKER_PORT` | `8000` | FastAPI listen port |

The operator injects these via the worker pod's environment, sourced from the `bulletfarm-secrets` Kubernetes Secret and the Agent CR's config.

## Auto-Start

When the operator creates a worker pod, it sets the `TASK_PAYLOAD` environment variable with a JSON payload. The worker automatically parses this and starts the task on boot — no manual API call needed.

## Incomplete Detection

After the LLM agent runs, the worker checks if real code changes were produced. If not, the task is marked `Incomplete`:

- A `.bulletfarm-incomplete.md` file is committed with the agent's analysis
- The PR is created in draft mode with `[INCOMPLETE]` in the title
- A comment is added explaining why the task couldn't be completed
- The operator treats this as a failure and retries if attempts remain

## Retry Behavior

On retry, the worker:

1. Clones the full repo (not shallow) and checks out the **existing branch**
2. The LLM agent runs again with the same tools and prompt
3. New commits are pushed to the **same branch**
4. If a PR already exists, a comment is added noting the retry succeeded
5. If no PR exists yet, a new one is created

## Strict Typing

All Python files use `from __future__ import annotations` and explicit type annotations:

- `TypedDict` for structured return types (`MemoryStore`, `GitHubClient`, `PRInfo`, etc.)
- Function signatures with full parameter and return types
- Local variable annotations where the type isn't obvious

## Building

```bash
# Install dependencies
uv sync

# Run locally
uv run uvicorn src.main:app --host 0.0.0.0 --port 8000

# Run tests
uv run pytest

# Build Docker image
docker build -t bulletfarm/worker:latest .
```

## File Structure

```
worker/
├── src/
│   ├── main.py              # FastAPI app, routes, lifespan, auto-start
│   ├── agent.py             # LangChain agent factory, tool system, run_task
│   ├── config.py            # Pydantic settings (env vars)
│   ├── github_tools.py      # PyGithub: PR creation, labels, status, comments
│   ├── memory.py            # Elasticsearch: task_memory + shared_memory
│   └── models.py            # Pydantic request/response models
├── tests/
│   ├── test_main.py
│   ├── test_agent.py
│   └── test_models.py
├── Dockerfile
├── pyproject.toml
└── uv.lock
```
