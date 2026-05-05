# R&D: Docker-in-Docker — Kind vs K3s

## Objective

Test whether we can run the full enablement stack (Kubernetes cluster + apps + Dynatrace) inside a single Docker container **without sharing the host's Docker socket**. This would allow fully containerized training environments — portable, schedulable, and infrastructure-independent.

## Results Summary

| Approach | Docker-in-Docker (no socket) | Direct on host (socket sharing) |
|---|---|---|
| **Kind** | FAILS — cgroup v2 delegation issue | WORKS (current approach) |
| **K3s (nested Docker)** | FAILS — same cgroup issue | WORKS |
| **K3s (direct binary, no Docker)** | FAILS — cgroup issue | WORKS |
| **K3s (rancher/k3s image on host)** | N/A | WORKS perfectly |

### Root Cause

All failures share the same root cause: **cgroup v2 controller delegation**. When running inside a container, the host's cgroup hierarchy only delegates `cpuset`, `cpu`, `pids` to the container's cgroup namespace. The `memory` and `io` controllers are NOT delegated. Both Kind (systemd) and K3s (kubelet) require the `memory` controller to create pod cgroups.

Error messages:
- Kind: `Failed to create /init.scope control group: Structure needs cleaning`
- K3s: `cannot enter cgroupv2 "/sys/fs/cgroup/kubepods" with domain controllers -- it is in an invalid state`

### What Would Fix It

1. **Sysbox runtime** — purpose-built for nested containers, handles cgroup delegation
2. **Host-level systemd configuration** — `Delegate=yes` on the container's cgroup slice
3. **cgroup v1 (legacy)** — works but deprecated on modern kernels
4. **Dedicated VM** — not a container, full cgroup access (what Codespaces/Gitpod do)

---

## Kind vs K3s Comparison

Both were tested running directly on the host Docker daemon (the working approach):

| Metric | Kind (v1.30.0) | K3s (v1.34.1) | Winner |
|---|---|---|---|
| **Image size** | 974 MB | 243 MB | K3s (4x smaller) |
| **Startup time** | ~60 seconds | ~20 seconds | K3s (3x faster) |
| **Memory usage** (idle) | 535 MB | 494 MB | K3s (slightly less) |
| **CPU usage** (idle) | 15% | 7% | K3s (half) |
| **System pods** | 9 | 3 | K3s (minimal) |
| **Process count** | 267 PIDs | 159 PIDs | K3s (40% fewer) |
| **Kubernetes API** | Full (etcd-backed) | Full (SQLite-backed) | Tie |
| **Container runtime** | containerd | containerd (built-in) | Tie |
| **Ingress nginx** | Works | Works | Tie |
| **Helm** | Requires install in framework | Built-in (HelmChart CRD) | K3s |
| **Multi-arch** | Manual config | Native | K3s |

### K3s Advantages

1. **Lightweight**: 4x smaller image, 3x faster startup, half the CPU
2. **Single binary**: No Docker dependency — K3s IS the cluster (API server + kubelet + containerd in one process)
3. **Built-in HelmChart CRD**: Deploy Helm charts declaratively without installing Helm CLI
4. **Built-in LoadBalancer** (ServiceLB/Klipper): No need for MetalLB in single-node setups
5. **SQLite instead of etcd**: Lower memory, simpler, sufficient for training
6. **Auto-TLS**: API server certificates managed automatically
7. **Simpler networking**: Built-in Flannel CNI, no kindnet

### Kind Advantages

1. **Full K8s parity**: Uses exact upstream Kubernetes binaries (same as production clusters)
2. **Teaching value**: Students learn "real" Kubernetes with etcd, kube-proxy, scheduler as separate components
3. **Multi-node simulation**: Can spin up worker nodes as separate containers
4. **Community adoption**: More widespread in K8s ecosystem tutorials and docs
5. **Codespaces native**: GitHub's devcontainer feature is designed around Docker socket sharing

---

## Dynatrace Compatibility

| Feature | Kind | K3s |
|---|---|---|
| Operator deployment (Helm) | Works | Works (needs helm install or HelmChart CRD) |
| CloudNativeFullStack | Works | Works (containerd-based injection) |
| ApplicationMonitoring | Works | Works |
| ActiveGate | Works | Works |
| Log Monitoring | Works | Works |
| KSPM | Works | Works (different node paths) |
| RUM (via ingress) | Works | Works |
| OneAgent (host monitoring) | Works | Works |

K3s uses containerd as its runtime, which is fully supported by Dynatrace OneAgent. The DT Operator's CSI driver works the same way. No differences in monitoring capability.

**Note**: K3s nodes report as `k3s` in the kubelet version string (`v1.34.1+k3s1`). Dynatrace detects this correctly as a Kubernetes distribution.

---

## Recommendation

### For Training (current use case)

**Keep Kind for now**. Reasons:
- Students learn standard Kubernetes architecture (etcd, kube-proxy, etc.)
- Codespaces/DevContainers work reliably with socket sharing
- The framework is already built around Kind
- The teaching value of seeing "real" K8s components outweighs the resource savings

### For CI/CD Nightly Tests

**Consider K3s**. Reasons:
- 3x faster startup = faster CI pipelines
- Less resources = cheaper infrastructure
- No Docker socket dependency = simpler CI runner setup
- HelmChart CRD simplifies app deployment in CI

### For Future DinD Goal

**Requires infrastructure changes**, not code changes:
- Use Sysbox runtime on the CI server, OR
- Use dedicated VMs (not nested containers), OR
- Wait for kernel improvements in cgroup v2 delegation

The limitation is at the Linux kernel/cgroup level, not in Kind or K3s. Neither can work in a nested container without proper cgroup delegation from the host.

---

## Tested On

- **Host**: Ubuntu 24.04, kernel 6.17.0-1010-aws
- **Docker**: 27.5.1
- **Kind**: v0.22.0 (node: kindest/node:v1.30.0)
- **K3s**: v1.34.1+k3s1 / v1.35.4+k3s1
- **cgroup**: v2 (unified hierarchy)
- **Date**: 2026-05-05
