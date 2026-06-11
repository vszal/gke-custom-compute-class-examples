# Hybrid: curated manual pools on top, NAP long-tail fallback

A common production shape. Your **baseline** is one or more node pools you created
and **curated** yourself; the **long tail** is node pool auto-creation (NAP)
provisioning generic capacity only when the baseline runs out.

- **Manual on top — control.** A pre-created pool can be backed by a
  reservation/CUD and can use configuration NAP either doesn't create for you or
  that you want pinned exactly: specific node system / kubelet tuning, a particular
  disk or networking setup, compact placement, etc. Pods fill it first.
- **NAP underneath — safety / obtainability.** When the curated pool is full or
  unavailable, the lower-priority rules let NAP provision fallback nodes so pods
  don't sit Pending. Best-effort capacity, not curated.

## Two ways to reference a manual pool — and why intent wins

A manually created pool is associated with the class by **labeling and tainting**
it (not by naming it in the spec):

```bash
gcloud container node-pools create reserved-c4-pool \
  --cluster CLUSTER_NAME --location LOCATION \
  --machine-type c4-standard-16 \
  --reservation-affinity specific --reservation RESERVATION_NAME \
  --node-labels "cloud.google.com/compute-class=hybrid-capacity" \
  --node-taints "cloud.google.com/compute-class=hybrid-capacity:NoSchedule"
```

Once labeled/tainted, the autoscaler evaluates each priority rule against
**existing** matching pools first. So two options reference that pool:

| | How | Churn behavior |
|---|---|---|
| **Explicit** | `nodepools: [reserved-c4-pool]` | Rename / recreate / blue-green the pool → you must **edit the class**. |
| **Intent-based** *(used here)* | `machineType: c4-standard-16`, `spot: false` | Any pool with the class label/taint that matches the intent qualifies → **no rewiring**. |

The intent form is fully declarative: state what you want, and the replacement
pool slots in as long as it carries the class label/taint and matches. See
[static-node-pools](../static-node-pools) for the pure-explicit `nodepools`
approach, and [machineFamily](../machineFamily) / [machineType](../machineType) for
intent rules driving NAP.

## Deploy & observe

```bash
# 1. Create + label/taint the curated baseline pool (command above).
# 2. Apply the class and workload:
kubectl apply -f hybrid-pools-class.yaml
kubectl apply -f hybrid-pools-deploy.yaml

# Small scale fits the curated pool:
kubectl get pods -o wide -l app=hybrid-app

# Scale past the baseline to watch NAP create fallback nodes:
kubectl scale deployment hybrid-app --replicas 40
kubectl get nodes -L cloud.google.com/compute-class,cloud.google.com/machine-family
```

As the curated pool frees up, `activeMigration.optimizeRulePriority` migrates pods
back onto it from the NAP fallback nodes.
