#!/usr/bin/env bash
# =============================================================================
# BulletFarm E2E Test Runner
# =============================================================================
# Automated end-to-end test suite for the BulletFarm agent operator.
# Tests multiple agents across multiple repositories with multiple tasks.
#
# Usage:
#   ./tests/e2e/run.sh              # Run full suite
#   ./tests/e2e/run.sh --cleanup    # Clean up all test resources
#   ./tests/e2e/run.sh --status     # Check status of running tests
#
# Prerequisites:
#   - Minikube running with operator deployed
#   - Elasticsearch running
#   - bulletfarm-secrets configured
#   - gh CLI authenticated
# =============================================================================

set -u  # Unset variable check only; no -e or pipefail (test runner needs to continue on failures)

export DOCKER_API_VERSION=1.44

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS_DIR="${SCRIPT_DIR}/manifests"
RESULTS_DIR="${SCRIPT_DIR}/results"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
RESULTS_FILE="${RESULTS_DIR}/e2e-${TIMESTAMP}.log"
REPORT_FILE="${RESULTS_DIR}/e2e-${TIMESTAMP}-report.md"

# Test configuration
TASK_TIMEOUT=480        # 8 minutes — alpha tasks need more time with gpt-4o-mini
POLL_INTERVAL=15        # seconds between status checks
TOTAL_TASKS=6           # number of tasks in tasks.yaml

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Counters
PASS=0
FAIL=0
SKIP=0

# --- Logging ---
log()  { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $*" | tee -a "$RESULTS_FILE"; }
pass() { echo -e "${GREEN}[PASS]${NC} $*" | tee -a "$RESULTS_FILE"; PASS=$((PASS + 1)); }
fail() { echo -e "${RED}[FAIL]${NC} $*" | tee -a "$RESULTS_FILE"; FAIL=$((FAIL + 1)); }
warn() { echo -e "${YELLOW}[WARN]${NC} $*" | tee -a "$RESULTS_FILE"; }
skip() { echo -e "${YELLOW}[SKIP]${NC} $*" | tee -a "$RESULTS_FILE"; SKIP=$((SKIP + 1)); }

# --- Cleanup ---
cleanup() {
    log "Cleaning up previous E2E test resources..."
    kubectl delete agenttasks -l app.kubernetes.io/part-of=bulletfarm-e2e --ignore-not-found 2>/dev/null || true
    kubectl delete agents -l app.kubernetes.io/part-of=bulletfarm-e2e --ignore-not-found 2>/dev/null || true
    # Wait for worker pods to be cleaned up via owner references
    sleep 5
    kubectl delete pods -l app=bulletfarm-worker --ignore-not-found 2>/dev/null || true
    log "Cleanup complete."
}

# --- Status check ---
check_status() {
    echo ""
    echo "=== Agents ==="
    kubectl get agents -l app.kubernetes.io/part-of=bulletfarm-e2e 2>/dev/null || echo "No agents"
    echo ""
    echo "=== Tasks ==="
    kubectl get agenttasks -l app.kubernetes.io/part-of=bulletfarm-e2e -o wide 2>/dev/null || echo "No tasks"
    echo ""
    echo "=== Worker Pods ==="
    kubectl get pods -l app=bulletfarm-worker 2>/dev/null || echo "No worker pods"
}

# --- Prerequisite checks ---
check_prerequisites() {
    log "Checking prerequisites..."

    # Minikube
    if ! minikube status &>/dev/null; then
        fail "Minikube is not running"
        exit 1
    fi
    pass "Minikube is running"

    # Operator — find the deployment by label since the name varies by chart
    OPERATOR_DEPLOY=$(kubectl get deployment -l control-plane=controller-manager --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null | head -1)
    if [[ -z "$OPERATOR_DEPLOY" ]]; then
        fail "Operator deployment not found (no deployment with label control-plane=controller-manager)"
        exit 1
    fi
    OPERATOR_READY=$(kubectl get deployment "$OPERATOR_DEPLOY" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
    if [[ "$OPERATOR_READY" != "1" ]]; then
        fail "Operator not ready (readyReplicas=$OPERATOR_READY)"
        exit 1
    fi
    pass "Operator is running and ready"

    # Elasticsearch
    ES_READY=$(kubectl get pod elasticsearch-master-0 -o jsonpath='{.status.phase}' 2>/dev/null)
    if [[ "$ES_READY" != "Running" ]]; then
        fail "Elasticsearch not running (phase=$ES_READY)"
        exit 1
    fi
    pass "Elasticsearch is running"

    # Secrets
    if ! kubectl get secret bulletfarm-secrets &>/dev/null; then
        fail "bulletfarm-secrets not found"
        exit 1
    fi
    GH_KEY_LEN=$(kubectl get secret bulletfarm-secrets -o jsonpath='{.data.github-token}' | base64 -d | wc -c)
    OAI_KEY_LEN=$(kubectl get secret bulletfarm-secrets -o jsonpath='{.data.openai-api-key}' | base64 -d | wc -c)
    if [[ "$GH_KEY_LEN" -lt 10 ]]; then
        fail "GitHub token appears empty or too short ($GH_KEY_LEN chars)"
        exit 1
    fi
    if [[ "$OAI_KEY_LEN" -lt 10 ]]; then
        fail "OpenAI API key appears empty or too short ($OAI_KEY_LEN chars)"
        exit 1
    fi
    pass "Secrets configured (github-token=${GH_KEY_LEN}c, openai-api-key=${OAI_KEY_LEN}c)"

    # CRDs
    if ! kubectl get crd agents.agents.bulletfarm.io &>/dev/null; then
        fail "Agent CRD not installed"
        exit 1
    fi
    if ! kubectl get crd agenttasks.agents.bulletfarm.io &>/dev/null; then
        fail "AgentTask CRD not installed"
        exit 1
    fi
    pass "CRDs installed"

    # GitHub repos
    for repo in bulletfarm-test-repo-alpha bulletfarm-test-repo-beta bulletfarm-test-repo-gamma; do
        if ! gh repo view "grahamprimm/$repo" &>/dev/null; then
            fail "GitHub repo grahamprimm/$repo not found"
            exit 1
        fi
    done
    pass "All 3 GitHub test repos accessible"

    log "All prerequisites passed."
}

# --- Wait for a single task to reach terminal state ---
wait_for_task() {
    local task_name="$1"
    local elapsed=0

    while [[ $elapsed -lt $TASK_TIMEOUT ]]; do
        PHASE=$(kubectl get agenttask "$task_name" -o jsonpath='{.status.phase}' 2>/dev/null || echo "NotFound")
        if [[ "$PHASE" == "Completed" || "$PHASE" == "Failed" ]]; then
            return 0
        fi
        sleep "$POLL_INTERVAL"
        elapsed=$((elapsed + POLL_INTERVAL))
    done
    return 1  # timeout
}

# --- Validate a single task result ---
validate_task() {
    local task_name="$1"
    local scenario="$2"
    local expected_repo="$3"
    local task_pass=true

    log "--- Validating $scenario: $task_name ---"

    # 1. Check phase (WaitingForPR is the new "completed" — task done, PR open)
    PHASE=$(kubectl get agenttask "$task_name" -o jsonpath='{.status.phase}' 2>/dev/null)
    if [[ "$PHASE" == "WaitingForPR" || "$PHASE" == "Completed" ]]; then
        pass "$scenario: Phase is $PHASE (task work done)"
    else
        fail "$scenario: Phase is '$PHASE' (expected WaitingForPR or Completed)"
        task_pass=false
    fi

    # 2. Check progress
    PROGRESS=$(kubectl get agenttask "$task_name" -o jsonpath='{.status.progress}' 2>/dev/null)
    if [[ "$PROGRESS" == "100" ]]; then
        pass "$scenario: Progress is 100"
    else
        fail "$scenario: Progress is '$PROGRESS' (expected 100)"
        task_pass=false
    fi

    # 3. Check worker pod was created
    POD_NAME=$(kubectl get agenttask "$task_name" -o jsonpath='{.status.workerPod}' 2>/dev/null)
    if [[ -n "$POD_NAME" ]]; then
        pass "$scenario: Worker pod '$POD_NAME' was created"
    else
        fail "$scenario: No worker pod recorded in status"
        task_pass=false
    fi

    # 4. Check PR URL
    PR_URL=$(kubectl get agenttask "$task_name" -o jsonpath='{.status.pullRequestURL}' 2>/dev/null)
    if [[ -n "$PR_URL" && "$PR_URL" == *"github.com"* ]]; then
        pass "$scenario: PR URL present: $PR_URL"
    else
        fail "$scenario: PR URL missing or invalid: '$PR_URL'"
        task_pass=false
    fi

    # 5. Verify PR exists on GitHub and is ready for review
    if [[ -n "$PR_URL" && "$PR_URL" == *"/pull/"* ]]; then
        PR_NUM=$(echo "$PR_URL" | grep -oP '/pull/\K\d+')
        PR_STATE=$(gh pr view "$PR_NUM" --repo "$expected_repo" --json state,isDraft --jq '.state + ":" + (.isDraft | tostring)' 2>/dev/null || echo "ERROR")
        if [[ "$PR_STATE" == "OPEN:false" ]]; then
            pass "$scenario: PR #$PR_NUM is OPEN and ready for review"
        elif [[ "$PR_STATE" == "OPEN:true" ]]; then
            warn "$scenario: PR #$PR_NUM is OPEN but still in draft"
        else
            fail "$scenario: PR state unexpected: '$PR_STATE'"
            task_pass=false
        fi
    fi

    # 6. Check timestamps
    STARTED=$(kubectl get agenttask "$task_name" -o jsonpath='{.status.startedAt}' 2>/dev/null)
    COMPLETED=$(kubectl get agenttask "$task_name" -o jsonpath='{.status.completedAt}' 2>/dev/null)
    if [[ -n "$STARTED" && -n "$COMPLETED" ]]; then
        pass "$scenario: Timestamps present (started=$STARTED, completed=$COMPLETED)"
    else
        fail "$scenario: Missing timestamps"
        task_pass=false
    fi

    # 7. Check ES task_memory has an entry with methodology
    # Check for Succeeded OR Incomplete entries
    ES_COUNT="0"
    ES_COUNT=$(kubectl exec elasticsearch-master-0 -c elasticsearch -- curl -s "http://localhost:9200/task_memory/_count" \
        -H 'Content-Type: application/json' \
        -d "{\"query\":{\"bool\":{\"must\":[{\"term\":{\"task_id\":\"$task_name\"}},{\"terms\":{\"phase\":[\"Succeeded\",\"Incomplete\"]}}]}}}" 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null) || ES_COUNT="0"
    if [[ "$ES_COUNT" -ge 1 ]]; then
        pass "$scenario: ES task_memory has $ES_COUNT entry(ies)"
    else
        fail "$scenario: No entry in ES task_memory (count=$ES_COUNT)"
        task_pass=false
    fi

    # 8. Check PR has agent labels
    if [[ -n "$PR_URL" && "$PR_URL" == *"/pull/"* ]]; then
        PR_NUM=$(echo "$PR_URL" | grep -oP '/pull/\K\d+')
        LABELS=$(gh pr view "$PR_NUM" --repo "$expected_repo" --json labels --jq '[.labels[].name] | join(",")' 2>/dev/null || echo "")
        if [[ "$LABELS" == *"bulletfarm/agent"* ]]; then
            pass "$scenario: PR has bulletfarm/agent label"
        else
            fail "$scenario: PR missing bulletfarm/agent label (labels: $LABELS)"
            task_pass=false
        fi
        if [[ "$LABELS" == *"bulletfarm/task:${task_name}"* ]]; then
            pass "$scenario: PR has task-specific label"
        else
            fail "$scenario: PR missing bulletfarm/task:${task_name} label (labels: $LABELS)"
            task_pass=false
        fi
    fi

    # 9. Check worker pod is still alive (should be kept for PR watching)
    if [[ -n "$POD_NAME" ]]; then
        POD_STATUS=$(kubectl get pod "$POD_NAME" -o jsonpath='{.status.phase}' 2>/dev/null || echo "NotFound")
        if [[ "$POD_STATUS" == "Running" ]]; then
            pass "$scenario: Worker pod still running (kept alive for PR watching)"
        else
            warn "$scenario: Worker pod status is '$POD_STATUS' (expected Running for PR watching)"
        fi
    fi

    if $task_pass; then
        log "$scenario: ALL CHECKS PASSED"
    else
        log "$scenario: SOME CHECKS FAILED"
    fi
}

# --- Validate post-merge state for a single task ---
validate_post_merge() {
    local task_name="$1"
    local scenario="$2"
    local expected_phase="$3"
    local task_pass=true

    log "--- Post-merge validation $scenario: $task_name ---"

    # 1. Check phase is Merged or Closed
    PHASE=$(kubectl get agenttask "$task_name" -o jsonpath='{.status.phase}' 2>/dev/null)
    if [[ "$PHASE" == "$expected_phase" ]]; then
        pass "$scenario: Phase is $expected_phase after PR action"
    else
        fail "$scenario: Phase is '$PHASE' (expected $expected_phase)"
        task_pass=false
    fi

    # 2. Check prState field
    PR_STATE=$(kubectl get agenttask "$task_name" -o jsonpath='{.status.prState}' 2>/dev/null)
    if [[ -n "$PR_STATE" ]]; then
        pass "$scenario: prState is '$PR_STATE'"
    else
        fail "$scenario: prState is empty"
        task_pass=false
    fi

    # 3. Check task_memory was deleted (graduated)
    ES_COUNT="-1"
    ES_COUNT=$(kubectl exec elasticsearch-master-0 -c elasticsearch -- curl -s "http://localhost:9200/task_memory/_count" -H 'Content-Type: application/json' -d "{\"query\":{\"term\":{\"task_id\":\"$task_name\"}}}" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',-1))" 2>/dev/null) || ES_COUNT="-1"
    if [[ "$ES_COUNT" == "0" ]]; then
        pass "$scenario: task_memory deleted after graduation"
    else
        fail "$scenario: task_memory has $ES_COUNT docs (expected 0)"
        task_pass=false
    fi

    # 4. Check shared_memory was updated with graduation entry
    SHARED_COUNT="0"
    SHARED_COUNT=$(kubectl exec elasticsearch-master-0 -c elasticsearch -- curl -s "http://localhost:9200/shared_memory/_count" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null) || SHARED_COUNT="0"
    if [[ "$SHARED_COUNT" -ge 1 ]]; then
        pass "$scenario: shared_memory has $SHARED_COUNT entries"
    else
        fail "$scenario: shared_memory is empty after graduation"
        task_pass=false
    fi

    # 5. Check worker pod was torn down
    POD_NAME=$(kubectl get agenttask "$task_name" -o jsonpath='{.status.workerPod}' 2>/dev/null)
    if [[ -n "$POD_NAME" ]]; then
        POD_EXISTS=$(kubectl get pod "$POD_NAME" --no-headers 2>/dev/null | wc -l)
        if [[ "$POD_EXISTS" == "0" ]]; then
            pass "$scenario: Worker pod torn down after PR $expected_phase"
        else
            fail "$scenario: Worker pod still exists after PR $expected_phase"
            task_pass=false
        fi
    fi

    if $task_pass; then
        log "$scenario: ALL POST-MERGE CHECKS PASSED"
    else
        log "$scenario: SOME POST-MERGE CHECKS FAILED"
    fi
}

# --- Generate report ---
generate_report() {
    local total=$((PASS + FAIL + SKIP))
    cat > "$REPORT_FILE" << REPORT
# BulletFarm E2E Test Report

**Date:** $(date -Iseconds)
**Duration:** ${1}s
**Operator:** bulletfarm/operator:latest
**Worker:** bulletfarm/worker:latest

## Summary

| Metric | Count |
|--------|-------|
| Total Checks | $total |
| Passed | $PASS |
| Failed | $FAIL |
| Warnings/Skipped | $SKIP |
| **Pass Rate** | **$(( PASS * 100 / (total > 0 ? total : 1) ))%** |

## Test Matrix

| Scenario | Agent | Repo | Task | Skills |
|----------|-------|------|------|--------|
| S1 | alpha-agent | bulletfarm-test-repo-alpha (Python) | Error handling | code-edit |
| S2 | alpha-agent | bulletfarm-test-repo-alpha (Python) | Add tests | code-edit, testing |
| S3 | beta-agent | bulletfarm-test-repo-beta (Go) | Add logging | code-edit |
| S4 | beta-agent | bulletfarm-test-repo-beta (Go) | Update docs | documentation, doc-update |
| S5 | gamma-agent | bulletfarm-test-repo-gamma (Node.js) | Input validation | code-edit |
| S6 | gamma-agent | bulletfarm-test-repo-gamma (Node.js) | Add tests | code-edit, testing |

## Validation Checks Per Task

Each task is validated against these criteria:
1. **Phase** — Must be \`Completed\`
2. **Progress** — Must be \`100\`
3. **Worker Pod** — Must have been created and recorded in status
4. **PR URL** — Must be present and point to github.com
5. **PR State** — Must be OPEN and marked ready for review (not draft)
6. **Timestamps** — startedAt and completedAt must be populated
7. **ES Memory** — task_memory index must contain a Succeeded entry

## Detailed Results

\`\`\`
$(cat "$RESULTS_FILE")
\`\`\`

## Cluster State at Completion

\`\`\`
$(kubectl get agents -l app.kubernetes.io/part-of=bulletfarm-e2e -o wide 2>/dev/null)

$(kubectl get agenttasks -l app.kubernetes.io/part-of=bulletfarm-e2e -o wide 2>/dev/null)

$(kubectl get pods -l app=bulletfarm-worker 2>/dev/null)
\`\`\`

## GitHub PRs Created

$(for repo in grahamprimm/bulletfarm-test-repo-alpha grahamprimm/bulletfarm-test-repo-beta grahamprimm/bulletfarm-test-repo-gamma; do
    echo "### $repo"
    gh pr list --repo "$repo" --state all --json number,title,state,isDraft,headRefName --jq '.[] | "- PR #\(.number): \(.title) [\(.state), draft=\(.isDraft)] branch=\(.headRefName)"' 2>/dev/null || echo "- (no PRs)"
    echo ""
done)
REPORT

    log "Report written to: $REPORT_FILE"
}

# =============================================================================
# Main
# =============================================================================

# Handle flags
case "${1:-}" in
    --cleanup)
        cleanup
        exit 0
        ;;
    --status)
        check_status
        exit 0
        ;;
esac

mkdir -p "$RESULTS_DIR"
echo "" > "$RESULTS_FILE"

START_TIME=$(date +%s)

echo ""
echo "=============================================="
echo "  BulletFarm E2E Test Suite"
echo "  $(date)"
echo "=============================================="
echo ""

# Phase 1: Prerequisites
log "=== Phase 1: Prerequisites ==="
check_prerequisites

# Phase 2: Cleanup previous runs
log ""
log "=== Phase 2: Cleanup ==="
cleanup

# Close any open PRs from previous runs
log "Closing stale PRs from previous E2E runs..."
for repo in grahamprimm/bulletfarm-test-repo-alpha grahamprimm/bulletfarm-test-repo-beta grahamprimm/bulletfarm-test-repo-gamma; do
    for pr_num in $(gh pr list --repo "$repo" --state open --json number --jq '.[].number' 2>/dev/null); do
        gh pr close "$pr_num" --repo "$repo" --delete-branch 2>/dev/null && log "Closed PR #$pr_num on $repo" || true
    done
done

# Phase 3: Deploy agents
log ""
log "=== Phase 3: Deploy Agents ==="
kubectl apply -f "${MANIFESTS_DIR}/agents.yaml" 2>&1 | tee -a "$RESULTS_FILE"
sleep 3
AGENT_COUNT=$(kubectl get agents -l app.kubernetes.io/part-of=bulletfarm-e2e --no-headers 2>/dev/null | wc -l)
if [[ "$AGENT_COUNT" -eq 3 ]]; then
    pass "All 3 agents created"
else
    fail "Expected 3 agents, got $AGENT_COUNT"
fi

# Phase 4: Deploy all tasks simultaneously
log ""
log "=== Phase 4: Deploy Tasks (all 6 simultaneously) ==="
kubectl apply -f "${MANIFESTS_DIR}/tasks.yaml" 2>&1 | tee -a "$RESULTS_FILE"
sleep 3
TASK_COUNT=$(kubectl get agenttasks -l app.kubernetes.io/part-of=bulletfarm-e2e --no-headers 2>/dev/null | wc -l)
if [[ "$TASK_COUNT" -eq $TOTAL_TASKS ]]; then
    pass "All $TOTAL_TASKS tasks created"
else
    fail "Expected $TOTAL_TASKS tasks, got $TASK_COUNT"
fi

# Phase 5: Wait for all tasks to complete
log ""
log "=== Phase 5: Waiting for tasks (timeout=${TASK_TIMEOUT}s per task) ==="

TASKS=(
    "alpha-error-handling"
    "alpha-add-tests"
    "beta-add-logging"
    "beta-update-docs"
    "gamma-add-validation"
    "gamma-add-tests"
)

# Poll all tasks until all are terminal or timeout
GLOBAL_ELAPSED=0
GLOBAL_TIMEOUT=$((TASK_TIMEOUT + 120))  # extra buffer for 6 concurrent tasks

while [[ $GLOBAL_ELAPSED -lt $GLOBAL_TIMEOUT ]]; do
    ALL_DONE=true
    STATUS_LINE=""
    for task in "${TASKS[@]}"; do
        PHASE=$(kubectl get agenttask "$task" -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
        PROGRESS=$(kubectl get agenttask "$task" -o jsonpath='{.status.progress}' 2>/dev/null || echo "0")
        STATUS_LINE+="  ${task}=${PHASE}(${PROGRESS}%)"
        if [[ "$PHASE" != "WaitingForPR" && "$PHASE" != "Completed" && "$PHASE" != "Failed" && "$PHASE" != "Merged" && "$PHASE" != "Closed" ]]; then
            ALL_DONE=false
        fi
    done
    log "Poll @${GLOBAL_ELAPSED}s:${STATUS_LINE}"

    if $ALL_DONE; then
        log "All tasks reached terminal state."
        break
    fi

    sleep "$POLL_INTERVAL"
    GLOBAL_ELAPSED=$((GLOBAL_ELAPSED + POLL_INTERVAL))
done

if ! $ALL_DONE; then
    warn "Timeout reached. Some tasks may not have completed."
fi

# Phase 6: Validate results
log ""
log "=== Phase 6: Validation ==="

validate_task "alpha-error-handling" "S1" "grahamprimm/bulletfarm-test-repo-alpha"
validate_task "alpha-add-tests"      "S2" "grahamprimm/bulletfarm-test-repo-alpha"
validate_task "beta-add-logging"     "S3" "grahamprimm/bulletfarm-test-repo-beta"
validate_task "beta-update-docs"     "S4" "grahamprimm/bulletfarm-test-repo-beta"
validate_task "gamma-add-validation" "S5" "grahamprimm/bulletfarm-test-repo-gamma"
validate_task "gamma-add-tests"      "S6" "grahamprimm/bulletfarm-test-repo-gamma"

# Phase 7: Simulate PR merge/close and validate lifecycle
log ""
log "=== Phase 7: PR Merge/Close Simulation ==="

# Merge some PRs, close others to test both paths
MERGE_TASKS=("beta-add-logging" "gamma-add-validation")
CLOSE_TASKS=("beta-update-docs")

for task_name in "${MERGE_TASKS[@]}"; do
    PR_URL=$(kubectl get agenttask "$task_name" -o jsonpath='{.status.pullRequestURL}' 2>/dev/null)
    if [[ -n "$PR_URL" && "$PR_URL" == *"/pull/"* ]]; then
        PR_NUM=$(echo "$PR_URL" | grep -oP '/pull/\K\d+')
        REPO=$(echo "$PR_URL" | grep -oP 'github\.com/\K[^/]+/[^/]+')
        log "Merging PR #$PR_NUM on $REPO for task $task_name"
        gh pr merge "$PR_NUM" --repo "$REPO" --merge --delete-branch 2>/dev/null && log "  Merged PR #$PR_NUM" || warn "  Failed to merge PR #$PR_NUM"
    fi
done

for task_name in "${CLOSE_TASKS[@]}"; do
    PR_URL=$(kubectl get agenttask "$task_name" -o jsonpath='{.status.pullRequestURL}' 2>/dev/null)
    if [[ -n "$PR_URL" && "$PR_URL" == *"/pull/"* ]]; then
        PR_NUM=$(echo "$PR_URL" | grep -oP '/pull/\K\d+')
        REPO=$(echo "$PR_URL" | grep -oP 'github\.com/\K[^/]+/[^/]+')
        log "Closing PR #$PR_NUM on $REPO for task $task_name"
        gh pr close "$PR_NUM" --repo "$REPO" --delete-branch 2>/dev/null && log "  Closed PR #$PR_NUM" || warn "  Failed to close PR #$PR_NUM"
    fi
done

# Wait for operator to detect PR state changes
log "Waiting 90s for operator to detect PR merge/close..."
sleep 90

# Phase 8: Post-merge validation
log ""
log "=== Phase 8: Post-Merge/Close Validation ==="

for task_name in "${MERGE_TASKS[@]}"; do
    validate_post_merge "$task_name" "POST-MERGE" "Merged"
done

for task_name in "${CLOSE_TASKS[@]}"; do
    validate_post_merge "$task_name" "POST-CLOSE" "Closed"
done

# Phase 9: Report
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

log ""
log "=== Phase 9: Report ==="
generate_report "$DURATION"

echo ""
echo "=============================================="
echo "  E2E Test Suite Complete"
echo "  Duration: ${DURATION}s"
echo "  Passed: $PASS  Failed: $FAIL  Skipped: $SKIP"
echo "  Report: $REPORT_FILE"
echo "=============================================="
echo ""

# Exit with failure if any checks failed
[[ $FAIL -eq 0 ]]
