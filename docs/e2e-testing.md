# BulletFarm E2E Testing Guide

## Overview

The E2E test suite validates the complete operator lifecycle across multiple agents, repositories, and task types. It runs against a live Minikube cluster and produces a Markdown report.

**Latest results: 83 passed, 0 failed, 0 skipped (218 seconds)**

## What It Tests

- **3 agents** operating on **3 repositories** (Python, Go, Node.js)
- **6 concurrent tasks** with different skill combinations
- **Full lifecycle**: CRD creation → pod spawn → LLM execution → git clone/branch/commit/push → draft PR → mark ready → ES memory
- **PR labels**: `bulletfarm/agent`, `bulletfarm/task:{id}`, `bulletfarm/agent:{name}`
- **PR merge/close detection**: operator detects human action, graduates memory, cleans up pods
- **Memory graduation**: task_memory → shared_memory on PR merge/close, then task_memory deleted
- **10 validation checks per task** (60 total) + **5 post-merge checks per PR** (15 total)

## Test Matrix

| ID | Agent | Repository | Language | Task | Skills |
|----|-------|-----------|----------|------|--------|
| S1 | alpha-agent | bulletfarm-test-repo-alpha | Python | Add error handling | code-edit |
| S2 | alpha-agent | bulletfarm-test-repo-alpha | Python | Generate pytest tests | code-edit, testing |
| S3 | beta-agent | bulletfarm-test-repo-beta | Go | Add structured logging | code-edit |
| S4 | beta-agent | bulletfarm-test-repo-beta | Go | Update README docs | documentation, doc-update |
| S5 | gamma-agent | bulletfarm-test-repo-gamma | Node.js | Add input validation | code-edit |
| S6 | gamma-agent | bulletfarm-test-repo-gamma | Node.js | Generate Jest tests | code-edit, testing |

## Validation Checks

### Per Task (Phase 6)

| # | Check | Pass Condition |
|---|-------|---------------|
| 1 | Phase | `WaitingForPR` or `Completed` |
| 2 | Progress | `100` |
| 3 | Worker Pod | `status.workerPod` is non-empty |
| 4 | PR URL | Contains `github.com` |
| 5 | PR State | `OPEN` and ready for review (not draft) |
| 6 | Timestamps | `startedAt` and `completedAt` populated |
| 7 | ES Memory | `task_memory` has Succeeded or Incomplete entry |
| 8 | PR Agent Label | PR has `bulletfarm/agent` label |
| 9 | PR Task Label | PR has `bulletfarm/task:{name}` label |
| 10 | Pod Alive | Worker pod still Running (kept alive for PR watching) |

### Post-Merge/Close (Phase 8)

| # | Check | Pass Condition |
|---|-------|---------------|
| 1 | Phase | `Merged` or `Closed` (matches action taken) |
| 2 | PR State | `prState` field matches |
| 3 | Task Memory | `task_memory` count is 0 (graduated) |
| 4 | Shared Memory | `shared_memory` has entries |
| 5 | Pod Cleanup | Worker pod deleted |

## Running

### Prerequisites

- Minikube running with operator deployed via Helm
- Elasticsearch running
- `bulletfarm-secrets` configured with GitHub token and OpenAI key
- `gh` CLI authenticated
- Worker and operator images built in Minikube's Docker

### Full Suite

```bash
./tests/e2e/run.sh
```

### Check Status (while running)

```bash
./tests/e2e/run.sh --status
```

### Cleanup Only

```bash
./tests/e2e/run.sh --cleanup
```

## Execution Phases

| Phase | Description | Duration |
|-------|-------------|----------|
| 1. Prerequisites | Verify Minikube, operator, ES, secrets, CRDs, repos | ~5s |
| 2. Cleanup | Delete previous test resources, close stale PRs | ~5s |
| 3. Deploy Agents | Apply 3 Agent CRs | ~3s |
| 4. Deploy Tasks | Apply 6 AgentTask CRs simultaneously | ~3s |
| 5. Wait | Poll all tasks until terminal state (timeout 480s) | ~60-120s |
| 6. Validation | Run 10 checks per task (60 total) | ~20s |
| 7. PR Merge/Close | Simulate human merging 2 PRs and closing 1 | ~10s |
| 8. Post-Merge | Wait 90s, then run 5 checks per actioned PR (15 total) | ~95s |
| 9. Report | Generate Markdown report | ~2s |

## Output

| File | Description |
|------|-------------|
| `tests/e2e/results/e2e-YYYYMMDD-HHMMSS.log` | Raw test log with timestamps |
| `tests/e2e/results/e2e-YYYYMMDD-HHMMSS-report.md` | Formatted Markdown report |

## Extending

### Adding a Scenario

1. Add Agent CR to `manifests/agents.yaml` (if new repo)
2. Add AgentTask CR to `manifests/tasks.yaml`
3. Update `TOTAL_TASKS` in `run.sh`
4. Add task name to `TASKS` array
5. Add `validate_task` call in Phase 6

### Adding a Validation Check

Add to `validate_task()` in `run.sh`:

```bash
VALUE=$(kubectl get agenttask "$task_name" -o jsonpath='{.status.field}')
if [[ "$VALUE" == "expected" ]]; then
    pass "$scenario: Description"
else
    fail "$scenario: Description (got '$VALUE')"
    task_pass=false
fi
```

## Known Behaviors

- **Incomplete tasks** are treated as failures and retried. The E2E test accepts both `Succeeded` and `Incomplete` phases in ES memory.
- **Same-branch retries**: failed tasks retry on the same branch/PR. The E2E test doesn't explicitly test retries (tasks are configured with `maxRetries: 2` in the manifests).
- **ES queries** use `python3 -c json.load` instead of `grep -oP` to avoid `pipefail` issues in bash.
