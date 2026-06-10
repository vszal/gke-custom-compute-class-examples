# GKE Custom Compute Class Examples

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
| [priority-tiebreak](./priority-tiebreak) | `priorityScore` — let GKE pick the cheapest-available family in a tier |
| [balanced-reservations](./balanced-reservations) | Node- vs pod-level "balanced"; multi-zone Specific reservations |
| [large-shape-fallback](./large-shape-fallback) | Progressive smaller-shape fallback for scarce >32 vCPU shapes |
| [stateful-db](./stateful-db) | Zone-pinned PostgreSQL; reservation-first, all Gen-4 Hyperdisk |
| [gpu-accelerator](./gpu-accelerator) | vLLM Gemma 4 GPU inference priority ladder + the required GPU toleration |
| [system-pool](./system-pool) | A cheap class for non-DaemonSet `kube-system` pods |
| [kueue-ccc](./kueue-ccc) | Kueue job queueing layered on top of ComputeClasses |
