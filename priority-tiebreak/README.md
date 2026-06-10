# priorityScore cost-based tie-breaking

This example shows how to let GKE pick the **cheapest-available** machine family
from a group of equivalent options, instead of baking a fixed family order into
your YAML that goes stale as prices and availability change.

## When to use it

- **Stateless workloads** — web servers, APIs, async/batch processors — where
  several machine families are functionally interchangeable.
- You hold **CUDs or reservations** across multiple families (e.g. c4d, c4, n4)
  and want GKE to prefer whichever committed capacity is cheapest right now.
- You want availability spread across families/generations without hand-ranking
  them.

## How `priorityScore` works

- `priorityScore` is an integer `1-1000`; **higher = more preferred**.
- Rules that share a score form one **tier** and are evaluated **together**.
  Within a tier, GKE breaks the tie by **lowest unit cost** — it provisions the
  cheapest family in that tier that also has capacity right now.
- A **maximum of 3 rules** may share the same score.
- **All-or-nothing:** if any rule sets `priorityScore`, every rule must.

The tiers in [`tiebreak-class.yaml`](./tiebreak-class.yaml):

| Score | Families        | Role                                            |
|-------|-----------------|-------------------------------------------------|
| 100   | c4d, c4, n4     | Top tier — three equivalent Gen-4 On-Demand     |
| 50    | n2, n2d         | Fallback — Gen-2, broader availability          |
| 10    | e2              | Floor — widest zone net, guarantees execution   |

## Requirements

- GKE **1.35.2-gke.1842000** or later (required for `priorityScore`).
- Works on Standard and Autopilot.

> Edit the `machineFamily` entries to match the families you actually hold CUDs
> or reservations for — that's what makes the cost tie-break land on your
> committed capacity.

## Deploy

```bash
# 1. Create the ComputeClass
kubectl apply -f tiebreak-class.yaml

# 2. Deploy the stateless web tier (12 replicas)
kubectl apply -f tiebreak-deploy.yaml
```

## Observe which family GKE picked

```bash
# Machine type per node (look at the INSTANCE-TYPE column)
kubectl get nodes -o wide

# Surface the machine family (the axis this class selects on), the exact shape,
# and the compute class as columns
kubectl get nodes -L cloud.google.com/machine-family,node.kubernetes.io/instance-type,cloud.google.com/compute-class
```

You should see nodes from the tier-1 families (c4d / c4 / n4) when they have
capacity, falling back to n2/n2d, then e2.

## Notes

- Without any `priorityScore`, ComputeClass falls back to **strict declaration
  order** (top priority first, then the next, etc.). Add scores only when you
  want the cost tie-break behavior.
- Need more than 3 families at the "same" preference? You can't — cap a tier at
  3 and push the rest into a lower tier.
