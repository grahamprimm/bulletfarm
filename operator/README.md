# BulletFarm Operator

Kubernetes controller written in Go that manages the lifecycle of AI agent tasks. Built with [controller-runtime](https://github.com/kubernetes-sigs/controller-runtime) (kubebuilder).

## What It Does

The operator watches two Custom Resource Definitions:

- **Agent** — defines a repository, worker image, default skills, and LLM configuration
- **AgentTask** — defines a specific task to perform on an agent's repository

For each AgentTask, the operator:

1. Spawns an ephemeral worker pod with the task payload
2. Polls the worker's `/status` endpoint every 15 seconds
3. Updates the AgentTask status (phase, progress, PR URL)
4. Calls `/finalize` to mark the PR ready for review on success
5. Watches the PR state (via `/pr-status`) until a human merges or closes it
6. Calls `/graduate` to move task memory to shared memory, then cleans up the pod

## Reconcile Loop

```
                    ┌─────────┐
                    │ Pending  │
                    └────┬────┘
                         │ Create worker pod
                         ▼
                    ┌─────────┐
              ┌────▶│ Running │◀──── Poll GET /tasks/{id}/status
              │     └────┬────┘
              │          │
              │    ┌─────┴──────┬──────────────┐
              │    ▼            ▼              ▼
              │ Succeeded   Incomplete      Failed
              │    │            │              │
              │    │            └──▶ Failed ◀──┘
              │    │                   │
              │    ▼                   │ retryCount < maxRetries?
              │ WaitingForPR          │
              │    │                  ├─ Yes: clean pod → Pending (same branch)
              │    │ Poll PR state    └─ No: terminal Failed (PR left open)
              │    │
              │    ├─ merged → Merged (graduate + cleanup)
              │    └─ closed → Closed (graduate + cleanup)
              │
              └──── Requeue after 15s
```

## Key Design Decisions

**Agent never merges or closes PRs.** The operator marks PRs ready for review, then waits for a human to merge or close. This is a deliberate design choice — the agent's job is to produce code, not to approve it.

**Retries reuse the same branch and PR.** When a task fails and retries, the new worker pod pushes additional commits to the existing branch. The PR automatically picks up the new commits. This preserves the full history of attempts.

**Incomplete detection.** If the LLM agent runs but produces no meaningful code changes, the task is marked `Incomplete` (treated as a failure for retry purposes). A draft PR is created with an explanatory comment.

**Owner references.** Worker pods have an owner reference to their AgentTask, so they're garbage-collected if the task is deleted.

## CRD Types

### AgentSpec

| Field | Type | Description |
|-------|------|-------------|
| `repository` | string | GitHub repository (e.g. `org/repo`) |
| `branch` | string | Base branch (default: `main`) |
| `workerImage` | string | Docker image for worker pods |
| `defaultSkills` | []string | Skills loaded for every task |
| `config.llmProvider` | string | `openai` or `ollama` |
| `config.llmModel` | string | Model name (e.g. `gpt-4o-mini`) |
| `config.elasticsearchURL` | string | ES endpoint |

### AgentTaskSpec

| Field | Type | Description |
|-------|------|-------------|
| `agentRef` | string | Name of the Agent CR |
| `description` | string | Human-readable description |
| `prompt` | string | Task instruction for the LLM |
| `targetBranch` | string | Branch name (auto-generated if empty) |
| `skills` | []string | Skills for this task (merged with agent defaults) |
| `maxRetries` | int | Max retry attempts (default 2) |

### AgentTaskStatus

| Field | Type | Description |
|-------|------|-------------|
| `phase` | string | Pending, Running, WaitingForPR, Merged, Closed, Failed |
| `progress` | int | 0–100 |
| `pullRequestURL` | string | GitHub PR URL |
| `prState` | string | open, merged, closed |
| `workerPod` | string | Pod name |
| `retryCount` | int | Current retry count |
| `lastFailureReason` | string | Why the last attempt failed |
| `startedAt` | Time | When the task started |
| `completedAt` | Time | When the task completed |
| `message` | string | Human-readable status |

## Building

```bash
# Install dependencies
go mod tidy

# Generate DeepCopy methods
controller-gen object:headerFile="hack/boilerplate.go.txt" paths="./..."

# Generate CRD manifests
controller-gen crd:maxDescLen=0 paths="./..." output:crd:artifacts:config=config/crd/bases

# Build binary
go build -o bin/manager ./cmd/main.go

# Build Docker image
docker build -t bulletfarm/operator:latest .
```

## Running Locally

```bash
# Outside cluster (uses kubeconfig)
go run ./cmd/main.go

# In cluster (via Helm)
helm install bulletfarm ../charts/bulletfarm-operator/
```

## File Structure

```
operator/
├── api/v1alpha1/
│   ├── agent_types.go          # Agent CRD type definitions
│   ├── agenttask_types.go      # AgentTask CRD type definitions
│   ├── groupversion_info.go    # API group registration
│   └── zz_generated.deepcopy.go
├── cmd/main.go                 # Entry point
├── internal/controller/
│   ├── agenttask_controller.go # Main reconcile loop
│   └── agent_controller.go     # Agent status reconciler
├── config/
│   ├── crd/bases/              # Generated CRD YAMLs
│   ├── rbac/                   # RBAC manifests
│   ├── manager/                # Deployment manifest
│   └── samples/                # Example Agent and AgentTask CRs
├── Dockerfile
├── Makefile
└── go.mod
```
