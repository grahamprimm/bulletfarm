# BulletFarm

A Kubernetes operator that orchestrates AI agent workers to perform coding tasks on GitHub repositories. Agents clone repos, make changes using LLM-powered tools, create draft pull requests, and track progress through Elasticsearch memory.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                         Kubernetes Cluster                            │
│                                                                      │
│  ┌─────────────────────┐     ┌────────────────────────────────────┐  │
│  │  Operator (Go)      │     │  Elasticsearch                     │  │
│  │                     │     │  ├─ task_memory   (per-task)        │  │
│  │  Watches:           │     │  └─ shared_memory (cross-task)      │  │
│  │  ├─ Agent CRDs      │     └────────────────────────────────────┘  │
│  │  └─ AgentTask CRDs  │                                            │
│  │                     │     ┌────────────────────────────────────┐  │
│  │  For each task:     │     │  Worker Pod (Python)               │  │
│  │  1. Spawn worker pod│────▶│  ├─ FastAPI REST API               │  │
│  │  2. Poll /status    │     │  ├─ LangChain + OpenAI/Ollama      │  │
│  │  3. Call /finalize  │     │  ├─ Git clone/branch/commit/push   │  │
│  │  4. Watch PR state  │     │  ├─ PyGithub (draft PR + labels)   │  │
│  │  5. Graduate memory │     │  └─ ES memory read/write           │  │
│  └─────────────────────┘     └────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
                              ┌─────────────────┐
                              │  GitHub          │
                              │  ├─ Clone repo   │
                              │  ├─ Push branch  │
                              │  ├─ Draft PR     │
                              │  └─ Labels       │
                              └─────────────────┘
```

## Components

| Component | Language | Location | Description |
|-----------|----------|----------|-------------|
| **Operator** | Go | `operator/` | Kubernetes controller that watches Agent/AgentTask CRDs, spawns worker pods, polls status, and manages the full task lifecycle |
| **Worker** | Python | `worker/` | FastAPI service with LangChain agent that clones repos, makes code changes, creates PRs, and stores results in Elasticsearch |
| **Helm Chart** | YAML | `charts/bulletfarm-operator/` | Production-ready Helm chart for deploying the operator, CRDs, RBAC, and optional agents/tasks |
| **E2E Tests** | Bash | `tests/e2e/` | Automated end-to-end test suite that validates the full lifecycle across 3 agents, 3 repos, and 6 tasks |

## Quick Start

### Prerequisites

- [Minikube](https://minikube.sigs.k8s.io/) running
- [Helm](https://helm.sh/) 3.x
- [Docker](https://www.docker.com/) (Minikube's Docker daemon)
- GitHub personal access token with `repo` scope
- OpenAI API key (or Ollama for local LLM)

### 1. Start Minikube

```bash
minikube start --memory 6144 --cpus 4
export DOCKER_API_VERSION=1.44
```

### 2. Install Elasticsearch

```bash
bash deploy/elasticsearch/install-elasticsearch.sh
```

### 3. Build Images

```bash
eval $(minikube docker-env)

# Build operator
docker build -t bulletfarm/operator:latest operator/

# Build worker
docker build -t bulletfarm/worker:latest worker/
```

### 4. Create Secrets

```bash
kubectl create secret generic bulletfarm-secrets \
  --from-literal=github-token=ghp_your_token \
  --from-literal=openai-api-key=sk-your_key
```

### 5. Install with Helm

```bash
helm install bulletfarm charts/bulletfarm-operator/ \
  --set image.pullPolicy=Never \
  --set image.tag=latest \
  --set workerImage.pullPolicy=Never
```

### 6. Create an Agent

```yaml
apiVersion: agents.bulletfarm.io/v1alpha1
kind: Agent
metadata:
  name: my-agent
spec:
  repository: "myorg/myrepo"
  branch: "main"
  workerImage: "bulletfarm/worker:latest"
  defaultSkills:
    - code-edit
    - testing
  config:
    llmProvider: "openai"
    llmModel: "gpt-4o-mini"
    elasticsearchURL: "http://elasticsearch-master:9200"
```

### 7. Create a Task

```yaml
apiVersion: agents.bulletfarm.io/v1alpha1
kind: AgentTask
metadata:
  name: add-tests
spec:
  agentRef: "my-agent"
  description: "Add unit tests"
  prompt: "Create unit tests for the main API endpoints"
  targetBranch: "add-tests"
  skills:
    - code-edit
    - testing
```

### 8. Watch Progress

```bash
kubectl get agenttasks -w
```

## Task Lifecycle

```
AgentTask created
    │
    ▼
┌─────────┐     ┌─────────┐     ┌──────────────┐     ┌────────┐
│ Pending  │────▶│ Running │────▶│ WaitingForPR │────▶│ Merged │
└─────────┘     └────┬────┘     └──────────────┘     └────────┘
                     │                  │
                     ▼                  ▼
                ┌────────┐        ┌────────┐
                │ Failed │        │ Closed │
                └───┬────┘        └────────┘
                    │
                    ▼ (retry on same branch)
                ┌─────────┐
                │ Pending  │
                └─────────┘
```

**Phases:**

| Phase | Description |
|-------|-------------|
| `Pending` | Task created, waiting for worker pod |
| `Running` | Worker pod active, LLM agent executing |
| `WaitingForPR` | Task complete, draft PR created and marked ready for review, waiting for human action |
| `Merged` | Human merged the PR — task memory graduated to shared memory, pod cleaned up |
| `Closed` | Human closed the PR without merging — memory graduated, pod cleaned up |
| `Failed` | Task failed — will retry (up to `maxRetries`) on the same branch/PR |

**Key behaviors:**

- Agent marks PRs ready for review on success — **never merges or closes PRs**
- Retries push new commits to the **same branch and PR** to preserve progress
- Failed tasks with exhausted retries leave the PR open for human review
- Incomplete tasks (no meaningful changes) create a draft PR with an explanatory comment

## CRD Reference

### Agent

```yaml
apiVersion: agents.bulletfarm.io/v1alpha1
kind: Agent
metadata:
  name: github-agent
spec:
  repository: "org/repo"           # GitHub repository
  branch: "main"                   # Base branch
  workerImage: "bulletfarm/worker:latest"
  defaultSkills:                   # Skills loaded for every task
    - code-edit
    - testing
    - documentation
  config:
    llmProvider: "openai"          # openai or ollama
    llmModel: "gpt-4o-mini"
    elasticsearchURL: "http://elasticsearch-master:9200"
status:
  ready: true
  activeTasks: 2
  runningTasks: ["task-1", "task-2"]
  lastReconciled: "2026-03-15T23:00:00Z"
```

### AgentTask

```yaml
apiVersion: agents.bulletfarm.io/v1alpha1
kind: AgentTask
metadata:
  name: add-pagination
spec:
  agentRef: "github-agent"        # References an Agent CR
  description: "Add pagination"
  prompt: "Add cursor-based pagination to the REST API"
  targetBranch: "add-pagination"  # Branch name (auto-generated if empty)
  skills:                         # Merged with agent's defaultSkills
    - code-edit
    - graphql
  maxRetries: 2                   # Retry attempts on failure (default 2)
status:
  phase: "WaitingForPR"
  progress: 100
  pullRequestURL: "https://github.com/org/repo/pull/42"
  prState: "open"
  workerPod: "worker-add-pagination"
  retryCount: 0
  startedAt: "2026-03-15T23:00:00Z"
  completedAt: "2026-03-15T23:01:30Z"
  message: "Task completed — PR marked ready for review"
```

## Available Skills

| Skill | Tool | Description |
|-------|------|-------------|
| `code-edit` | `code_edit` | Edit existing files or create new files |
| `testing` | `test_generator` | Generate test stubs for Python (pytest) and JavaScript (Jest) |
| `documentation` | `doc_update` | Update or append sections in documentation files |
| `graphql` | `graphql_debug` | Debug GraphQL queries against endpoints |

**Core tools** (always available, not skill-dependent):

| Tool | Description |
|------|-------------|
| `read_file` | Read file contents |
| `list_files` | List files in a directory |
| `search_shared_knowledge` | Search Elasticsearch shared memory |

## Elasticsearch Memory

Two indices store agent knowledge:

| Index | Purpose | Lifecycle |
|-------|---------|-----------|
| `task_memory` | Per-task results, methodology, tools used, files modified | Deleted when PR is merged/closed (graduated to shared) |
| `shared_memory` | Cross-task knowledge, generalizable learnings | Permanent — enriched on each PR merge/close |

Memory is **never used for code** — only for task metadata, methodology, and outcomes.

## PR Labels

Every PR created by an agent gets three labels:

| Label | Example | Purpose |
|-------|---------|---------|
| `bulletfarm/agent` | `bulletfarm/agent` | Identifies agent-created PRs |
| `bulletfarm/task:{id}` | `bulletfarm/task:add-tests` | Links PR to specific task |
| `bulletfarm/agent:{name}` | `bulletfarm/agent:my-agent` | Links PR to specific agent |

## Project Structure

```
bulletfarm/
├── operator/                          # Go Kubernetes operator
│   ├── api/v1alpha1/                  # CRD type definitions
│   ├── cmd/main.go                    # Entry point
│   ├── internal/controller/           # Reconcile loop logic
│   ├── config/                        # Kustomize manifests
│   ├── Dockerfile
│   └── Makefile
├── worker/                            # Python FastAPI + LangChain worker
│   ├── src/
│   │   ├── main.py                    # FastAPI app
│   │   ├── agent.py                   # LangChain agent + tools
│   │   ├── config.py                  # Pydantic settings
│   │   ├── github_tools.py            # PyGithub wrapper
│   │   ├── memory.py                  # Elasticsearch memory store
│   │   └── models.py                  # Request/response models
│   ├── tests/
│   ├── Dockerfile
│   └── pyproject.toml
├── charts/bulletfarm-operator/        # Helm chart
│   ├── Chart.yaml
│   ├── values.yaml
│   ├── crds/
│   └── templates/
├── deploy/elasticsearch/              # ES Helm values + install script
├── tests/e2e/                         # Automated E2E test suite
│   ├── run.sh
│   ├── manifests/
│   └── results/
└── docs/                              # Documentation
    ├── e2e-testing.md
    └── elasticsearch-install.md
```

## Documentation

| Document | Description |
|----------|-------------|
| [Operator README](operator/README.md) | Go operator architecture, reconcile loop, CRD types |
| [Worker README](worker/README.md) | Python worker API, LangChain agent, tool system |
| [Helm Chart README](charts/bulletfarm-operator/README.md) | Chart values, installation, configuration |
| [E2E Testing Guide](docs/e2e-testing.md) | Test matrix, validation checks, running tests |
| [Elasticsearch Install](docs/elasticsearch-install.md) | ES Helm setup for Minikube |

## Development

### Build

```bash
# Operator
cd operator && go build -o bin/manager ./cmd/main.go

# Worker
cd worker && uv sync

# Docker images (in Minikube)
eval $(minikube docker-env)
docker build -t bulletfarm/operator:latest operator/
docker build -t bulletfarm/worker:latest worker/
```

### Run E2E Tests

```bash
./tests/e2e/run.sh
```

### Regenerate CRDs

```bash
cd operator
controller-gen object:headerFile="hack/boilerplate.go.txt" paths="./..."
controller-gen crd:maxDescLen=0 paths="./..." output:crd:artifacts:config=config/crd/bases
```

## License

Apache License 2.0
