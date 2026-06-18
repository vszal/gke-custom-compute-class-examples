# Restrict who can manage and use a ComputeClass

Governing an expensive ComputeClass takes **two independent controls** — and the
common mistake is reaching for one when you need the other:

| Control | Protects | Mechanism | File |
|---------|----------|-----------|------|
| **CRUD** | who can **create/modify** the class *object* | RBAC `ClusterRole` | [`rbac-editor.yaml`](./rbac-editor.yaml) |
| **Consumption** | who can **request** the class from a workload | `ValidatingAdmissionPolicy` | [`restrict-usage-vap.yaml`](./restrict-usage-vap.yaml) |

**Why two?** RBAC governs verbs on the ComputeClass *object* (`create`, `update`,
`patch`, `delete`). But a workload "using" a class isn't a verb on the object — it's
a field in the **Pod spec** (`nodeSelector` / `nodeAffinity` / `toleration`). RBAC
can't see pod-spec fields, so it **cannot** stop a team from requesting a class.
That's an admission-time check. There is also **no field on the ComputeClass** that
allow-lists namespaces — consumption control lives in the admission policy, not on
the object.

> **Requires** Kubernetes/GKE with `ValidatingAdmissionPolicy` (GA in 1.30+).
> RBAC binding to a Google Group requires Google Groups for GKE RBAC enabled on the
> cluster.

The protected class here is [`restricted-class.yaml`](./restricted-class.yaml) — a
GPU class (`gpu-restricted`) that's costly to leave open.

## Control 1 — lock down CRUD with RBAC

[`rbac-editor.yaml`](./rbac-editor.yaml) grants the mutating verbs only to a Google
Group:

```bash
kubectl apply -f rbac-editor.yaml      # set <GROUP_DOMAIN> first
```

- ComputeClass is **cluster-scoped**, so this is a `ClusterRole` + `ClusterRoleBinding`
  — a namespaced `Role` can't grant access to it.
- It grants `create`, `update`, **`patch`, `delete`** — not just `create`+`update`,
  or a non-creator could still patch/delete an existing class.

Verify (member → `yes`, everyone else → `no`):

```bash
kubectl auth can-i create computeclasses.cloud.google.com --as=<member@your-domain>
kubectl auth can-i delete computeclasses.cloud.google.com --as=<other@your-domain>
```

## Control 2 — restrict consumption with a ValidatingAdmissionPolicy

[`restrict-usage-vap.yaml`](./restrict-usage-vap.yaml) denies any workload in the
`restricted-demo` namespace that tries to reach `gpu-restricted`. The CEL closes
**all three** access paths — miss one and it leaks:

1. `nodeSelector` → `cloud.google.com/compute-class: gpu-restricted`
2. `nodeAffinity` → `matchExpressions` key `cloud.google.com/compute-class` `In [gpu-restricted]`
3. `tolerations` → tolerating the class's `NoSchedule` taint, **including the
   wildcard** (`operator: Exists` with no key, which tolerates *every* taint).

`matchConstraints` also covers every pod-controller kind (Deployments, StatefulSets,
DaemonSets, ReplicaSets, Jobs, CronJobs) plus bare Pods — not just pods+deployments,
or a StatefulSet/Job would slip past.

```bash
kubectl create namespace restricted-demo
kubectl apply -f restrict-usage-vap.yaml
```

**Roll out Audit-first.** Before enforcing, set `validationActions: ["Audit"]` in
the binding to surface existing violators in the audit log, then switch to
`["Deny", "Audit"]`.

## Deploy & observe

```bash
kubectl apply -f restricted-class.yaml
kubectl create namespace restricted-demo
kubectl apply -f restrict-usage-vap.yaml

# Compliant workload (no gpu-restricted request) — ADMITTED:
kubectl apply -f allowed-deploy.yaml
kubectl get pods -n restricted-demo -o wide
```

## Test the deny

Each of these should be **rejected** by the policy (try them as server dry-runs so
nothing is created):

```bash
# 1. nodeSelector
kubectl -n restricted-demo run gpu-probe --image=nginx --dry-run=server \
  --overrides='{"spec":{"nodeSelector":{"cloud.google.com/compute-class":"gpu-restricted"}}}'

# 2. wildcard toleration (tolerates every taint, incl. the class taint)
kubectl -n restricted-demo run wild-probe --image=nginx --dry-run=server \
  --overrides='{"spec":{"tolerations":[{"operator":"Exists"}]}}'
```

Expected:

```
admission ... denied the request: This namespace cannot request ComputeClass
gpu-restricted or use wildcard tolerations.
```

The same request in a namespace **not** selected by the binding is admitted — scope
is controlled by the binding's `namespaceSelector`.

## Cleanup

```bash
kubectl delete -f restrict-usage-vap.yaml --ignore-not-found
kubectl delete -f rbac-editor.yaml --ignore-not-found
kubectl delete -f restricted-class.yaml --ignore-not-found
kubectl delete namespace restricted-demo --ignore-not-found
```
