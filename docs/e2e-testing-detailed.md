# End-to-End Testing Guide (Detailed)

This comprehensive guide covers the complete end-to-end (e2e) test suite for BulletFarm, including automated setup, execution, troubleshooting, and CI/CD integration.

> **Quick Start**: See [e2e-testing.md](e2e-testing.md) for a concise overview. This document provides detailed instructions for setup and troubleshooting.

## Table of Contents

- [Overview](#overview)
- [Quick Start](#quick-start)
- [Prerequisites](#prerequisites)
- [Automated Setup](#automated-setup)
- [Manual Setup](#manual-setup)
- [Running Tests](#running-tests)
- [Test Matrix](#test-matrix)
- [Test Results](#test-results)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [CI/CD Integration](#cicd-integration)
- [Best Practices](#best-practices)

## Overview

The e2e test suite validates the complete BulletFarm system in a real Kubernetes environment:

- **3 Agents** across 3 different GitHub repositories
- **6 Tasks** total (2 per agent)
- **Full lifecycle** from CRD creation to PR merge
- **Real LLM execution** with actual code changes
- **Elasticsearch integration** with memory writes and retrieval
- **Concurrent execution** with proper isolation

**Latest results**: 83 passed, 0 failed, 0 skipped (218 seconds)

## Quick Start

```bash
# 1. Set environment variables
export GITHUB_TOKEN="ghp_your_token_here"
export OPENAI_API_KEY="sk_your_key_here"

# 2. Run automated setup
./tests/e2e/setup.sh

# 3. Run e2e tests
./tests/e2e/run.sh

# 4. View results
cat tests/e2e/results/e2e-*-report.md
```

## Prerequisites

### Required Tools

| Tool | Version | Installation |
|------|---------|--------------|
| **Minikube** | Latest | https://minikube.sigs.k8s.io/docs/start/ |
| **kubectl** | 1.28+ | https://kubernetes.io/docs/tasks/tools/ |
| **Helm** | 3.x | https://helm.sh/docs/intro/install/ |
| **Docker** | Latest | https://docs.docker.com/get-docker/ |
| **gh CLI** | Latest (optional) | https://cli.github.com/ |

### Required Credentials

1. **GitHub Personal Access Token**
   - Scope: `repo` (full repository access)
   - Create at: https://github.com/settings/tokens
   - Set as: `export GITHUB_TOKEN="ghp_..."`

2. **OpenAI API Key**
   - Get from: https://platform.openai.com/api-keys
   - Set as: `export OPENAI_API_KEY="sk-..."`
   - Alternative: Use Ollama for local LLM (see Configuration)

### System Requirements

- **Memory**: 8GB+ available (6GB for Minikube + 2GB for host)
- **CPU**: 4+ cores
- **Disk**: 20GB+ free space
- **Network**: Internet access for pulling images and LLM API calls

## Automated Setup

### Using the Setup Script (Recommended)

The automated setup script (`tests/e2e/setup.sh`) handles all configuration:

```bash
# Full setup (recommended for first run)
./tests/e2e/setup.sh

# Skip Minikube start (if already running)
./tests/e2e/setup.sh --skip-minikube

# Skip image builds (if already built)
./tests/e2e/setup.sh --skip-images

# Validate environment only (no changes)
./tests/e2e/setup.sh --validate-only
```

**What the script does:**
1. ✓ Validates prerequisites (tools, credentials)
2. ✓ Starts Minikube with appropriate resources
3. ✓ Installs Elasticsearch via Helm
4. ✓ Builds operator and worker Docker images
5. ✓ Creates Kubernetes secrets
6. ✓ Installs BulletFarm operator via Helm
7. ✓ Validates the complete environment

### Setup Script Options

| Option | Description |
|--------|-------------|
| `--skip-minikube` | Skip Minikube start (use if already running) |
| `--skip-images` | Skip Docker image builds (use if already built) |
| `--validate-only` | Only validate environment, don't make changes |
| `--help` | Show usage information |

### Environment Variables for Setup

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_TOKEN` | (required) | GitHub personal access token |
| `OPENAI_API_KEY` | (required) | OpenAI API key |
| `MINIKUBE_MEMORY` | `6144` | Memory for Minikube (MB) |
| `MINIKUBE_CPUS` | `4` | CPUs for Minikube |

## Manual Setup

If you prefer manual setup or need to troubleshoot:

### 1. Start Minikube

```bash
minikube start --memory 6144 --cpus 4 --driver=docker
export DOCKER_API_VERSION=1.44
```

### 2. Install Elasticsearch

```bash
bash deploy/elasticsearch/install-elasticsearch.sh

# Wait for Elasticsearch to be ready
kubectl wait --for=condition=ready pod -l app=elasticsearch-master --timeout=300s
```

### 3. Build Docker Images

```bash
# Configure Docker to use Minikube's daemon
eval $(minikube docker-env)
export DOCKER_API_VERSION=1.44

# Build operator image
docker build -t bulletfarm/operator:latest operator/

# Build worker image
docker build -t bulletfarm/worker:latest worker/
```

### 4. Create Kubernetes Secrets

```bash
kubectl create secret generic bulletfarm-secrets \
  --from-literal=github-token="${GITHUB_TOKEN}" \
  --from-literal=openai-api-key="${OPENAI_API_KEY}"
```

### 5. Install Operator

```bash
helm install bulletfarm charts/bulletfarm-operator/ \
  --set image.pullPolicy=Never \
  --set image.tag=latest \
  --set workerImage.pullPolicy=Never \
  --set workerImage.tag=latest

# Wait for operator to be ready
kubectl wait --for=condition=ready pod \
  -l app.kubernetes.io/name=bulletfarm-operator \
  --timeout=120s
```

## Running Tests

### Full Test Suite

Run all tests (3 agents, 6 tasks):

```bash
./tests/e2e/run.sh
```

**Expected duration**: 15-30 minutes (depends on LLM response times)

### Check Status

Monitor running tests:

```bash
./tests/e2e/run.sh --status
```

### Cleanup

Remove all test resources:

```bash
./tests/e2e/run.sh --cleanup
```

This removes:
- All Agent CRDs
- All AgentTask CRDs
- All worker pods
- Test results (optional)

## Test Matrix

### Agents

| Agent | Repository | Base Branch | Skills |
|-------|------------|-------------|--------|
| `alpha-agent` | `bulletfarm-test-repo-alpha` | `main` | code-edit, testing |
| `beta-agent` | `bulletfarm-test-repo-beta` | `main` | code-edit, documentation |
| `gamma-agent` | `bulletfarm-test-repo-gamma` | `main` | code-edit, testing, documentation |

### Tasks

| ID | Agent | Repository | Language | Task | Skills |
|----|-------|-----------|----------|------|--------|
| S1 | alpha-agent | bulletfarm-test-repo-alpha | Python | Add error handling | code-edit |
| S2 | alpha-agent | bulletfarm-test-repo-alpha | Python | Generate pytest tests | code-edit, testing |
| S3 | beta-agent | bulletfarm-test-repo-beta | Go | Add structured logging | code-edit |
| S4 | beta-agent | bulletfarm-test-repo-beta | Go | Update README docs | documentation, doc-update |
| S5 | gamma-agent | bulletfarm-test-repo-gamma | Node.js | Add input validation | code-edit |
| S6 | gamma-agent | bulletfarm-test-repo-gamma | Node.js | Generate Jest tests | code-edit, testing |

### Validation Checks

#### Per Task (60 total checks)

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

#### Post-Merge/Close (15 total checks)

| # | Check | Pass Condition |
|---|-------|---------------|
| 1 | Phase | `Merged` or `Closed` (matches action taken) |
| 2 | PR State | `prState` field matches |
| 3 | Task Memory | `task_memory` count is 0 (graduated) |
| 4 | Shared Memory | `shared_memory` has entries |
| 5 | Pod Cleanup | Worker pod deleted |

## Test Results

### Output Files

Test results are saved to `tests/e2e/results/`:

```
tests/e2e/results/
├── e2e-20260317-143022.log          # Full execution log
└── e2e-20260317-143022-report.md    # Summary report
```

### Report Format

The Markdown report includes:

```markdown
# BulletFarm E2E Test Report

**Date**: 2026-03-17 14:30:22
**Duration**: 218 seconds
**Status**: PASS

## Summary

- Total Checks: 83
- Passed: 83
- Failed: 0
- Skipped: 0

## Task Results

### S1: Add error handling (alpha-agent)
- Status: PASS
- Duration: 3m 12s
- PR: https://github.com/org/bulletfarm-test-repo-alpha/pull/42
- Worker Pod: worker-task-s1-xyz
...
```

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | All tests passed |
| `1` | One or more tests failed |
| `2` | Setup/validation error |

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_TOKEN` | (required) | GitHub personal access token |
| `OPENAI_API_KEY` | (required) | OpenAI API key |
| `MINIKUBE_MEMORY` | `6144` | Memory for Minikube (MB) |
| `MINIKUBE_CPUS` | `4` | CPUs for Minikube |
| `TASK_TIMEOUT` | `480` | Task timeout (seconds) |
| `POLL_INTERVAL` | `15` | Status poll interval (seconds) |

### Using Ollama (Local LLM)

To use Ollama instead of OpenAI:

1. Install Ollama: https://ollama.ai/
2. Start Ollama server: `ollama serve`
3. Pull a model: `ollama pull llama2`
4. Update agent manifests to use Ollama:

```yaml
apiVersion: agents.bulletfarm.io/v1alpha1
kind: Agent
metadata:
  name: alpha-agent
spec:
  config:
    llmProvider: "ollama"
    llmModel: "llama2"
    ollamaBaseURL: "http://host.minikube.internal:11434"
```

## Troubleshooting

### Common Issues

#### 1. Minikube Won't Start

**Symptom**: `minikube start` fails or hangs

**Solutions**:
```bash
# Delete and recreate cluster
minikube delete
minikube start --memory 6144 --cpus 4

# Try different driver
minikube start --driver=virtualbox  # or hyperkit, kvm2
```

#### 2. Elasticsearch Not Ready

**Symptom**: Elasticsearch pod stuck in Pending or CrashLoopBackOff

**Solutions**:
```bash
# Check pod status
kubectl get pods -l app=elasticsearch-master

# Check logs
kubectl logs -l app=elasticsearch-master

# Increase resources
minikube delete
minikube start --memory 8192 --cpus 4
```

#### 3. Worker Pod ImagePullBackOff

**Symptom**: Worker pod can't pull images

**Solutions**:
```bash
# Ensure Docker is using Minikube's daemon
eval $(minikube docker-env)
export DOCKER_API_VERSION=1.44

# Rebuild images
docker build -t bulletfarm/worker:latest worker/

# Verify images exist in Minikube
minikube ssh docker images | grep bulletfarm
```

#### 4. Tasks Timeout

**Symptom**: Tasks stuck in Running state, never complete

**Solutions**:
```bash
# Check worker pod logs
kubectl logs -l bulletfarm.io/task-id=task-s1

# Check operator logs
kubectl logs -l app.kubernetes.io/name=bulletfarm-operator

# Increase timeout
export TASK_TIMEOUT=600  # 10 minutes
./tests/e2e/run.sh
```

#### 5. GitHub Rate Limits

**Symptom**: Tasks fail with "rate limit exceeded"

**Solutions**:
- Wait for rate limit to reset (check: `gh api rate_limit`)
- Use a different GitHub token
- Reduce concurrent tasks

#### 6. OpenAI Rate Limits

**Symptom**: Tasks fail with "rate_limit" error

**Solutions**:
- Wait for rate limit to reset
- Upgrade OpenAI plan for higher limits
- Use Ollama for local LLM (no rate limits)

### Debug Mode

Enable verbose logging:

```bash
# Set debug level for operator
kubectl set env deployment/bulletfarm-operator LOG_LEVEL=debug

# Set debug level for worker (via Agent CR)
kubectl edit agent alpha-agent
# Add: spec.config.logLevel: "debug"
```

### Manual Inspection

Inspect resources manually:

```bash
# List all agents
kubectl get agents

# Describe agent
kubectl describe agent alpha-agent

# List all tasks
kubectl get agenttasks

# Describe task
kubectl describe agenttask task-s1

# Get worker pod logs
kubectl logs -l bulletfarm.io/task-id=task-s1 --tail=100

# Get operator logs
kubectl logs -l app.kubernetes.io/name=bulletfarm-operator --tail=100

# Check Elasticsearch
kubectl port-forward svc/elasticsearch-master 9200:9200
curl http://localhost:9200/_cat/indices
```

## CI/CD Integration

### GitHub Actions Example

```yaml
name: E2E Tests

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  e2e:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    
    steps:
      - uses: actions/checkout@v3
      
      - name: Setup Minikube
        uses: medyagh/setup-minikube@latest
        with:
          memory: 6144
          cpus: 4
      
      - name: Setup environment
        env:
          GITHUB_TOKEN: ${{ secrets.E2E_GITHUB_TOKEN }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
        run: |
          ./tests/e2e/setup.sh
      
      - name: Run e2e tests
        run: |
          ./tests/e2e/run.sh
      
      - name: Upload results
        if: always()
        uses: actions/upload-artifact@v3
        with:
          name: e2e-results
          path: tests/e2e/results/
```

## Best Practices

### Before Running Tests

1. **Clean environment**: Start with a fresh Minikube cluster
2. **Check credentials**: Verify GitHub token and OpenAI key are valid
3. **Check resources**: Ensure sufficient memory and CPU available
4. **Check network**: Verify internet connectivity for API calls

### During Tests

1. **Monitor logs**: Watch operator and worker logs for issues
2. **Check resources**: Monitor Minikube resource usage
3. **Be patient**: LLM calls can take 30-60 seconds per task
4. **Don't interrupt**: Let tests complete fully for accurate results

### After Tests

1. **Review results**: Check the report for failures
2. **Inspect PRs**: Verify PRs were created correctly on GitHub
3. **Check memory**: Verify Elasticsearch has task and shared memory
4. **Clean up**: Run cleanup to remove test resources

## Performance Benchmarks

Expected performance on recommended hardware:

| Metric | Value |
|--------|-------|
| **Setup time** | 5-10 minutes |
| **Test duration** | 15-30 minutes |
| **Tasks per minute** | 0.2-0.4 |
| **Memory usage** | 4-6 GB |
| **CPU usage** | 50-80% |

Factors affecting performance:
- LLM response time (OpenAI vs Ollama)
- Network latency
- Repository size
- Task complexity

## Additional Resources

- [E2E Testing Overview](e2e-testing.md) - Concise overview
- [Operator README](../operator/README.md) - Operator architecture
- [Worker README](../worker/README.md) - Worker implementation
- [Helm Chart README](../charts/bulletfarm-operator/README.md) - Deployment
- [Elasticsearch Install](elasticsearch-install.md) - ES setup details
