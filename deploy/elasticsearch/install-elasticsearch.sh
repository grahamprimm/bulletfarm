#!/usr/bin/env bash
# Install Elasticsearch on Minikube via Helm
# Usage: bash deploy/install-elasticsearch.sh
#
# Prerequisites:
#   - Minikube running (minikube status)
#   - Helm 3.x installed (helm version)
#
# Why DOCKER_API_VERSION=1.44: The Docker client version is older than the
# daemon requires; this env var forces compatibility.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VALUES_FILE="${SCRIPT_DIR}/elasticsearch-values.yaml"

echo "==> Setting Docker API compatibility..."
export DOCKER_API_VERSION=1.44

echo "==> Adding Elastic Helm repository..."
helm repo add elastic https://helm.elastic.co 2>/dev/null || true
helm repo update

echo "==> Installing Elasticsearch (single-node, dev config)..."
helm install elasticsearch elastic/elasticsearch \
  -f "${VALUES_FILE}" \
  --wait \
  --timeout 5m

echo "==> Verifying installation..."
echo ""
echo "--- Pods ---"
kubectl get pods -l app=elasticsearch-master

echo ""
echo "--- Service ---"
kubectl get svc elasticsearch-master

echo ""
echo "==> Elasticsearch installed successfully!"
echo "    Internal URL: http://elasticsearch-master:9200"
echo "    Namespace:    default"
echo ""
echo "    Quick health check from within the cluster:"
echo "    kubectl run curl-test --rm -it --image=curlimages/curl -- curl http://elasticsearch-master:9200/_cluster/health?pretty"
