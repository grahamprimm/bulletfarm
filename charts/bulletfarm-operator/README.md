# BulletFarm Operator Helm Chart

Production-ready Helm chart for deploying the BulletFarm agent operator, CRDs, RBAC, and optional agent/task definitions.

## Installation

### Quick Start (Minikube)

```bash
# Build images first
eval $(minikube docker-env)
docker build -t bulletfarm/operator:latest operator/
docker build -t bulletfarm/worker:latest worker/

# Create secrets
kubectl create secret generic bulletfarm-secrets \
  --from-literal=github-token=ghp_xxx \
  --from-literal=openai-api-key=sk-xxx

# Install
helm install bulletfarm charts/bulletfarm-operator/ \
  --set image.pullPolicy=Never \
  --set image.tag=latest \
  --set workerImage.pullPolicy=Never
```

### Production

```bash
helm install bulletfarm charts/bulletfarm-operator/ \
  --set image.repository=myregistry/bulletfarm-operator \
  --set image.tag=v0.2.0 \
  --set workerImage.repository=myregistry/bulletfarm-worker \
  --set workerImage.tag=v0.2.0 \
  --set leaderElection.enabled=true \
  --set replicaCount=2 \
  --set metrics.enabled=true
```

## Configuration

### Key Values

| Value | Default | Description |
|-------|---------|-------------|
| `image.repository` | `bulletfarm/operator` | Operator image |
| `image.tag` | `""` (appVersion) | Operator image tag |
| `workerImage.repository` | `bulletfarm/worker` | Default worker image |
| `workerImage.tag` | `latest` | Default worker image tag |
| `replicaCount` | `1` | Operator replicas |
| `leaderElection.enabled` | `false` | Enable for multi-replica HA |
| `elasticsearch.url` | `http://elasticsearch-master:9200` | ES endpoint |
| `llm.provider` | `openai` | Default LLM provider |
| `llm.model` | `gpt-4o-mini` | Default LLM model |
| `secretName` | `bulletfarm-secrets` | K8s Secret with API keys |
| `installCRDs` | `true` | Install CRDs with chart |

### Secrets

The chart expects a Kubernetes Secret with these keys:

| Key | Required | Description |
|-----|----------|-------------|
| `github-token` | Yes | GitHub personal access token with `repo` scope |
| `openai-api-key` | If using OpenAI | OpenAI API key |
| `ollama-base-url` | If using Ollama | Ollama endpoint URL |

Create manually:

```bash
kubectl create secret generic bulletfarm-secrets \
  --from-literal=github-token=ghp_xxx \
  --from-literal=openai-api-key=sk-xxx
```

Or via chart values (not recommended for production):

```yaml
createSecret:
  enabled: true
  githubToken: "ghp_xxx"
  openaiApiKey: "sk-xxx"
```

### Defining Agents in values.yaml

```yaml
agents:
  my-agent:
    repository: "myorg/myrepo"
    branch: "main"
    workerImage: "bulletfarm/worker:latest"
    defaultSkills:
      - code-edit
      - testing
    config:
      llmProvider: openai
      llmModel: gpt-4o-mini
```

### Defining Tasks in values.yaml

```yaml
agentTasks:
  fix-readme:
    agentRef: "my-agent"
    prompt: "Fix markdown formatting in README.md"
    targetBranch: "fix-readme"
    skills:
      - documentation
    maxRetries: 2
```

## What Gets Deployed

| Resource | Description |
|----------|-------------|
| Deployment | Operator controller manager |
| ServiceAccount | With RBAC for CRDs, pods, secrets |
| ClusterRole + Binding | Permissions for Agent, AgentTask, Pod, Secret resources |
| CRDs | `agents.agents.bulletfarm.io`, `agenttasks.agents.bulletfarm.io` |
| Agent CRs | (optional) From `agents` values |
| AgentTask CRs | (optional) From `agentTasks` values |
| Service | (optional) Metrics endpoint |
| ServiceMonitor | (optional) Prometheus scraping |
| NetworkPolicy | (optional) Network isolation |
| PodDisruptionBudget | (optional) HA protection |

## Upgrading

```bash
helm upgrade bulletfarm charts/bulletfarm-operator/ --set image.tag=v0.3.0
```

CRDs are updated automatically if `installCRDs: true`.

## Uninstalling

```bash
helm uninstall bulletfarm

# CRDs are NOT deleted by helm uninstall (by design)
# To remove CRDs manually:
kubectl delete crd agents.agents.bulletfarm.io agenttasks.agents.bulletfarm.io
```
