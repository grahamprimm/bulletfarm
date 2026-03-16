# Elasticsearch Installation (Minikube)

Single-node Elasticsearch for the bulletfarm agent operator's memory store, deployed via Helm on Minikube.

## Prerequisites

- Minikube running (`minikube status` shows all components healthy)
- Helm 3.x installed (`helm version`)
- `DOCKER_API_VERSION=1.44` exported (Docker client/daemon compatibility)

## Installation

### Quick Start

```bash
bash deploy/install-elasticsearch.sh
```

### Manual Steps

1. **Add the Elastic Helm repo**:
   ```bash
   helm repo add elastic https://helm.elastic.co
   helm repo update
   ```

2. **Install with dev values**:
   ```bash
   export DOCKER_API_VERSION=1.44
   helm install elasticsearch elastic/elasticsearch \
     -f deploy/elasticsearch-values.yaml \
     --wait --timeout 5m
   ```

## Configuration

Values file: [`deploy/elasticsearch-values.yaml`](../deploy/elasticsearch-values.yaml)

| Setting | Value | Why |
|---------|-------|-----|
| `replicas` | 1 | Single-node dev setup |
| `discovery.type` | single-node | Skips bootstrap checks |
| `persistence.enabled` | false | Dev data is ephemeral; avoids PV issues on Minikube |
| `resources.requests.memory` | 1Gi | Fits within typical Minikube allocation |
| `resources.limits.memory` | 2Gi | Prevents OOM while leaving room for other pods |
| `esJavaOpts` | -Xmx1g -Xms1g | JVM heap = half of memory limit (best practice) |

## Connection

- **Internal (in-cluster)**: `http://elasticsearch-master:9200`
- **Namespace**: `default`
- **Service name**: `elasticsearch-master`
- **Port**: `9200`

## Verification

```bash
export DOCKER_API_VERSION=1.44

# Check pod status (should be Running, 1/1 Ready)
kubectl get pods -l app=elasticsearch-master

# Check service exists
kubectl get svc elasticsearch-master

# Health check from within the cluster
kubectl run curl-test --rm -it --image=curlimages/curl -- \
  curl http://elasticsearch-master:9200/_cluster/health?pretty
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Pod stuck in `Pending` | Reduce memory requests in values file, or increase Minikube resources: `minikube config set memory 4096` |
| Helm timeout | Increase timeout: `--timeout 10m` |
| `ImagePullBackOff` | Check Minikube network: `minikube ssh -- ping docker.elastic.co` |
| Docker API version error | Ensure `export DOCKER_API_VERSION=1.44` is set before commands |

## Uninstall

```bash
export DOCKER_API_VERSION=1.44
helm uninstall elasticsearch
```
