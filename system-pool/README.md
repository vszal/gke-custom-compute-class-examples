# Dedicated cheap pool for kube-system pods

This example stops singleton `kube-system` pods from pinning expensive nodes and
blocking the autoscaler from consolidating them.

> **No Deployment here.** Unlike the other examples, the binding is a **label on
> the `kube-system` namespace**, not a workload you apply. The class captures the
> system pods that are already running.

## The problem

System pods — **metrics-server**, **coredns**, **konnectivity-agent**, custom
operators in `kube-system` — frequently:

- have **no PodDisruptionBudget**, and
- carry `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"` defensively.

They land on whichever node the scheduler picks first — often an expensive
**GPU/TPU or large** node — and then pin it. The autoscaler can't drain that node
and logs:

```
noScaleDown ... reason messageId = "no.scale.down.node.pod.kube.system.unmovable"
```

So a 200m-CPU metrics-server can hold a costly node alive indefinitely.

## The fix

Route **non-DaemonSet** system pods to a dedicated, cheap class
([`system-pool-class.yaml`](./system-pool-class.yaml) — small n4 nodes). Expensive
nodes are then free of system pods and consolidate normally.

**Why non-DaemonSet?** DaemonSets (node-exporter, CNI agents, etc.) *must* run on
every node, so they're deliberately excluded. The autoscaler already ignores
DaemonSet-only nodes for scale-down, so there's nothing to gain from rerouting
them.

## Apply

### 1. Create the ComputeClass

```bash
kubectl apply -f system-pool-class.yaml
```

(If you hold committed use discounts/reservations for a different family, edit `machineFamily` /
`minCores` first.)

### 2. Label the kube-system namespace

```bash
kubectl label namespace kube-system \
  cloud.google.com/default-compute-class-non-daemonset=system-pool
```

This tells GKE: for **non-DaemonSet** pods in `kube-system`, use the `system-pool`
class. DaemonSets keep running everywhere.

### 3. Move existing pods

Existing system pods **do not reschedule on their own** — the class only affects
pods as they're (re)created. Wait for natural restarts, or force them:

```bash
kubectl rollout restart deployment -n kube-system metrics-server
kubectl rollout restart deployment -n kube-system coredns
```

## Verify

```bash
# System pods should now be on the n4 system-pool nodes, not GPU/TPU/large nodes
kubectl get pods -n kube-system -o wide

# Confirm the namespace label is set
kubectl get namespace kube-system -o jsonpath='{.metadata.labels}'

# Machine family per node — the system-pool nodes should show n4
kubectl get nodes -L cloud.google.com/machine-family
```

Once the system pods are off the expensive nodes, the
`no.scale.down.node.pod.kube.system.unmovable` messages for those nodes stop and
the autoscaler can consolidate them.

## Notes

- Keep `spot: false` — don't put system pods on preemptible capacity.
- `whenUnsatisfiable: ScaleUpAnyway` is deliberate: a Pending kube-system pod is
  worse than an extra node.
- If pods are still on expensive nodes, re-check the namespace label and that the
  pod has actually restarted since you applied it.
