# GKE custom ComputeClass examples

Example configurations for Google Kubernetes Engine's **custom compute class**
feature. See the [custom compute class documentation](https://cloud.google.com/kubernetes-engine/docs/concepts/about-custom-compute-classes)
for the authoritative reference.

## Watch a demo

[![Custom Compute Class Demo](https://img.youtube.com/vi/wxlE0FqeaNY/0.jpg)](https://www.youtube.com/watch?v=wxlE0FqeaNY)

## Prerequisites

- Works with both GKE **Standard** and **Autopilot**.
- Requires GKE **1.30.3-gke.1451000+**. Eligible clusters get the ComputeClass CRD
  automatically — no special flags at create/update.
- A few examples need newer features (e.g. `priorityScore` needs
  1.35.2-gke.1842000+); each folder README notes its own requirements.

## The `status.sh` helper

`status.sh` prints node pools, nodes, and pods. Edit it and set `CLUSTER_NAME` and
`LOCATION` at the top, then make it executable:

```bash
chmod 750 status.sh
```

It assumes you're authenticated to `gcloud` and your kubeconfig points at the target
cluster. Many examples use `watch ./status.sh` to observe nodes and pods.

## Examples

Each folder has its own README with the full walkthrough.

| Example | What it demonstrates |
|---------|----------------------|
| [machineFamily](./machineFamily) | Rank whole machine families — the simplest priority rule |
| [machineType](./machineType) | Pin exact shapes + Spot → On-Demand fallback |
| [storage](./storage) | Custom boot disk type/size + attach local SSD |
| [static-node-pools](./static-node-pools) | Reference pre-created pools; fallback + active migration (Standard only) |
| [hybrid-pools](./hybrid-pools) | Curated manual pool on top, node auto-provisioning long-tail fallback; intent matches manual pools (churn-resilient) |
| [priority-tiebreak](./priority-tiebreak) | `priorityScore` — let GKE pick the cheapest-available family in a tier |
| [balanced-reservations](./balanced-reservations) | Node- vs pod-level "balanced"; multi-zone Specific reservations |
| [large-shape-fallback](./large-shape-fallback) | Progressive smaller-shape fallback for scarce >32 vCPU shapes |
| [arm-axion](./arm-axion) | Prefer Arm (Axion `c4a`), x86 fallback; multi-arch image + arm64 taint |
| [spot-batch](./spot-batch) | Spot-**primary** for fault-tolerant batch (inverse of the serving rule) |
| [memory-optimized](./memory-optimized) | m-series for RAM-bound workloads (high memory-per-vCPU) |
| [stateful-db](./stateful-db) | Zone-pinned PostgreSQL; reservation-first, all Gen-4 Hyperdisk |
| [gpu-accelerator](./gpu-accelerator) | vLLM Gemma 4 GPU inference priority ladder + the required GPU toleration |
| [gpu-sharing](./gpu-sharing) | Pack many pods on one GPU via time-sharing (`gpuSharing`) |
| [flexstart-batch](./flexstart-batch) | Dynamic Workload Scheduler flex-start: queued, time-bounded GPU capacity for batch/training |
| [tpu-serving](./tpu-serving) | Provision a single-host TPU v6e slice (`tpu` priority block) |
| [system-pool](./system-pool) | A cheap class for non-DaemonSet `kube-system` pods |
| [kueue-ccc](./kueue-ccc) | Kueue job queueing layered on top of ComputeClasses |
| [default-class-namespace](./default-class-namespace) | Make a class the namespace default via label — no per-pod nodeSelector |
| [restrict-usage](./restrict-usage) | Govern a class: RBAC for who can edit it + a ValidatingAdmissionPolicy for who can request it |
| [capacity-quota](./capacity-quota) | Limit ComputeClass scale-ups via a CapacityQuota custom resource |
| [observability](./observability) | Traceability from pending pods to scaled nodes (`ccc_priority_index`), status conditions (`ProvisioningSuspended` vs `ProvisioningConstrained`), and hard stockout monitoring (1.36.4+) |
| [priority-fulfillment](./priority-fulfillment) | Historical, capacity-normalized dashboards of how often priority rule 0 was fulfilled vs. fallbacks, via a `ccc_priority_index` exporter and Managed Prometheus (1.33+). Five portable Cloud Monitoring dashboards — one class, fleet by vCPU / GPU chip / TPU chip, and scale-up health — installed into any project with `./install-dashboards.sh` |
| [agent-sandbox](./agent-sandbox) | GKE Agent Sandbox (gVisor and microVM): warm pools on a wide fallback ladder, plus the constraints. Claimed sandboxes pin nodes, eviction resets sessions silently, and whole-pod Pod Snapshots narrow the class to one family with Guaranteed QoS, while rootfs-only snapshots keep the wide ladder but preserve only files |
| [agent-substrate](./agent-substrate) | Agent Substrate (microVM and gVisor): one ComputeClass per snapshot lineage. Size fallback is safe, cross-family fallback crashes restores, and microVM classes need a seed. Plus the interactive [scale control tower](./agent-substrate/scale-demo) |

### Agent sandbox: what survives a restart

Pick the persistence option first; it decides how wide the ComputeClass can be. Details in
the [agent-sandbox README](./agent-sandbox#pick-the-persistence-option).

| Option | What is preserved | What is lost | Machine-family fallback | Typical resume cost | When to use |
|---|---|---|---|---|---|
| **Stateless** (no snapshots) | Nothing. A claimed sandbox keeps its node until the session ends | Everything on any pod restart; eviction is a silent reset | Any family and size: wide ladder | No resume. A new sandbox starts cold, or a claim adopts a warm-pool sandbox | Disposable sessions, or state held outside the pod |
| **Pod Snapshots, rootfs-only** (`snapshotScope: rootfs-only`) | Files on the container root filesystem | Memory, running processes (the container starts again) and `/tmp` (tmpfs under gVisor) | Crosses families: wide ladder | Restore plus a process restart. Not quoted here: measure it with Resume → Ready in the demo | State lives in files (workspaces, checkouts, notebooks) and the process can be restarted |
| **Pod Snapshots, whole-pod** (`snapshotScope: whole-pod`) | Memory, processes, `/tmp` and the root filesystem | Nothing, if the restore applies. On another machine family (or different CPU features) the pod cold-starts silently | **One family**, size fallback only, with Guaranteed QoS | Restore. Not quoted here: measure it with Resume → Ready in the demo | State lives in memory or running processes and cannot be rebuilt |
