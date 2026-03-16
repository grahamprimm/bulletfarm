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

// AgentSpec defines the desired state of Agent.
type AgentSpec struct {
	// Repository is the GitHub repo URL the agent operates on.
	Repository string `json:"repository"`

	// Branch is the target branch for operations.
	// +optional
	Branch string `json:"branch,omitempty"`

	// WorkerImage is the Docker image for the Python worker.
	WorkerImage string `json:"workerImage"`

	// DefaultSkills are skills loaded by default for every task this agent runs.
	// +optional
	DefaultSkills []string `json:"defaultSkills,omitempty"`

	// Config holds agent-specific configuration.
	// +optional
	Config AgentConfig `json:"config,omitempty"`
}

// AgentConfig holds LLM and storage configuration for an Agent.
type AgentConfig struct {
	// LLMProvider is the LLM backend (openai, ollama).
	// +optional
	LLMProvider string `json:"llmProvider,omitempty"`

	// LLMModel is the model name.
	// +optional
	LLMModel string `json:"llmModel,omitempty"`

	// ElasticsearchURL is the ES endpoint for memory.
	// +optional
	ElasticsearchURL string `json:"elasticsearchURL,omitempty"`
}

// AgentStatus defines the observed state of Agent.
type AgentStatus struct {
	// Ready indicates if the agent is operational.
	// +optional
	Ready bool `json:"ready,omitempty"`

	// ActiveTasks is the count of running AgentTasks.
	// +optional
	ActiveTasks int `json:"activeTasks,omitempty"`

	// RunningTasks lists the names of AgentTasks currently running.
	// +optional
	RunningTasks []string `json:"runningTasks,omitempty"`

	// LastReconciled is the timestamp of last reconciliation.
	// +optional
	LastReconciled *metav1.Time `json:"lastReconciled,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Repository",type=string,JSONPath=`.spec.repository`
// +kubebuilder:printcolumn:name="Image",type=string,JSONPath=`.spec.workerImage`
// +kubebuilder:printcolumn:name="Ready",type=boolean,JSONPath=`.status.ready`
// +kubebuilder:printcolumn:name="Active Tasks",type=integer,JSONPath=`.status.activeTasks`

// Agent is the Schema for the agents API.
type Agent struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   AgentSpec   `json:"spec,omitempty"`
	Status AgentStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// AgentList contains a list of Agent.
type AgentList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Agent `json:"items"`
}

func init() {
	SchemeBuilder.Register(&Agent{}, &AgentList{})
}
