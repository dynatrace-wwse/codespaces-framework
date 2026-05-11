#!/bin/bash
# Load framework
source .devcontainer/util/source_framework.sh

printInfoSection "Running integration Tests for $RepositoryName"

# --- Kubernetes Cluster ---
assertRunningPod kube-system coredns

printInfoSection "Running integration Tests for $RepositoryName"

assertRunningPod dynatrace operator

assertRunningPod dynatrace activegate

# CloudNativeFullStack: assert OneAgent DaemonSet pod is running
assertRunningPod dynatrace oneagent

assertRunningPod todoapp todoapp

# Kind has no bound NodePort for ingress — skip HTTP reachability check
if [[ "${CLUSTER_ENGINE:-k3d}" != "kind" ]]; then
  assertRunningApp todoapp
fi


printInfoSection "Integration tests completed for $RepositoryName"
