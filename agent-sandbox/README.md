# GKE Agent Sandbox on ComputeClasses (gVisor and microVM)

This example provisions nodes for [GKE Agent Sandbox](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/machine-learning/agent-sandbox)
with ComputeClasses. Agent Sandbox runs on both GKE Sandbox runtimes:
`runtimeClassName: gvisor` and `runtimeClassName: microvm` (Kata Containers with Cloud
Hypervisor).

For stateless sessions, a ComputeClass works as it does for any other workload. Any family
and any size can serve any sandbox, so a wide fallback ladder is safe, and warm pools refill
on whatever the class provides. The constraints come from **session state** and
**disruption**:

- A claimed sandbox keeps its node until the session ends.
- Any pod restart resets the session silently.
- Keeping state across restarts takes GKE Pod Snapshots (gVisor only). Whole-pod snapshots
  narrow the class to **one machine family**; rootfs-only snapshots keep the wide ladder
  but preserve only files. See [Pick the class](#pick-the-class-by-whether-sessions-keep-state).

> **Requires GKE 1.36.3-gke.1767000+** for the Agent Sandbox add-on, and **1.37.0-gke.4713000+**
> for the microVM class. Pod Snapshots is a separate, optional cluster feature. See
> [Prerequisites](#prerequisites).

## Prerequisites

Versions and setup steps change, so treat the linked GKE pages as the source of truth.

| Dependency | Needed for | Minimum GKE | How to set it up |
|---|---|---|---|
| Agent Sandbox add-on (`--enable-agent-sandbox`) | Everything except the baseline Deployment | 1.36.3-gke.1767000 | [Enable Agent Sandbox on GKE](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/how-install-agent-sandbox) |
| GKE Sandbox, gVisor | `agent-sandbox-gvisor*` classes | — | Nothing to install: the class's `nodePoolConfig.sandbox` enables it on each auto-created node. See [Isolate workloads by using GKE Sandbox](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/sandbox-pods#choose-sandbox-auto-created). |
| GKE Sandbox, microVM, with nested virtualization | `agent-sandbox-microvm` | 1.37.0-gke.4713000 | [GKE Sandbox](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/sandbox-pods#choose-sandbox-auto-created) and [Use nested VMs with GKE clusters](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/nested-virtualization). Pick series with nested virtualization from the [machine series comparison](https://docs.cloud.google.com/compute/docs/machine-resource). |
| Pod Snapshots (`--enable-pod-snapshots`), a Cloud Storage bucket, IAM, Workload Identity | `agent-sandbox-gvisor-snapshots.yaml` only | 1.35.3-gke.1234000 | [Save and restore Agent Sandbox environments with Pod snapshots](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/agent-sandbox-pod-snapshots). Restore rules: [About GKE Pod snapshots](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/pod-snapshots#limitations). |
| Node pool auto-creation by ComputeClasses | All classes | 1.33.3-gke.1136000 (earlier versions also need cluster-level node auto-provisioning) | [About custom ComputeClasses](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/about-custom-compute-classes) |

The Agent Sandbox guide documents gVisor. microVM is a GKE Sandbox runtime, and the add-on's
admission policies accept it with no extra setup.

## Best practices

### Pick the class by whether sessions keep state

| Session state | Class | Ladder |
|---|---|---|
| None (default) | `agent-sandbox-gvisor` or `agent-sandbox-microvm` | Wide: N4 / C4 / C3 (plus AMD and N2 for gVisor), 8 and 16 cores |
| Pod Snapshots, rootfs-only | `agent-sandbox-gvisor-rootfs-snapshots` (or `agent-sandbox-gvisor`) | Wide. Only files survive, and not `/tmp` |
| Pod Snapshots, whole-pod | `agent-sandbox-gvisor-snapshots` | **One family**, size fallback only (N4 8 → 16 → 4) |

A whole-pod snapshot restores only on the machine family it was taken on, so the snapshot
class keeps to one family and varies only size. A rootfs-only snapshot carries no memory or
CPU state, so it restores across families.

### Pick the persistence option

| Option | What is preserved | What is lost | Machine-family fallback | Typical resume cost | When to use |
|---|---|---|---|---|---|
| **Stateless** (no snapshots) | Nothing. A claimed sandbox keeps its node until the session ends | Everything on any pod restart; eviction is a silent reset | Any family and size: wide ladder | No resume. A new sandbox starts cold, or a claim adopts a warm-pool sandbox | Disposable sessions, or state held outside the pod |
| **Pod Snapshots, rootfs-only** (`snapshotScope: rootfs-only`) | Files on the container root filesystem | Memory, running processes (the container starts again) and `/tmp` (tmpfs under gVisor) | Crosses families: wide ladder | Restore plus a process restart. Not quoted here: measure it with Resume → Ready in the demo | State lives in files (workspaces, checkouts, notebooks) and the process can be restarted |
| **Pod Snapshots, whole-pod** (`snapshotScope: whole-pod`) | Memory, processes, `/tmp` and the root filesystem | Nothing, if the restore applies. On another machine family (or different CPU features) the pod cold-starts silently | **One family**, size fallback only, with Guaranteed QoS | Restore. Not quoted here: measure it with Resume → Ready in the demo | State lives in memory or running processes and cannot be rebuilt |

The rootfs-only and whole-pod rows are GKE Pod Snapshots modes (gVisor only, GKE 1.35.3+,
see [Prerequisites](#prerequisites)). Their restore rules are in
[About GKE Pod snapshots](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/pod-snapshots#limitations).
The [scale demo](./scale-demo) runs all three side by side, so you can read the resume
latency and, for rootfs-only, what a resume restored.

### Set the runtime on the class

`nodePoolConfig.sandbox.type: gvisor | microvm` gives each auto-created node the label and
taint `sandbox.gke.io/runtime=<type>`. That is exactly what the admission policy requires
every sandbox pod to select and tolerate.

### Write pods that pass the admission policy

`sandbox-core-policy` and `sandbox-hardening-policy` (both Deny) check every Sandbox and
SandboxTemplate:

- `runtimeClassName` is `gvisor` or `microvm`. `nodeSelector` has
  `sandbox.gke.io/runtime: <same>` and a matching `NoSchedule` toleration. Add
  `cloud.google.com/compute-class` next to it.
- `automountServiceAccountToken: false`. No host namespaces, `hostPath`, `hostPort`,
  privileged mode, sysctls or `procMount`.
- `runAsNonRoot`, drop `ALL` capabilities, no added capabilities.
- CPU and memory limits. **Memory request ≥ 1 GiB per vCPU of request.**

### microVM specifics

- Set nested virtualization in `priorityDefaults.enableNestedVirtualization`. The CRD
  requires it on every auto-created rule, and `nodePoolConfig.enableNestedVirtualization`
  is rejected.
- The `microvm` RuntimeClass adds **250m CPU / 130Mi** of pod overhead. Include it in node
  sizing.
- Use Intel families. C4D and C3D don't expose nested virtualization to guests.

### Use Guaranteed QoS

The templates set requests equal to limits. That costs nothing without snapshots, and with
whole-pod snapshots it is what lets a session restore on a node of a different size (see
[Caveats](#caveats)).

### Leave `activeMigration` off

Claimed sandboxes aren't backed by a controller the autoscaler can move, so
`activeMigration` doesn't move sessions back to a preferred rule. Plan for fallback nodes
to stay until their sessions end.

## Files

| File | What it is |
|---|---|
| [`agent-sandbox-gvisor-class.yaml`](./agent-sandbox-gvisor-class.yaml) | `agent-sandbox-gvisor` (wide) and `agent-sandbox-gvisor-snapshots` (one family) |
| [`agent-sandbox-microvm-class.yaml`](./agent-sandbox-microvm-class.yaml) | `agent-sandbox-microvm` (Intel, nested virtualization) |
| [`agent-sandbox-template.yaml`](./agent-sandbox-template.yaml) | SandboxTemplate, WarmPool and Claim for gVisor, and a template and WarmPool for microVM |
| [`agent-sandbox-gvisor-snapshots.yaml`](./agent-sandbox-gvisor-snapshots.yaml) | Optional: whole-pod Pod Snapshots storage config, policy, and a per-session Sandbox (one-family class) |
| [`agent-sandbox-gvisor-rootfs-snapshots.yaml`](./agent-sandbox-gvisor-rootfs-snapshots.yaml) | Optional: the wide `agent-sandbox-gvisor-rootfs-snapshots` class, a rootfs-only policy (`snapshotScope: rootfs-only`), and a per-session Sandbox |
| [`agent-sandbox-gvisor-deploy.yaml`](./agent-sandbox-gvisor-deploy.yaml) | Baseline Deployment for checking provisioning without the add-on |

## Deploy

```bash
gcloud beta container clusters update <CLUSTER_NAME> --location=<LOCATION> --enable-agent-sandbox

kubectl get crd sandboxes.agents.x-k8s.io sandboxtemplates.extensions.agents.x-k8s.io \
  sandboxwarmpools.extensions.agents.x-k8s.io sandboxclaims.extensions.agents.x-k8s.io
kubectl get validatingadmissionpolicy sandbox-core-policy sandbox-hardening-policy

kubectl apply -f agent-sandbox-gvisor-class.yaml -f agent-sandbox-microvm-class.yaml
kubectl apply -f agent-sandbox-template.yaml
```

A `SandboxClaim` adopts a warm Sandbox from the pool named in `warmPoolRef`, and the pool
refills.

For snapshots, first enable the feature and create the bucket, custom role and IAM bindings
per [Save and restore Agent Sandbox environments with Pod snapshots](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/agent-sandbox-pod-snapshots).
Enabling it on an existing regional cluster is a long-running update. Then edit the bucket
name in `agent-sandbox-gvisor-snapshots.yaml` (whole-pod) or
`agent-sandbox-gvisor-rootfs-snapshots.yaml` (rootfs-only) and apply it.

## Observe

```bash
kubectl get sandboxwarmpool,sandboxclaim,sandbox,pod
kubectl get nodes -L cloud.google.com/compute-class,sandbox.gke.io/runtime,node.kubernetes.io/instance-type
kubectl get computeclass agent-sandbox-gvisor -o jsonpath='{.status.conditions}'   # fallback reasons
```

When a rule is out of quota or capacity, the class reports `ProvisioningSuspended` (with
reason `QuotaExceeded` or `OutOfResources`) and warm-pool pods land on the next rule.

## Caveats

### Disruption and lifecycle

- **Claimed sandboxes block scale-down.** The autoscaler reports
  `no.scale.down.node.pod.not.backed.by.controller` for a node with a claimed sandbox, and
  doesn't consolidate it. A session that starts on a fallback node stays there, and **so
  does the node, until the session ends.** Budget for fallback capacity to outlive the
  stockout.
- **Eviction is a silent reset.** The Sandbox controller recreates an evicted pod with a new
  IP and fresh process state, and the `SandboxClaim` stays `Ready` throughout. Upgrades,
  maintenance and drains reset sessions without any signal to the client. There is no PDB
  and no `safe-to-evict` annotation by default.
- **`operatingMode: Suspended` without Pod Snapshots deletes the pod.** Setting `Running`
  again is a cold start.

### Pod Snapshots (gVisor only)

- **A whole-pod restore on another family is a silent cold start.** The snapshot records
  `podsnapshot.gke.io/node-machine-family`, and pods on other families don't use it. No
  error or event says so. That's why the snapshot class keeps to one family.
- **CPU features can also differ by zone.** GKE requires a whole-pod restore to match the
  CPU features of the host that took the snapshot, and some features vary by zone within
  one family. If a session must resume with its state, keep it in one zone.
- **Burstable pods can't restore on a node of a different size.** The kubelet derives the
  container's `OOMScoreAdj` from memory request ÷ node memory, so it differs between, say,
  `n4-standard-8` and `n4-standard-16`. The restore fails spec validation, and the
  container is left in `RunContainerError` instead of cold-starting. Recreating the pod
  fails against the same snapshot. Guaranteed QoS avoids this.
- **Suspend doesn't take a snapshot, so the next resume rolls back.** With a manual policy,
  `Suspended` deletes the pod without a snapshot, and `Running` restores the **last manual**
  snapshot. Trigger a `PodSnapshotManualTrigger` right before suspending.
- **rootfs-only crosses families but loses `/tmp`.** Files persist and the process
  restarts: anything held in memory is gone. gVisor mounts `/tmp` as tmpfs, so it isn't in
  the snapshot. Write state elsewhere, such as `/home`. See
  `agent-sandbox-gvisor-rootfs-snapshots.yaml`.
- **Snapshots and warm pools don't combine cleanly.** Restore is keyed by a pod label
  (`groupByLabelValue`) that has to exist when the pod is created. A warm pod starts before
  anyone knows which session it will serve. The snapshot file uses one Sandbox per session
  instead.
- **A trigger that never completes usually means a missing node agent.** Pod Snapshots runs
  a `pod-snapshot-agent` DaemonSet in `gke-managed-pod-snapshots`. Without it a
  `PodSnapshotManualTrigger` stays at `observedGeneration: 1` with no event or error, and
  suspending the Sandbox hangs with `FailedKillPod`. Check
  `kubectl get ds -n gke-managed-pod-snapshots` first. If a `--disable-pod-snapshots` left that namespace stuck `Terminating`, re-enabling won't recover it: the agent pods keep a `podsnapshot.gke.io/token-finalizer` that only the removed controller clears. Remove that finalizer from the leftover pods; the add-on then recreates the namespace and DaemonSet within a minute.
- **Turning Pod Snapshots on or off pauses node auto-creation.** Each toggle is a cluster
  update that held the cluster in `RECONCILING` for about 10 minutes in our runs. While it
  runs, every class priority reports `NodeProvisioning ... failed due to the InternalError
  error` and pending pods wait.
- **`automountServiceAccountToken: false` is compatible with Pod Snapshots,** even though the
  GKE guide's sample omits it.

### Agent Sandbox is not Agent Substrate

This add-on uses GKE Sandbox nodes and RuntimeClasses.
[Agent Substrate](../agent-substrate) brings its own runtimes and has different
constraints.
