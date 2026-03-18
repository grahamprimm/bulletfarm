#!/usr/bin/env bash
# =============================================================================
# BulletFarm E2E Test Setup Script
# =============================================================================
# Automates the complete setup of the BulletFarm e2e test environment.
#
# This script:
#   1. Starts Minikube with appropriate resources
#   2. Installs Elasticsearch
#   3. Builds operator and worker Docker images
#   4. Creates Kubernetes secrets
#   5. Installs the BulletFarm operator via Helm
#   6. Validates the environment is ready for e2e tests
#
# Usage:
#   ./tests/e2e/setup.sh                    # Full setup
#   ./tests/e2e/setup.sh --skip-minikube    # Skip Minikube start (already running)
#   ./tests/e2e/setup.sh --skip-images      # Skip image builds (already built)
#   ./tests/e2e/setup.sh --validate-only    # Only validate environment
#
# Prerequisites:
#   - Minikube installed (https://minikube.sigs.k8s.io/)
#   - Helm 3.x installed (https://helm.sh/)
#   - Docker installed
#   - kubectl installed
#   - GitHub personal access token (repo scope)
#   - OpenAI API key (or Ollama for local LLM)
#
# Environment Variables:
#   GITHUB_TOKEN    - GitHub personal access token (required)
#   OPENAI_API_KEY  - OpenAI API key (required unless using Ollama)
#   MINIKUBE_MEMORY - Memory for Minikube (default: 6144)
#   MINIKUBE_CPUS   - CPUs for Minikube (default: 4)
# =============================================================================

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Configuration
MINIKUBE_MEMORY="${MINIKUBE_MEMORY:-6144}"
MINIKUBE_CPUS="${MINIKUBE_CPUS:-4}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Flags
SKIP_MINIKUBE=false
SKIP_IMAGES=false
VALIDATE_ONLY=false

# Parse arguments
for arg in "$@"; do
    case $arg in
        --skip-minikube)
            SKIP_MINIKUBE=true
            shift
            ;;
        --skip-images)
            SKIP_IMAGES=true
            shift
            ;;
        --validate-only)
            VALIDATE_ONLY=true
            shift
            ;;
        --help)
            head -n 30 "$0" | grep "^#" | sed 's/^# //'
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown argument: $arg${NC}"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# --- Logging Functions ---

log() {
    echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $*"
}

success() {
    echo -e "${GREEN}✓${NC} $*"
}

error() {
    echo -e "${RED}✗${NC} $*"
}

warn() {
    echo -e "${YELLOW}⚠${NC} $*"
}

step() {
    echo ""
    echo -e "${BLUE}==>${NC} $*"
}

# --- Validation Functions ---

check_command() {
    local cmd=$1
    local install_url=$2
    
    if ! command -v "$cmd" &> /dev/null; then
        error "$cmd not found"
        echo "  Install from: $install_url"
        return 1
    fi
    success "$cmd found"
    return 0
}

check_env_var() {
    local var_name=$1
    local description=$2
    
    if [[ -z "${!var_name:-}" ]]; then
        error "$var_name not set"
        echo "  $description"
        return 1
    fi
    success "$var_name set"
    return 0
}

validate_prerequisites() {
    step "Validating prerequisites"
    
    local all_ok=true
    
    # Check required commands
    check_command "minikube" "https://minikube.sigs.k8s.io/docs/start/" || all_ok=false
    check_command "kubectl" "https://kubernetes.io/docs/tasks/tools/" || all_ok=false
    check_command "helm" "https://helm.sh/docs/intro/install/" || all_ok=false
    check_command "docker" "https://docs.docker.com/get-docker/" || all_ok=false
    
    # Check required environment variables
    check_env_var "GITHUB_TOKEN" "Set your GitHub personal access token (repo scope)" || all_ok=false
    check_env_var "OPENAI_API_KEY" "Set your OpenAI API key (or use Ollama)" || all_ok=false
    
    if [[ "$all_ok" == "false" ]]; then
        error "Prerequisites not met. Please install missing tools and set environment variables."
        exit 1
    fi
    
    success "All prerequisites met"
}

validate_minikube() {
    step "Validating Minikube"
    
    if ! minikube status &> /dev/null; then
        error "Minikube is not running"
        return 1
    fi
    
    success "Minikube is running"
    
    # Check resources
    local memory=$(minikube config get memory 2>/dev/null || echo "0")
    local cpus=$(minikube config get cpus 2>/dev/null || echo "0")
    
    if [[ "$memory" -lt 6144 ]]; then
        warn "Minikube memory is ${memory}MB (recommended: 6144MB)"
    else
        success "Minikube memory: ${memory}MB"
    fi
    
    if [[ "$cpus" -lt 4 ]]; then
        warn "Minikube CPUs: ${cpus} (recommended: 4)"
    else
        success "Minikube CPUs: ${cpus}"
    fi
}

validate_elasticsearch() {
    step "Validating Elasticsearch"
    
    # Check if any Elasticsearch pods exist
    local pod_count=$(kubectl get pods -n default -l app=elasticsearch-master --no-headers 2>/dev/null | wc -l)
    
    if [[ "$pod_count" -eq 0 ]]; then
        error "Elasticsearch not found (no pods with label app=elasticsearch-master)"
        return 1
    fi
    
    # Check if the first pod is ready
    local es_ready=$(kubectl get pods -n default -l app=elasticsearch-master -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
    
    if [[ "$es_ready" != "True" ]]; then
        error "Elasticsearch pod not ready (status: ${es_ready:-unknown})"
        return 1
    fi
    
    success "Elasticsearch is running and ready"
}

validate_secrets() {
    step "Validating Kubernetes secrets"
    
    if ! kubectl get secret bulletfarm-secrets &> /dev/null; then
        error "bulletfarm-secrets not found"
        return 1
    fi
    
    success "bulletfarm-secrets exists"
}

validate_operator() {
    step "Validating BulletFarm operator"
    
    if ! helm list | grep -q bulletfarm; then
        error "BulletFarm operator not installed"
        return 1
    fi
    
    success "BulletFarm operator installed"
    
    # Check if any operator pods exist
    local pod_count=$(kubectl get pods -l app.kubernetes.io/name=bulletfarm-operator --no-headers 2>/dev/null | wc -l)
    
    if [[ "$pod_count" -eq 0 ]]; then
        error "Operator pod not found"
        return 1
    fi
    
    # Check operator pod readiness
    local operator_ready=$(kubectl get pods -l app.kubernetes.io/name=bulletfarm-operator -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "False")
    
    if [[ "$operator_ready" != "True" ]]; then
        error "Operator pod not ready (status: ${operator_ready})"
        return 1
    fi
    
    success "Operator pod is ready"
}

# --- Setup Functions ---

setup_minikube() {
    step "Starting Minikube"
    
    if minikube status &> /dev/null; then
        warn "Minikube already running"
        return 0
    fi
    
    log "Starting Minikube with ${MINIKUBE_MEMORY}MB memory and ${MINIKUBE_CPUS} CPUs..."
    
    minikube start \
        --memory="${MINIKUBE_MEMORY}" \
        --cpus="${MINIKUBE_CPUS}" \
        --driver=docker
    
    export DOCKER_API_VERSION=1.44
    
    success "Minikube started"
}

setup_elasticsearch() {
    step "Installing Elasticsearch"
    
    if kubectl get pods -n default -l app=elasticsearch-master &> /dev/null; then
        warn "Elasticsearch already installed"
        return 0
    fi
    
    log "Installing Elasticsearch via Helm..."
    
    if [[ ! -f "${PROJECT_ROOT}/deploy/elasticsearch/install-elasticsearch.sh" ]]; then
        error "Elasticsearch install script not found"
        exit 1
    fi
    
    bash "${PROJECT_ROOT}/deploy/elasticsearch/install-elasticsearch.sh"
    
    log "Waiting for Elasticsearch to be ready..."
    kubectl wait --for=condition=ready pod -l app=elasticsearch-master --timeout=300s
    
    success "Elasticsearch installed and ready"
}

build_images() {
    step "Building Docker images"
    
    log "Configuring Docker to use Minikube's Docker daemon..."
    eval $(minikube docker-env)
    export DOCKER_API_VERSION=1.44
    
    # Build operator image
    log "Building operator image..."
    docker build -t bulletfarm/operator:latest "${PROJECT_ROOT}/operator/"
    success "Operator image built"
    
    # Build worker image
    log "Building worker image..."
    docker build -t bulletfarm/worker:latest "${PROJECT_ROOT}/worker/"
    success "Worker image built"
}

create_secrets() {
    step "Creating Kubernetes secrets"
    
    if kubectl get secret bulletfarm-secrets &> /dev/null; then
        warn "bulletfarm-secrets already exists, deleting..."
        kubectl delete secret bulletfarm-secrets
    fi
    
    log "Creating bulletfarm-secrets..."
    
    kubectl create secret generic bulletfarm-secrets \
        --from-literal=github-token="${GITHUB_TOKEN}" \
        --from-literal=openai-api-key="${OPENAI_API_KEY}"
    
    success "Secrets created"
}

install_operator() {
    step "Installing BulletFarm operator"
    
    if helm list | grep -q bulletfarm; then
        warn "BulletFarm operator already installed, upgrading..."
        helm upgrade bulletfarm "${PROJECT_ROOT}/charts/bulletfarm-operator/" \
            --set image.pullPolicy=Never \
            --set image.tag=latest \
            --set workerImage.pullPolicy=Never \
            --set workerImage.tag=latest
    else
        log "Installing BulletFarm operator via Helm..."
        helm install bulletfarm "${PROJECT_ROOT}/charts/bulletfarm-operator/" \
            --set image.pullPolicy=Never \
            --set image.tag=latest \
            --set workerImage.pullPolicy=Never \
            --set workerImage.tag=latest
    fi
    
    log "Waiting for operator to be ready..."
    kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=bulletfarm-operator --timeout=120s
    
    success "Operator installed and ready"
}

# --- Main Execution ---

main() {
    echo ""
    echo "=========================================="
    echo "  BulletFarm E2E Test Setup"
    echo "=========================================="
    echo ""
    
    # Always validate prerequisites
    validate_prerequisites
    
    # If validate-only mode, just run validations
    if [[ "$VALIDATE_ONLY" == "true" ]]; then
        validate_minikube || true
        validate_elasticsearch || true
        validate_secrets || true
        validate_operator || true
        echo ""
        echo "Validation complete. Check results above."
        exit 0
    fi
    
    # Setup steps
    if [[ "$SKIP_MINIKUBE" == "false" ]]; then
        setup_minikube
    else
        log "Skipping Minikube setup (--skip-minikube)"
        validate_minikube
    fi
    
    setup_elasticsearch
    
    if [[ "$SKIP_IMAGES" == "false" ]]; then
        build_images
    else
        log "Skipping image builds (--skip-images)"
    fi
    
    create_secrets
    install_operator
    
    # Final validation
    echo ""
    step "Final validation"
    validate_minikube
    validate_elasticsearch
    validate_secrets
    validate_operator
    
    echo ""
    echo "=========================================="
    success "E2E test environment ready!"
    echo "=========================================="
    echo ""
    echo "Next steps:"
    echo "  1. Run e2e tests: ./tests/e2e/run.sh"
    echo "  2. Check status: ./tests/e2e/run.sh --status"
    echo "  3. Clean up: ./tests/e2e/run.sh --cleanup"
    echo ""
}

# Run main function
main "$@"
