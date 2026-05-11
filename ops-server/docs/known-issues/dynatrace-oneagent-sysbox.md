# Dynatrace OneAgent CloudNative — Sysbox Compatibility: k3d vs Kind

**Status:** Confirmed — fails on k3d, works on Kind (both inside Sysbox)  
**Component:** Dynatrace Operator / CloudNative Fullstack DynaKube  
**Error:**
```
Error: Initialization procedure failed
Using volume-based storage
Volume mount detected under /mnt/volume_storage_mount
Volume host path was not found on the same partition as host's root filesystem, trying fallback lookup
Error: Cannot determine volume host path from /proc/self/mountinfo:
  /var/lib/docker/volumes/354647ec.../93c2f31a.../_data/plugins/csi.oneagent.dynatrace.com/data/_dynakubes/codespaces-framework/
```

---

## Environment

```
EC2 host (Ubuntu, x86_64)
  └── Sysbox container (sb-{job_id})          ← outer Docker, Sysbox runtime
        ├── k3d-enablement-server-0            ← k3d node (inner Docker-in-Sysbox)
        │     └── k8s cluster
        │           ├── dynatrace-operator
        │           ├── dynatrace-oneagent-csi-driver  ← 4/4 Running ✓
        │           ├── codespaces-framework-activegate ← 1/1 Running ✓
        │           └── codespaces-framework-oneagent  ← 0/1 CrashLoopBackOff ✗
        └── dt (devcontainer)
```

---

## Root Cause

### OneAgent host-path resolution algorithm

The CloudNative Fullstack OneAgent initialisation does the following:

1. Reads `/proc/self/mountinfo` to find the source path of its CSI volume (`/mnt/volume_storage_mount`).
2. Checks whether that source path is on the same filesystem partition as the host root (`/mnt/root`, mounted via `hostPath: /`).
3. If yes: derives the "host path" of the volume by stripping the partition root prefix.
4. If no / fallback: tries to locate the path directly under `/mnt/root`.

### What `/proc/self/mountinfo` shows inside Sysbox

The CSI volume mount entry (extracted from the actual crash logs):

```
259:1  /var/lib/docker/volumes/354647ec.../93c2f31a.../plugins/
       csi.oneagent.dynatrace.com/data/_dynakubes/codespaces-framework/osagent
       /mnt/volume_storage_mount  rw,relatime,idmapped - ext4 /dev/root
```

- Device `259:1` is the EC2 host's root EBS volume (`/dev/root`).
- The source path (`/var/lib/docker/volumes/354647ec.../`) is a path **within the Sysbox container's Docker volume namespace**, not a path on the real EC2 host.

### Why the resolution fails

The `hostPath: /` volume on the OneAgent DaemonSet maps to the **k3d node container's root filesystem** (`k3d-enablement-server-0`). This is a container running _inside_ the Sysbox container — it is **not** the Sysbox container's own filesystem.

So:
- The CSI volume source is at `/var/lib/docker/volumes/354647ec.../` in the **Sysbox container's namespace**.
- The OneAgent's `hostPath` (`/mnt/root`) provides the **k3d node container's namespace**.
- These are two different filesystem namespaces; the path does not exist in the k3d node's view.

```
What OneAgent looks for:
  /mnt/root/var/lib/docker/volumes/354647ec.../csi.oneagent.dynatrace.com/...
         ^                ^
    k3d node's /     Sysbox's Docker path — doesn't exist inside the k3d node
```

Sysbox intentionally isolates these namespaces. This is not a misconfiguration; it is the expected behaviour of Sysbox's security model.

### Why even partition-matching fails

The OneAgent partition check compares the device number of `/mnt/root` against device `259:1`. Inside Sysbox, all `ext4 /dev/root` mounts appear with the same physical device ID. However, the k3d node's root filesystem itself is mounted as an overlay or idmapped bind-mount within Sysbox, which may present a different device major:minor to the OneAgent's partition check — triggering the fallback path.

Even if the partition check passed, the fallback would fail for the namespace reason above.

---

## k3d vs Kind: Why One Works and the Other Doesn't

The failure is **specific to k3d**, not to Sysbox in general. Verified by examining `/proc/self/mountinfo` inside each node type:

### k3d node — kubelet source path
```
259:1  /var/lib/docker/volumes/354647ec.../_data/volumes/93c2f31a.../_data
       /var/lib/kubelet  rw,relatime,idmapped - ext4 /dev/root
```
k3d externalises all node state into **Docker volumes** managed by the Sysbox container's Docker daemon. The source path (`/var/lib/docker/volumes/...`) is meaningful only within the Sysbox container's namespace — **it does not exist inside the k3d node container's own namespace**.

### Kind node — kubelet source path
```
259:1  /var/lib/sysbox/kubelet/0f86f1af1fe0075fcf34c8a45d4052a8451c1fafef5c1fad004069f5d5f93a06
       /mnt/root/var/lib/kubelet  rw,relatime,idmapped - ext4 /dev/root
```
Kind uses Sysbox's **first-class Kind integration**. Sysbox stores kubelet state at `/var/lib/sysbox/kubelet/{hash}/` on the real EC2 host filesystem (confirmed: `/var/lib/sysbox/kubelet/` exists on the worker). This path is properly mounted inside the Kind node container and is accessible from within it. When the OneAgent's `hostPath: /` mounts the Kind node's root at `/mnt/root`, the path `/mnt/root/var/lib/kubelet/pods/.../csi.oneagent.dynatrace.com/...` **exists and is readable** — the CSI path resolution succeeds.

### Summary
| Cluster engine | Kubelet storage backend | OneAgent CSI path resolves? | CloudNative Fullstack works? |
|---|---|---|---|
| k3d (k3s) | Docker volumes (`/var/lib/docker/volumes/...`) | No — path not in node namespace | **No — CrashLoopBackOff** |
| Kind | Sysbox-native (`/var/lib/sysbox/kubelet/...`) | Yes — on same device, in node namespace | **Yes** |

## Can It Be Fixed for k3d?

**Not without switching cluster engines.** The issue is that k3d's architecture externalises node state into Docker volumes, and there is no configuration knob to change where k3d stores its node data. The Sysbox CSI namespace boundary cannot be bridged.

---

## What Does Work for Observability

### Option 0: Switch to Kind as the cluster engine

If CloudNative Fullstack is required, switch from k3d to Kind. Sysbox has native Kind support and the OneAgent will initialise correctly. The `bug-busters` training sessions already run on Kind and can serve as a reference.

Trade-off: Kind has no built-in LoadBalancer (no equivalent to k3d's `serverlb`). Ingress requires deploying a separate nginx ingress controller and using NodePort for external access. The app-preview port-forwarding feature of the ops dashboard would need adjustment (Kind maps NodePorts, not a single LB port).

### Option 1: OpenTelemetry Collector → Dynatrace SaaS (already deployed, k3d path)

The astroshop stack already ships a `codespaces-framework-otel-collector` (1/1 Running). Configure it to forward to DT SaaS via OTLP:

```yaml
exporters:
  otlphttp:
    endpoint: "https://{dt-tenant}.live.dynatrace.com/api/v2/otlp"
    headers:
      Authorization: "Api-Token {token}"
```

This gives full traces, metrics, and logs without any host-level access. **This is the recommended path.**

### Option 2: Dynatrace Application-Only / Pod-Level injection

Dynatrace supports `applicationMonitoring` mode which injects only at the pod level using init containers and emptyDir volumes — no CSI driver, no hostPath. Deploy the DynaKube with:

```yaml
spec:
  applicationMonitoring:
    useCSIDriver: false
```

This avoids the CSI host-path resolution entirely. The agent runs inside the monitored pods, not as a node-level DaemonSet. **Untested in this environment — worth trying.**

### Option 3: ActiveGate + Kubernetes API monitoring

The `codespaces-framework-activegate` is already running (1/1). It can monitor the k8s API, collect events, and scrape Prometheus metrics from within the cluster without needing host-level access. Configure the DynaKube's `kubernetesMonitoring` section.

---

## References

- Sysbox documentation on filesystem isolation: https://github.com/nestybox/sysbox/blob/master/docs/user-guide/security.md
- DT CloudNative Fullstack prerequisites: requires `hostPath: /` to traverse directly to the physical node's filesystem.
- DT Application-Only mode: `applicationMonitoring.useCSIDriver: false`
