# Default ComputeClass for a namespace

Make a ComputeClass the **default for every pod in a namespace** with a single
label — teams ship plain manifests and GKE applies the class for them. No
per-workload `nodeSelector` to add (or forget).

> **Requires GKE 1.33.1-gke.1788000+** for namespace-level defaults.

## How it works

Label the namespace with the class name:

```bash
kubectl create namespace team-a
kubectl label namespace team-a \
  cloud.google.com/default-compute-class=team-default
```

For any pod created in `team-a` that **doesn't already select a class**, GKE
mutates the pod spec to add the `team-default` class. The
[`team-deploy.yaml`](./team-deploy.yaml) here has **no** `cloud.google.com/compute-class`
nodeSelector — that's the point.

## Per-pod override

The default only fills a gap. A pod that sets its own
`cloud.google.com/compute-class` nodeSelector keeps it — the namespace default is
ignored for that pod. So a team can opt a single workload onto, say,
[gpu-accelerator](../gpu-accelerator) while everything else rides the namespace
default.

## Caveat — never label a system namespace

The plain label affects **DaemonSets** too. Setting it on `kube-system` can
disrupt the cluster. For system namespaces use the non-DaemonSet variant instead:

```bash
cloud.google.com/default-compute-class-non-daemonset=<class>
```

That's exactly what [system-pool](../system-pool) does for `kube-system`. The two
labels are a pair: `default-compute-class` for your own team namespaces,
`default-compute-class-non-daemonset` for system ones.

## Deploy & observe

```bash
kubectl apply -f default-class.yaml
kubectl label namespace team-a \
  cloud.google.com/default-compute-class=team-default
kubectl apply -f team-deploy.yaml

# The pods carry no nodeSelector in the manifest, yet land on team-default nodes:
kubectl get pods -n team-a -o wide
kubectl get nodes -L cloud.google.com/compute-class,cloud.google.com/machine-family
```
