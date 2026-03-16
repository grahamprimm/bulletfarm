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

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	agentsv1alpha1 "github.com/grahamprimm/bulletfarm/operator/api/v1alpha1"
)

// AgentReconciler reconciles Agent objects.
// It maintains the Agent's status.ready, status.activeTasks, and status.runningTasks
// fields by scanning AgentTasks that reference this agent.
type AgentReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=agents.bulletfarm.io,resources=agents,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=agents.bulletfarm.io,resources=agents/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=agents.bulletfarm.io,resources=agents/finalizers,verbs=update
// +kubebuilder:rbac:groups=agents.bulletfarm.io,resources=agenttasks,verbs=get;list;watch

// Reconcile ensures the Agent status reflects the current state of its tasks.
func (r *AgentReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	agent := &agentsv1alpha1.Agent{}
	if err := r.Get(ctx, req.NamespacedName, agent); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	// List all AgentTasks in the same namespace
	taskList := &agentsv1alpha1.AgentTaskList{}
	if err := r.List(ctx, taskList, client.InNamespace(req.Namespace)); err != nil {
		logger.Error(err, "Failed to list AgentTasks")
		return ctrl.Result{}, err
	}

	// Compute running tasks for this agent
	var runningTasks []string
	for i := range taskList.Items {
		t := &taskList.Items[i]
		if t.Spec.AgentRef == agent.Name && (t.Status.Phase == "Running" || t.Status.Phase == "Pending") {
			runningTasks = append(runningTasks, t.Name)
		}
	}

	// Update status
	now := metav1.Now()
	agent.Status.Ready = true
	agent.Status.ActiveTasks = len(runningTasks)
	agent.Status.RunningTasks = runningTasks
	agent.Status.LastReconciled = &now

	if err := r.Status().Update(ctx, agent); err != nil {
		logger.Error(err, "Failed to update Agent status")
		return ctrl.Result{}, err
	}

	return ctrl.Result{RequeueAfter: requeueAfter}, nil
}

// SetupWithManager sets up the controller with the Manager.
func (r *AgentReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentsv1alpha1.Agent{}).
		Named("agent").
		Complete(r)
}
