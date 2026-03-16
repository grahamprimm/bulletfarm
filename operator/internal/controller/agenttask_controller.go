/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	agentsv1alpha1 "github.com/grahamprimm/bulletfarm/operator/api/v1alpha1"
)

const (
	requeueAfter      = 15 * time.Second
	prPollInterval    = 30 * time.Second
	retryBackoff      = 30 * time.Second
	workerPort        = 8000
	defaultMaxRetries = 2
	labelTaskName     = "agents.bulletfarm.io/task-name"
	labelAgentName    = "agents.bulletfarm.io/agent-name"
)

// AgentTaskReconciler reconciles a AgentTask object.
type AgentTaskReconciler struct {
	client.Client
	Scheme     *runtime.Scheme
	HTTPClient *http.Client
}

// WorkerStatus is the response from the worker GET /status endpoint.
type WorkerStatus struct {
	TaskID           string `json:"task_id"`
	Phase            string `json:"phase"`
	Progress         int    `json:"progress"`
	Message          string `json:"message"`
	PullRequestURL   string `json:"pull_request_url"`
	RateLimited      bool   `json:"rate_limited"`
	IncompleteReason string `json:"incomplete_reason"`
}

// TaskPayload is the JSON payload passed to the worker pod as TASK_PAYLOAD env var.
type TaskPayload struct {
	TaskID       string   `json:"task_id"`
	AgentRef     string   `json:"agent_ref"`
	Repository   string   `json:"repository"`
	Prompt       string   `json:"prompt"`
	Description  string   `json:"description"`
	TargetBranch string   `json:"target_branch"`
	Skills       []string `json:"skills"`
	IsRetry      bool     `json:"is_retry"`
	PRURL        string   `json:"pr_url"`
}

// +kubebuilder:rbac:groups=agents.bulletfarm.io,resources=agenttasks,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=agents.bulletfarm.io,resources=agenttasks/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=agents.bulletfarm.io,resources=agenttasks/finalizers,verbs=update
// +kubebuilder:rbac:groups=agents.bulletfarm.io,resources=agents,verbs=get;list;watch;update;patch
// +kubebuilder:rbac:groups=agents.bulletfarm.io,resources=agents/status,verbs=get;update;patch
// +kubebuilder:rbac:groups="",resources=pods,verbs=get;list;watch;create;delete
// +kubebuilder:rbac:groups="",resources=secrets,verbs=get;list;watch

// Reconcile manages the lifecycle of AgentTask resources.
//
// Lifecycle:
//
//	Pending → spawn pod → Running → poll status → WaitingForPR → poll PR → Merged/Closed
//	                                     ↓ (failure)
//	                                   Failed → retry (same branch/PR) → Pending
//
// Rules:
//   - Agent marks PR ready for review on success
//   - Agent NEVER merges or closes PRs — only humans do that
//   - Retries push new commits to the SAME branch and PR
func (r *AgentTaskReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	task := &agentsv1alpha1.AgentTask{}
	if err := r.Get(ctx, req.NamespacedName, task); err != nil {
		if errors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	// Initialize phase if empty
	if task.Status.Phase == "" {
		task.Status.Phase = "Pending"
		task.Status.Progress = 0
		if err := r.Status().Update(ctx, task); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, nil
	}

	switch task.Status.Phase {
	case "Pending":
		return r.handlePending(ctx, task)
	case "Running":
		return r.handleRunning(ctx, task)
	case "Failed":
		return r.handleFailed(ctx, task)
	case "Completed", "WaitingForPR":
		return r.handleWaitingForPR(ctx, task)
	case "Merged", "Closed":
		logger.Info("Task in terminal state", "phase", task.Status.Phase, "task", task.Name)
		return ctrl.Result{}, nil
	default:
		logger.Info("Unknown phase, resetting to Pending", "phase", task.Status.Phase)
		task.Status.Phase = "Pending"
		_ = r.Status().Update(ctx, task)
		return ctrl.Result{Requeue: true}, nil
	}
}

// handlePending fetches the Agent, builds a worker pod, and transitions to Running.
// On retries, the same branch name is reused so new commits go to the existing PR.
func (r *AgentTaskReconciler) handlePending(ctx context.Context, task *agentsv1alpha1.AgentTask) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	agent := &agentsv1alpha1.Agent{}
	agentKey := types.NamespacedName{Name: task.Spec.AgentRef, Namespace: task.Namespace}
	if err := r.Get(ctx, agentKey, agent); err != nil {
		logger.Error(err, "Failed to fetch Agent", "agent", task.Spec.AgentRef)
		task.Status.Message = fmt.Sprintf("Agent %q not found", task.Spec.AgentRef)
		_ = r.Status().Update(ctx, task)
		return ctrl.Result{RequeueAfter: requeueAfter}, nil
	}

	skills := mergeSkills(agent.Spec.DefaultSkills, task.Spec.Skills)

	// Branch name is ALWAYS the same — retries push new commits to the same branch
	targetBranch := task.Spec.TargetBranch
	if targetBranch == "" {
		targetBranch = fmt.Sprintf("task-%s", task.Name)
	}

	isRetry := task.Status.RetryCount > 0

	payload := TaskPayload{
		TaskID:       task.Name,
		AgentRef:     task.Spec.AgentRef,
		Repository:   agent.Spec.Repository,
		Prompt:       task.Spec.Prompt,
		Description:  task.Spec.Description,
		TargetBranch: targetBranch,
		Skills:       skills,
		IsRetry:      isRetry,
		PRURL:        task.Status.PullRequestURL, // pass existing PR URL for retries
	}
	payloadJSON, _ := json.Marshal(payload)

	pod := r.buildWorkerPod(task, agent, string(payloadJSON))

	if err := ctrl.SetControllerReference(task, pod, r.Scheme); err != nil {
		return ctrl.Result{}, err
	}

	if err := r.Create(ctx, pod); err != nil {
		if errors.IsAlreadyExists(err) {
			logger.Info("Worker pod already exists, transitioning to Running")
		} else {
			logger.Error(err, "Failed to create worker pod")
			task.Status.Message = fmt.Sprintf("Failed to create pod: %v", err)
			_ = r.Status().Update(ctx, task)
			return ctrl.Result{RequeueAfter: requeueAfter}, nil
		}
	}

	now := metav1.Now()
	task.Status.Phase = "Running"
	task.Status.Progress = 5
	task.Status.WorkerPod = pod.Name
	task.Status.StartedAt = &now
	if isRetry {
		task.Status.Message = fmt.Sprintf("Retry %d started, pushing to existing branch %s", task.Status.RetryCount, targetBranch)
	} else {
		task.Status.Message = "Worker pod created, starting task"
	}
	if err := r.Status().Update(ctx, task); err != nil {
		return ctrl.Result{}, err
	}

	r.updateAgentStatus(ctx, task.Spec.AgentRef, task.Namespace)

	logger.Info("Created worker pod", "pod", pod.Name, "task", task.Name, "retry", task.Status.RetryCount, "branch", targetBranch)
	return ctrl.Result{RequeueAfter: requeueAfter}, nil
}

// handleRunning polls the worker pod for status updates.
func (r *AgentTaskReconciler) handleRunning(ctx context.Context, task *agentsv1alpha1.AgentTask) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	pod := &corev1.Pod{}
	podKey := types.NamespacedName{Name: task.Status.WorkerPod, Namespace: task.Namespace}
	if err := r.Get(ctx, podKey, pod); err != nil {
		if errors.IsNotFound(err) {
			logger.Info("Worker pod disappeared, marking task Failed")
			task.Status.Phase = "Failed"
			task.Status.Message = "Worker pod not found"
			task.Status.LastFailureReason = "pod_disappeared"
			_ = r.Status().Update(ctx, task)
			r.updateAgentStatus(ctx, task.Spec.AgentRef, task.Namespace)
			return ctrl.Result{Requeue: true}, nil
		}
		return ctrl.Result{RequeueAfter: requeueAfter}, nil
	}

	switch pod.Status.Phase {
	case corev1.PodFailed:
		task.Status.Phase = "Failed"
		task.Status.Message = "Worker pod failed"
		task.Status.LastFailureReason = "pod_failed"
		_ = r.Status().Update(ctx, task)
		r.updateAgentStatus(ctx, task.Spec.AgentRef, task.Namespace)
		return ctrl.Result{Requeue: true}, nil
	case corev1.PodSucceeded:
		if task.Status.Phase != "Completed" {
			now := metav1.Now()
			task.Status.Phase = "Completed"
			task.Status.Progress = 100
			task.Status.CompletedAt = &now
			task.Status.Message = "Task completed"
			_ = r.Status().Update(ctx, task)
			r.updateAgentStatus(ctx, task.Spec.AgentRef, task.Namespace)
		}
		return ctrl.Result{}, nil
	case corev1.PodRunning:
		// Continue to poll
	default:
		logger.Info("Pod not yet running", "podPhase", pod.Status.Phase)
		return ctrl.Result{RequeueAfter: requeueAfter}, nil
	}

	podIP := pod.Status.PodIP
	if podIP == "" {
		return ctrl.Result{RequeueAfter: requeueAfter}, nil
	}

	status, err := r.queryWorkerStatus(podIP, task.Name)
	if err != nil {
		logger.Info("Failed to query worker status, will retry", "error", err)
		return ctrl.Result{RequeueAfter: requeueAfter}, nil
	}

	task.Status.Progress = status.Progress
	task.Status.Message = status.Message
	if status.PullRequestURL != "" {
		task.Status.PullRequestURL = status.PullRequestURL
	}

	switch status.Phase {
	case "Succeeded", "Completed":
		now := metav1.Now()
		task.Status.Phase = "WaitingForPR"
		task.Status.Progress = 100
		task.Status.CompletedAt = &now
		task.Status.Message = "Task completed — PR marked ready for review, awaiting human action"
		task.Status.PRState = "open"
		logger.Info("Task completed, marking PR ready and transitioning to WaitingForPR",
			"task", task.Name, "pr_url", task.Status.PullRequestURL)
		// Mark the PR ready for review — the agent's job is done
		r.finalizeWorker(podIP, task.Name)
		r.updateAgentStatus(ctx, task.Spec.AgentRef, task.Namespace)

	case "Incomplete":
		// Agent ran but couldn't make meaningful changes.
		// Treat as failure so retry logic kicks in.
		// The draft PR stays open with an explanatory comment.
		task.Status.Phase = "Failed"
		task.Status.LastFailureReason = "incomplete"
		if status.IncompleteReason != "" {
			task.Status.Message = fmt.Sprintf("Task incomplete: %s", status.IncompleteReason)
		} else {
			task.Status.Message = "Task incomplete: agent could not make meaningful changes"
		}
		logger.Info("Task incomplete, treating as failure for retry",
			"task", task.Name,
			"pr_url", task.Status.PullRequestURL,
			"reason", status.IncompleteReason,
		)
		r.updateAgentStatus(ctx, task.Spec.AgentRef, task.Namespace)

	case "Failed":
		reason := "worker_failed"
		if status.RateLimited {
			reason = "rate_limited"
		} else if strings.Contains(status.Message, "rate limit") || strings.Contains(status.Message, "429") {
			reason = "rate_limited"
		}
		task.Status.Phase = "Failed"
		task.Status.Message = status.Message
		task.Status.LastFailureReason = reason
		r.updateAgentStatus(ctx, task.Spec.AgentRef, task.Namespace)
	}

	if err := r.Status().Update(ctx, task); err != nil {
		return ctrl.Result{}, err
	}

	if task.Status.Phase == "Running" {
		return ctrl.Result{RequeueAfter: requeueAfter}, nil
	}

	// If just transitioned to Failed, requeue immediately so handleFailed runs
	if task.Status.Phase == "Failed" {
		return ctrl.Result{Requeue: true}, nil
	}

	return ctrl.Result{}, nil
}

// PRStatusResponse is the response from the worker GET /tasks/{id}/pr-status endpoint.
type PRStatusResponse struct {
	PRURL  string `json:"pr_url"`
	State  string `json:"state"`
	Merged bool   `json:"merged"`
	Draft  bool   `json:"draft"`
}

// GraduateResponse is the response from the worker POST /tasks/{id}/graduate endpoint.
type GraduateResponse struct {
	TaskMemoryDeleted   bool   `json:"task_memory_deleted"`
	SharedMemoryUpdated bool   `json:"shared_memory_updated"`
	PRState             string `json:"pr_state"`
}

// handleWaitingForPR polls the GitHub PR status via the worker pod.
// When a HUMAN merges or closes the PR, it graduates memory and tears down the pod.
// The agent NEVER merges or closes PRs itself.
func (r *AgentTaskReconciler) handleWaitingForPR(ctx context.Context, task *agentsv1alpha1.AgentTask) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	// If no PR URL, skip straight to terminal
	if task.Status.PullRequestURL == "" {
		task.Status.Phase = "Closed"
		task.Status.Message = "No PR was created"
		_ = r.Status().Update(ctx, task)
		r.cleanupWorkerPod(ctx, task.Status.WorkerPod, task.Namespace)
		r.updateAgentStatus(ctx, task.Spec.AgentRef, task.Namespace)
		return ctrl.Result{}, nil
	}

	// Find the worker pod to query PR status
	pod := &corev1.Pod{}
	podKey := types.NamespacedName{Name: task.Status.WorkerPod, Namespace: task.Namespace}
	if err := r.Get(ctx, podKey, pod); err != nil || pod.Status.Phase != corev1.PodRunning || pod.Status.PodIP == "" {
		// Worker pod is gone — can't check PR status, mark as Closed
		logger.Info("Worker pod unavailable during WaitingForPR, marking Closed", "task", task.Name)
		task.Status.Phase = "Closed"
		task.Status.Message = "Worker pod unavailable, cannot track PR"
		_ = r.Status().Update(ctx, task)
		r.updateAgentStatus(ctx, task.Spec.AgentRef, task.Namespace)
		return ctrl.Result{}, nil
	}

	podIP := pod.Status.PodIP

	// Poll PR status via worker (read-only — just checking what the human did)
	prStatus, err := r.queryPRStatus(podIP, task.Name)
	if err != nil {
		logger.Info("Failed to query PR status, will retry", "error", err)
		return ctrl.Result{RequeueAfter: prPollInterval}, nil
	}

	task.Status.PRState = prStatus.State

	switch {
	case prStatus.Merged:
		// Human merged the PR
		logger.Info("PR merged by human, graduating memory and tearing down", "task", task.Name)
		task.Status.Phase = "Merged"
		task.Status.Message = "PR merged by reviewer"

		// Graduate: move task_memory → shared_memory, delete task_memory
		r.graduateWorker(podIP, task.Name)

		// Tear down the worker pod — task is fully complete
		r.cleanupWorkerPod(ctx, task.Status.WorkerPod, task.Namespace)

	case prStatus.State == "closed":
		// Human closed the PR without merging
		logger.Info("PR closed by human without merge, graduating memory and tearing down", "task", task.Name)
		task.Status.Phase = "Closed"
		task.Status.Message = "PR closed by reviewer without merge"

		// Graduate: move task_memory → shared_memory, delete task_memory
		r.graduateWorker(podIP, task.Name)

		// Tear down the worker pod
		r.cleanupWorkerPod(ctx, task.Status.WorkerPod, task.Namespace)

	default:
		// PR still open — keep polling, waiting for human action
		if task.Status.Phase != "WaitingForPR" {
			task.Status.Phase = "WaitingForPR"
		}
		task.Status.Message = fmt.Sprintf("PR open (draft=%v), awaiting human review", prStatus.Draft)
	}

	if err := r.Status().Update(ctx, task); err != nil {
		return ctrl.Result{}, err
	}

	r.updateAgentStatus(ctx, task.Spec.AgentRef, task.Namespace)

	// Keep polling if still waiting for human action
	if task.Status.Phase == "WaitingForPR" {
		return ctrl.Result{RequeueAfter: prPollInterval}, nil
	}

	return ctrl.Result{}, nil
}

// queryPRStatus calls GET /tasks/{task_id}/pr-status on the worker pod.
func (r *AgentTaskReconciler) queryPRStatus(podIP string, taskID string) (*PRStatusResponse, error) {
	url := fmt.Sprintf("http://%s:%d/tasks/%s/pr-status", podIP, workerPort, taskID)

	httpClient := r.HTTPClient
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 10 * time.Second}
	}

	resp, err := httpClient.Get(url)
	if err != nil {
		return nil, fmt.Errorf("GET /pr-status failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("reading response body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("worker returned %d: %s", resp.StatusCode, string(body))
	}

	var prStatus PRStatusResponse
	if err := json.Unmarshal(body, &prStatus); err != nil {
		return nil, fmt.Errorf("unmarshalling PR status: %w", err)
	}

	return &prStatus, nil
}

// graduateWorker calls POST /tasks/{task_id}/graduate on the worker pod.
func (r *AgentTaskReconciler) graduateWorker(podIP string, taskID string) {
	url := fmt.Sprintf("http://%s:%d/tasks/%s/graduate", podIP, workerPort, taskID)

	httpClient := r.HTTPClient
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 30 * time.Second}
	}

	resp, err := httpClient.Post(url, "application/json", nil)
	if err != nil {
		return
	}
	defer resp.Body.Close()
}

// handleFailed decides whether to retry or stay in terminal Failed state.
// On retry: cleans up the old pod, keeps the branch and PR intact, spawns a new pod
// that will push new commits to the SAME branch.
// The agent NEVER closes or deletes PRs/branches.
func (r *AgentTaskReconciler) handleFailed(ctx context.Context, task *agentsv1alpha1.AgentTask) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	maxRetries := task.Spec.MaxRetries
	if maxRetries == 0 {
		maxRetries = defaultMaxRetries
	}

	canRetry := task.Status.RetryCount < maxRetries

	// Clean up the worker pod (but NOT the branch or PR — those persist across retries)
	if task.Status.WorkerPod != "" {
		r.cleanupWorkerPod(ctx, task.Status.WorkerPod, task.Namespace)
	}

	if canRetry {
		task.Status.RetryCount++
		logger.Info("Retrying failed task on same branch/PR",
			"task", task.Name,
			"retry", task.Status.RetryCount,
			"maxRetries", maxRetries,
			"lastFailure", task.Status.LastFailureReason,
			"existingPR", task.Status.PullRequestURL,
		)

		// Reset to Pending for re-execution.
		// KEEP PullRequestURL — the retry will push to the same branch and update the same PR.
		task.Status.Phase = "Pending"
		task.Status.Progress = 0
		task.Status.WorkerPod = ""
		task.Status.StartedAt = nil
		task.Status.CompletedAt = nil
		task.Status.Message = fmt.Sprintf("Retrying (attempt %d/%d) on same branch: %s",
			task.Status.RetryCount, maxRetries, task.Status.LastFailureReason)

		if err := r.Status().Update(ctx, task); err != nil {
			return ctrl.Result{}, err
		}

		// Backoff before retry: 30s * retryCount
		backoff := retryBackoff * time.Duration(task.Status.RetryCount)
		return ctrl.Result{RequeueAfter: backoff}, nil
	}

	// Terminal failure — no more retries.
	// The PR stays open for human review (agent never closes PRs).
	logger.Info("Task permanently failed, no retries remaining. PR left open for human review.",
		"task", task.Name,
		"retryCount", task.Status.RetryCount,
		"lastFailure", task.Status.LastFailureReason,
		"pr_url", task.Status.PullRequestURL,
	)
	task.Status.Message = fmt.Sprintf("Permanently failed after %d attempts: %s. PR left open for human review.",
		task.Status.RetryCount, task.Status.LastFailureReason)
	_ = r.Status().Update(ctx, task)

	return ctrl.Result{}, nil
}

// cleanupWorkerPod deletes the worker pod if it exists.
func (r *AgentTaskReconciler) cleanupWorkerPod(ctx context.Context, podName, namespace string) {
	logger := log.FromContext(ctx)
	pod := &corev1.Pod{}
	podKey := types.NamespacedName{Name: podName, Namespace: namespace}
	if err := r.Get(ctx, podKey, pod); err != nil {
		if !errors.IsNotFound(err) {
			logger.Error(err, "Failed to get pod for cleanup", "pod", podName)
		}
		return
	}
	if err := r.Delete(ctx, pod); err != nil && !errors.IsNotFound(err) {
		logger.Error(err, "Failed to delete worker pod", "pod", podName)
	} else {
		logger.Info("Cleaned up worker pod", "pod", podName)
	}
}

// updateAgentStatus recalculates the Agent's status by listing all AgentTasks that reference it.
func (r *AgentTaskReconciler) updateAgentStatus(ctx context.Context, agentName, namespace string) {
	logger := log.FromContext(ctx)

	agent := &agentsv1alpha1.Agent{}
	agentKey := types.NamespacedName{Name: agentName, Namespace: namespace}
	if err := r.Get(ctx, agentKey, agent); err != nil {
		if !errors.IsNotFound(err) {
			logger.Error(err, "Failed to fetch Agent for status update", "agent", agentName)
		}
		return
	}

	taskList := &agentsv1alpha1.AgentTaskList{}
	if err := r.List(ctx, taskList, client.InNamespace(namespace)); err != nil {
		logger.Error(err, "Failed to list AgentTasks for agent status")
		return
	}

	var runningTasks []string
	for i := range taskList.Items {
		t := &taskList.Items[i]
		if t.Spec.AgentRef == agentName && (t.Status.Phase == "Running" || t.Status.Phase == "Pending") {
			runningTasks = append(runningTasks, t.Name)
		}
	}

	now := metav1.Now()
	agent.Status.Ready = true
	agent.Status.ActiveTasks = len(runningTasks)
	agent.Status.RunningTasks = runningTasks
	agent.Status.LastReconciled = &now

	if err := r.Status().Update(ctx, agent); err != nil {
		logger.Error(err, "Failed to update Agent status", "agent", agentName)
	}
}

// buildWorkerPod constructs a Pod spec for the worker.
func (r *AgentTaskReconciler) buildWorkerPod(
	task *agentsv1alpha1.AgentTask,
	agent *agentsv1alpha1.Agent,
	payloadJSON string,
) *corev1.Pod {
	// Include retry count in pod name to avoid conflicts (pods are ephemeral)
	podName := fmt.Sprintf("worker-%s", task.Name)
	if task.Status.RetryCount > 0 {
		podName = fmt.Sprintf("worker-%s-r%d", task.Name, task.Status.RetryCount)
	}

	esURL := agent.Spec.Config.ElasticsearchURL
	if esURL == "" {
		esURL = "http://elasticsearch-master:9200"
	}

	llmProvider := agent.Spec.Config.LLMProvider
	if llmProvider == "" {
		llmProvider = "openai"
	}
	llmModel := agent.Spec.Config.LLMModel
	if llmModel == "" {
		llmModel = "gpt-4o-mini"
	}

	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      podName,
			Namespace: task.Namespace,
			Labels: map[string]string{
				labelTaskName:  task.Name,
				labelAgentName: task.Spec.AgentRef,
				"app":          "bulletfarm-worker",
			},
		},
		Spec: corev1.PodSpec{
			RestartPolicy: corev1.RestartPolicyNever,
			Containers: []corev1.Container{
				{
					Name:            "worker",
					Image:           agent.Spec.WorkerImage,
					ImagePullPolicy: corev1.PullIfNotPresent,
					Ports: []corev1.ContainerPort{
						{
							Name:          "http",
							ContainerPort: int32(workerPort),
							Protocol:      corev1.ProtocolTCP,
						},
					},
					Env: []corev1.EnvVar{
						{Name: "TASK_PAYLOAD", Value: payloadJSON},
						{Name: "BULLETFARM_ELASTICSEARCH_URL", Value: esURL},
						{Name: "BULLETFARM_LLM_PROVIDER", Value: llmProvider},
						{Name: "BULLETFARM_LLM_MODEL", Value: llmModel},
						{Name: "BULLETFARM_WORKER_PORT", Value: fmt.Sprintf("%d", workerPort)},
						{
							Name: "BULLETFARM_GITHUB_TOKEN",
							ValueFrom: &corev1.EnvVarSource{
								SecretKeyRef: &corev1.SecretKeySelector{
									LocalObjectReference: corev1.LocalObjectReference{Name: "bulletfarm-secrets"},
									Key:                  "github-token",
								},
							},
						},
						{
							Name: "BULLETFARM_OPENAI_API_KEY",
							ValueFrom: &corev1.EnvVarSource{
								SecretKeyRef: &corev1.SecretKeySelector{
									LocalObjectReference: corev1.LocalObjectReference{Name: "bulletfarm-secrets"},
									Key:                  "openai-api-key",
									Optional:             boolPtr(true),
								},
							},
						},
						{
							Name: "BULLETFARM_OLLAMA_BASE_URL",
							ValueFrom: &corev1.EnvVarSource{
								SecretKeyRef: &corev1.SecretKeySelector{
									LocalObjectReference: corev1.LocalObjectReference{Name: "bulletfarm-secrets"},
									Key:                  "ollama-base-url",
									Optional:             boolPtr(true),
								},
							},
						},
					},
					Resources: corev1.ResourceRequirements{
						Requests: corev1.ResourceList{
							corev1.ResourceCPU:    resource.MustParse("250m"),
							corev1.ResourceMemory: resource.MustParse("256Mi"),
						},
						Limits: corev1.ResourceList{
							corev1.ResourceCPU:    resource.MustParse("1"),
							corev1.ResourceMemory: resource.MustParse("512Mi"),
						},
					},
					ReadinessProbe: &corev1.Probe{
						ProbeHandler: corev1.ProbeHandler{
							HTTPGet: &corev1.HTTPGetAction{
								Path: "/health",
								Port: intstr.FromInt(workerPort),
							},
						},
						InitialDelaySeconds: 5,
						PeriodSeconds:       10,
					},
					VolumeMounts: []corev1.VolumeMount{
						{Name: "workspace", MountPath: "/workspace"},
					},
				},
			},
			Volumes: []corev1.Volume{
				{
					Name:         "workspace",
					VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
				},
			},
		},
	}
}

// queryWorkerStatus calls GET /tasks/{task_id}/status on the worker pod.
func (r *AgentTaskReconciler) queryWorkerStatus(podIP string, taskID string) (*WorkerStatus, error) {
	url := fmt.Sprintf("http://%s:%d/tasks/%s/status", podIP, workerPort, taskID)

	httpClient := r.HTTPClient
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 10 * time.Second}
	}

	resp, err := httpClient.Get(url)
	if err != nil {
		return nil, fmt.Errorf("GET /status failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("reading response body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("worker returned %d: %s", resp.StatusCode, string(body))
	}

	var status WorkerStatus
	if err := json.Unmarshal(body, &status); err != nil {
		return nil, fmt.Errorf("unmarshalling status: %w", err)
	}

	return &status, nil
}

// finalizeWorker calls POST /tasks/{task_id}/finalize on the worker pod.
// This marks the draft PR as ready for review — the agent's job is done.
// The agent NEVER merges or closes the PR.
func (r *AgentTaskReconciler) finalizeWorker(podIP string, taskID string) {
	url := fmt.Sprintf("http://%s:%d/tasks/%s/finalize", podIP, workerPort, taskID)

	httpClient := r.HTTPClient
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 30 * time.Second}
	}

	resp, err := httpClient.Post(url, "application/json", nil)
	if err != nil {
		return
	}
	defer resp.Body.Close()
}

// mergeSkills combines agent default skills with task-specific skills, deduplicating.
func mergeSkills(defaults, taskSkills []string) []string {
	seen := make(map[string]bool)
	var merged []string

	for _, s := range defaults {
		if !seen[s] {
			seen[s] = true
			merged = append(merged, s)
		}
	}
	for _, s := range taskSkills {
		if !seen[s] {
			seen[s] = true
			merged = append(merged, s)
		}
	}
	return merged
}

func boolPtr(b bool) *bool {
	return &b
}

// SetupWithManager sets up the controller with the Manager.
func (r *AgentTaskReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentsv1alpha1.AgentTask{}).
		Owns(&corev1.Pod{}).
		Named("agenttask").
		Complete(r)
}
