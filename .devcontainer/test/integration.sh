#!/bin/bash
# Load framework
source .devcontainer/util/source_framework.sh

printInfoSection "Running integration Tests for $RepositoryName"

# --- Kubernetes Cluster ---
assertRunningPod kube-system coredns

printInfoSection "Running integration Tests for $RepositoryName"

assertRunningPod dynatrace operator

assertRunningPod dynatrace activegate

#assertRunningPod dynatrace oneagent

assertRunningPod todoapp todoapp

# Test app is reachable via nginx ingress + magic DNS (sslip.io)
# Works for both k3d (default) and Kind clusters
assertRunningApp todoapp


printInfoSection "Integration tests completed for $RepositoryName"
