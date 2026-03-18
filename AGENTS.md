# AGENTS.md

Guidance for coding agents working in this repository.

## Scope

- Root application code lives in `operator/`, `worker/`, `charts/`, `tests/e2e/`, and `docs/`.
- `.opencode/` contains local agent tooling and context; it is not the main product runtime.
- There was no pre-existing root `AGENTS.md` when this file was created.

## Repository layout

- `operator/` — Go Kubernetes operator built with kubebuilder/controller-runtime.
- `worker/` — Python FastAPI worker using LangChain, PyGithub, and Elasticsearch.
- `tests/e2e/` — bash-driven end-to-end suite against a live cluster.
- `charts/bulletfarm-operator/` — Helm chart.
- `docs/` — project docs.

## Editor / assistant rule files

- No `.cursorrules` file was found.
- No `.cursor/rules/` directory was found.
- No `.github/copilot-instructions.md` file was found.
- Do not assume hidden editor rules exist; follow the repository conventions below.

## General workflow for agents

1. Identify whether the change is in `operator/`, `worker/`, or E2E/docs.
2. Use the component-local commands below instead of inventing new ones.
3. Prefer small, surgical changes over broad refactors.
4. Update tests when behavior changes.
5. Preserve the operator/worker contract: task lifecycle, PR lifecycle, and memory behavior are core invariants.

## Build, lint, and test commands

### Root-level / cross-cutting

```bash
# Build container images
docker build -t bulletfarm/operator:latest operator/
docker build -t bulletfarm/worker:latest worker/

# Run full E2E suite
./tests/e2e/setup.sh
./tests/e2e/run.sh

# Check E2E status / cleanup
./tests/e2e/run.sh --status
./tests/e2e/run.sh --cleanup
```

Notes:

- There is no root app `package.json`; `.opencode/package.json` is agent tooling only.
- E2E tests require Minikube, Elasticsearch, secrets, `gh`, and deployed images.

### Operator (`operator/`)

Canonical commands come from `operator/Makefile`.

```bash
# Help / discover targets
cd operator && make help

# Build
cd operator && make build

# Run locally
cd operator && make run

# Unit/integration-style controller tests
cd operator && make test

# Lint / auto-fix
cd operator && make lint
cd operator && make lint-fix

# Formatting and static analysis
cd operator && make fmt
cd operator && make vet

# Regenerate generated artifacts
cd operator && make manifests
cd operator && make generate

# Build container image
cd operator && make docker-build IMG=bulletfarm/operator:latest
```

Single-test guidance for the operator:

```bash
# Run the controller test package directly
cd operator && make setup-envtest && \
KUBEBUILDER_ASSETS="$(./bin/setup-envtest use 1.31.0 --bin-dir ./bin -p path)" \
go test ./internal/controller -run TestControllers -v
```

Important:

- The current operator tests are Ginkgo-based and expose one top-level Go test entrypoint: `TestControllers`.
- There are not many fine-grained `TestXxx` entrypoints yet, so package-level runs are the practical “single test” unit.
- `make test` excludes `/e2e` and is the safest default.

### Worker (`worker/`)

```bash
# Install/sync dependencies
cd worker && uv sync

# Run locally
cd worker && uv run uvicorn src.main:app --host 0.0.0.0 --port 8000

# Run full test suite
cd worker && uv run pytest
```

Single-test guidance for the worker:

```bash
# Single file
cd worker && uv run pytest tests/test_main.py

# Single test class
cd worker && uv run pytest tests/test_main.py::TestHealthEndpoint

# Single test function
cd worker && uv run pytest tests/test_main.py::TestHealthEndpoint::test_health_returns_200

# Name filter
cd worker && uv run pytest -k health_returns_200
```

Important:

- `pytest`, `pytest-asyncio`, and `httpx` are configured as dev dependencies in `worker/pyproject.toml`.
- No dedicated Python lint/type-check command is configured in this repo; do not invent `ruff`, `black`, `mypy`, or `pyright` steps unless you also add and document them.

## Code style guidelines

## Cross-language expectations

- Keep modules focused and small.
- Prefer pure/helper functions over large stateful routines when practical.
- Use explicit dependency injection instead of hidden globals.
- Validate inputs at boundaries.
- Preserve existing task/PR phase names and API field names.
- Add comments/docstrings for non-obvious behavior, not for trivial lines.
- Match existing terminology: agent, task, worker, PR, shared memory, task memory.

## Imports and file structure

### Go

- Let `gofmt` manage formatting and import ordering.
- Keep imports grouped as: standard library, third-party/Kubernetes, then local module imports.
- Keep package names lowercase.
- Shared constants should live in `const` blocks near the top of the file.

### Python

- Put `from __future__ import annotations` at the top of Python modules; this is the project norm.
- Group imports as: standard library, third-party, then `src.*` imports.
- Use one blank line between import groups.
- Keep file names in `snake_case.py`.

## Formatting and naming

### Go

- Use `PascalCase` for exported types and methods.
- Use `camelCase` for unexported helpers, locals, and internal constants.
- Prefer early returns to deep nesting.
- Keep controller logic readable even when files are necessarily long.

### Python

- Use `snake_case` for functions, variables, and module names.
- Use `PascalCase` for classes.
- Use `UPPER_SNAKE_CASE` for module constants.
- Use descriptive names; avoid one-letter variables except for trivial loop indices.

## Types and data modeling

### Go

- Prefer concrete structs over loose maps when modeling API payloads.
- Keep JSON tags accurate and stable.
- For CRD types, maintain kubebuilder comments/markers and field docs.
- Wrap errors with context using `%w` when returning them.

### Python

- Use explicit type annotations on parameters and return values.
- Add local variable annotations when the type is not obvious.
- Use `TypedDict` for structured dict-like service interfaces.
- Use Pydantic models for API request/response models.
- Keep models/data definitions free of business logic when possible.

## Error handling and logging

- Fail loudly at boundaries, but keep error messages specific and actionable.
- In Go, return errors instead of swallowing them; log with contextual fields.
- In Python, catch exceptions at integration boundaries (GitHub, ES, HTTP, subprocess) and convert them into typed status/results where appropriate.
- Use `logger.exception(...)` when preserving stack traces is useful.
- Do not silently change lifecycle semantics such as retry behavior, incomplete-task handling, or PR ownership rules.

## Testing conventions

- Put worker tests under `worker/tests/`.
- Worker tests use pytest, often organized as `class Test...` with `test_...` methods.
- Async worker tests use `@pytest.mark.asyncio`.
- Operator tests currently use Ginkgo/envtest in `operator/internal/controller/`.
- When changing public behavior, add or update the nearest relevant test instead of only relying on E2E coverage.

## Architecture-specific guardrails

- The operator never merges or closes PRs; humans do.
- Retries are expected to reuse the same branch/PR when applicable.
- Worker memory writes and retrieval fallbacks are deliberate behavior; do not simplify them casually.
- Keep the operator/worker HTTP contract stable unless you update both sides together.
- Do not replace configured commands with ad hoc scripts when a Makefile or README command already exists.

## Documentation expectations

- Keep docs concise, practical, and command-oriented.
- Prefer concrete commands over abstract descriptions.
- If you add a new required workflow or tool, update the nearest README/doc alongside the code.
