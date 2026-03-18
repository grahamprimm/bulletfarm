# E2E Test Suite

Automated end-to-end tests for BulletFarm that validate the complete system in a real Kubernetes environment.

## Quick Start

```bash
# 1. Set credentials
export GITHUB_TOKEN="ghp_your_token"
export OPENAI_API_KEY="sk_your_key"

# 2. Automated setup
./tests/e2e/setup.sh

# 3. Run tests
./tests/e2e/run.sh
```

## Files

| File | Description |
|------|-------------|
| `setup.sh` | Automated environment setup script |
| `run.sh` | E2E test runner |
| `manifests/` | Agent and AgentTask CRDs for testing |
| `results/` | Test results and reports |

## Documentation

- **[E2E Testing Overview](../../docs/e2e-testing.md)** - Concise overview and test matrix
- **[E2E Testing Detailed](../../docs/e2e-testing-detailed.md)** - Comprehensive setup and troubleshooting guide

## Setup Script

The `setup.sh` script automates the complete environment setup:

```bash
# Full setup (recommended)
./setup.sh

# Skip Minikube start (if already running)
./setup.sh --skip-minikube

# Skip image builds (if already built)
./setup.sh --skip-images

# Validate environment only
./setup.sh --validate-only
```

**What it does:**
1. ✓ Validates prerequisites (tools, credentials)
2. ✓ Starts Minikube (6GB RAM, 4 CPUs)
3. ✓ Installs Elasticsearch
4. ✓ Builds Docker images (operator + worker)
5. ✓ Creates Kubernetes secrets
6. ✓ Installs BulletFarm operator via Helm
7. ✓ Validates complete environment

## Test Runner

The `run.sh` script executes the full test suite:

```bash
# Run all tests
./run.sh

# Check status (while running)
./run.sh --status

# Cleanup resources
./run.sh --cleanup
```

**What it tests:**
- 3 agents across 3 repositories
- 6 concurrent tasks with different skills
- Full lifecycle: CRD → pod → LLM → git → PR → ES memory
- 83 validation checks total (60 per-task + 15 post-merge + 8 prerequisites)

## Prerequisites

### Required Tools
- Minikube
- kubectl
- Helm 3.x
- Docker
- gh CLI (optional)

### Required Credentials
- `GITHUB_TOKEN` - GitHub personal access token (repo scope)
- `OPENAI_API_KEY` - OpenAI API key (or use Ollama)

### System Requirements
- 8GB+ RAM (6GB for Minikube + 2GB for host)
- 4+ CPU cores
- 20GB+ disk space
- Internet access

## Test Matrix

| Agent | Repository | Tasks | Skills |
|-------|-----------|-------|--------|
| alpha-agent | bulletfarm-test-repo-alpha | 2 | code-edit, testing |
| beta-agent | bulletfarm-test-repo-beta | 2 | code-edit, documentation |
| gamma-agent | bulletfarm-test-repo-gamma | 2 | code-edit, testing, documentation |

## Results

Test results are saved to `results/`:

```
results/
├── e2e-20260317-143022.log          # Full execution log
└── e2e-20260317-143022-report.md    # Summary report
```

**Latest results**: 83 passed, 0 failed, 0 skipped (218 seconds)

## Troubleshooting

### Common Issues

**Minikube won't start:**
```bash
minikube delete
minikube start --memory 6144 --cpus 4
```

**Worker pod ImagePullBackOff:**
```bash
eval $(minikube docker-env)
export DOCKER_API_VERSION=1.44
docker build -t bulletfarm/worker:latest worker/
```

**Tasks timeout:**
```bash
# Check worker logs
kubectl logs -l bulletfarm.io/task-id=task-s1

# Check operator logs
kubectl logs -l app.kubernetes.io/name=bulletfarm-operator
```

See [E2E Testing Detailed](../../docs/e2e-testing-detailed.md) for comprehensive troubleshooting.

## CI/CD Integration

Example GitHub Actions workflow:

```yaml
- name: Setup environment
  env:
    GITHUB_TOKEN: ${{ secrets.E2E_GITHUB_TOKEN }}
    OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
  run: ./tests/e2e/setup.sh

- name: Run e2e tests
  run: ./tests/e2e/run.sh
```

## Performance

Expected on recommended hardware:
- Setup: 5-10 minutes
- Test duration: 15-30 minutes
- Tasks per minute: 0.2-0.4

## Additional Resources

- [Operator README](../../operator/README.md)
- [Worker README](../../worker/README.md)
- [Helm Chart README](../../charts/bulletfarm-operator/README.md)
- [Elasticsearch Install](../../docs/elasticsearch-install.md)
