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

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// AgentTaskSpec defines the desired state of AgentTask.
type AgentTaskSpec struct {
	// AgentRef references the parent Agent.
	AgentRef string `json:"agentRef"`

	// Description is a human-readable description of the task.
	// +optional
	Description string `json:"description,omitempty"`

	// Prompt is the task instruction for the LangChain agent.
	Prompt string `json:"prompt"`

	// TargetBranch is the branch to create for changes.
	// +optional
	TargetBranch string `json:"targetBranch,omitempty"`

	// Skills are the skills required for this task (merged with agent defaults).
	// +optional
	Skills []string `json:"skills,omitempty"`

	// MaxRetries is the maximum number of retry attempts on failure (default 2).
	// +optional
	MaxRetries int `json:"maxRetries,omitempty"`
}

// AgentTaskStatus defines the observed state of AgentTask.
type AgentTaskStatus struct {
	// Phase is the current task phase (Pending, Running, Completed, Failed).
	// +optional
	Phase string `json:"phase,omitempty"`

	// Progress is the task completion percentage (0-100).
	// +optional
	Progress int `json:"progress,omitempty"`

	// WorkerPod is the name of the worker pod.
	// +optional
	WorkerPod string `json:"workerPod,omitempty"`

	// PullRequestURL is the URL of the created draft PR.
	// +optional
	PullRequestURL string `json:"pullRequestURL,omitempty"`

	// PRState tracks the GitHub PR state (open, merged, closed).
	// +optional
	PRState string `json:"prState,omitempty"`

	// StartedAt is when the task started.
	// +optional
	StartedAt *metav1.Time `json:"startedAt,omitempty"`

	// CompletedAt is when the task completed.
	// +optional
	CompletedAt *metav1.Time `json:"completedAt,omitempty"`

	// Message provides human-readable status info.
	// +optional
	Message string `json:"message,omitempty"`

	// RetryCount tracks how many times this task has been retried.
	// +optional
	RetryCount int `json:"retryCount,omitempty"`

	// LastFailureReason records why the last attempt failed.
	// +optional
	LastFailureReason string `json:"lastFailureReason,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Agent",type=string,JSONPath=`.spec.agentRef`
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Progress",type=integer,JSONPath=`.status.progress`
// +kubebuilder:printcolumn:name="PR",type=string,JSONPath=`.status.pullRequestURL`

// AgentTask is the Schema for the agenttasks API.
type AgentTask struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   AgentTaskSpec   `json:"spec,omitempty"`
	Status AgentTaskStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// AgentTaskList contains a list of AgentTask.
type AgentTaskList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []AgentTask `json:"items"`
}

func init() {
	SchemeBuilder.Register(&AgentTask{}, &AgentTaskList{})
}
