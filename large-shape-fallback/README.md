# Large-shape obtainability with smaller-core fallback

A ComputeClass pinned to a single **large** machine shape is a common way to end
up with pods stuck `Pending`. This example shows why, and how a smaller-shape
fallback fixes it — along with the one case where that fallback does **not** help.

## The problem: large shapes are scarcer

Machine shapes larger than **32 vCPU** come from thinner capacity pools and hit
`out of resources` stockouts more often than smaller shapes. If your ComputeClass
lists **only** a large shape (say `c4-standard-48`) and that shape is exhausted
in your zones, the autoscaler has nowhere to land your pods and they sit
`Pending` indefinitely.

## The fix: progressively smaller fallback shapes

[`large-shape-class.yaml`](./large-shape-class.yaml) lists the preferred large
shape first, then progressively smaller shapes as fallbacks:

| Priority | Shape | Role |
|----------|-------|------|
| 1 | `c4-standard-48` | Preferred — densest packing, but scarcest |
| 2 | `c4-standard-16` | Fallback — wider availability |
| 3 | `c4-standard-8`  | Floor — most abundant safety net |

When the 48 stocks out, the autoscaler drops to the 16, then the 8, and
scheduling still succeeds.

**Why it works here:** the workload is many *small* pods (2 vCPU each), so a pod
fits on any of these shapes. You trade some bin-packing density (more nodes to
manage when only small shapes are available) for reliable scheduling.

## The hard caveat: a single >32 vCPU pod can't use shape fallback

node auto-creation sizes a node to fit the **Pod's requests**. So if a *single
pod* requests more than 32 vCPU, it cannot shrink onto a smaller node — the
smaller-shape fallbacks are useless for it. For big single pods, vary a different
axis instead:

- **Zone** — availability differs zone to zone.
- **Machine family** — e.g. fall back across `c4` → `c3` → `n4`.
- **Reservations** — guarantee capacity for predictable large workloads.

This example deliberately uses small pods so shape fallback is the right lever.

## Deploy

```bash
kubectl apply -f large-shape-class.yaml
kubectl apply -f large-shape-deploy.yaml
kubectl rollout status deployment/batch-workers
```

## Observe which shape was provisioned

```bash
# Machine type per node (INSTANCE-TYPE column)
kubectl get nodes -L node.kubernetes.io/instance-type

# How the 30 pods packed across nodes
kubectl get pods -o wide -l app=batch-worker
```

If the 48 had capacity you'll see ~2 dense nodes; if it stocked out, more 16- or
8-core nodes — but in every case the workload schedules.

## When to use this pattern

| Use it | Don't |
|--------|-------|
| Many small, independent pods (batch, web, microservices) | A single pod requesting >32 vCPU |
| Workload tolerates variable node count | Strict fixed-node-count requirements |
| Reliability matters more than perfect packing | Tight latency/placement constraints |
