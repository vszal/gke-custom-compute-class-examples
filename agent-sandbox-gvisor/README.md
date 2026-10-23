# GKE Agent Sandbox with gVisor

Provision secure, multi-tenant sandboxed execution environments for AI agents using
**GKE Agent Sandbox** and **gVisor**, driven by a right-sized ComputeClass priority ladder.

> **Requires GKE 1.36.2-gke.1368000+** for `sandbox.type: gvisor` in ComputeClasses,
> and **GKE 1.36.3-gke.1767000+** for the GKE Agent Sandbox addon.

## What this example shows

AI agents frequently execute untrusted, dynamically generated code (such as Python scripts,
shell commands, or tool integrations). Standard Linux containers share the host kernel and
cannot provide sufficient isolation boundaries for multi-tenant agent execution.

This example pairs **gVisor** application kernel sandboxing with a curated **hybrid On-Demand
fallback ladder** in a custom ComputeClass:

[`agent-sandbox-gvisor-class.yaml`](./agent-sandbox-gvisor-class.yaml):

```yaml
spec:
  nodePoolConfig:
    sandbox:
      type: gvisor
    nodeLabels:
      workload-type: agent-sandbox
  priorities:
  # Tier 1: 8-core standard shapes across 5 families
  - machineType: c4-standard-8
  - machineType: n4-standard-8
  - machineType: c3-standard-8
  - machineType: n4d-standard-8
  - machineType: n2-standard-8
  # Tier 2: 16-core shapes for higher pod packing
  - machineType: c4-standard-16
  - machineType: n4-standard-16
  - machineType: c3-standard-16
  - machineType: n2-standard-16
  # Tier 3: Bounded family catch-all
  - machineFamily: n2
    minCores: 8
    minMemoryGb: 32
  activeMigration:
    optimizeRulePriority: true
  nodePoolAutoCreation:
    enabled: true
  whenUnsatisfiable: DoNotScaleUp
```

- `nodePoolConfig.sandbox.type: gvisor` configures node auto-provisioning to launch nodes
  with the `runsc` gVisor runtime. Nodes automatically receive the label
  `sandbox.gke.io/runtime=gvisor` and taint `sandbox.gke.io/runtime=gvisor:NoSchedule`.
- The priority ladder evaluates explicit 8-core and 16-core shapes across five modern x86
  machine families (`c4`, `n4`, `c3`, `n4d`, `n2`) before falling back to a bounded `n2`
  family catch-all. Targeting explicit shapes keeps the candidate evaluation set compact
  and predictable across zones.
- `activeMigration.optimizeRulePriority: true` smoothly migrates agent pools back up to higher
  priority tiers as primary capacity becomes available.

## Agent Sandbox CRDs and admission requirements

When the GKE Agent Sandbox addon is enabled, GKE deploys a native controller and enforces
a strict `ValidatingAdmissionPolicy` (`sandbox-core-policy`):

* **Runtime class**: Pods must declare `runtimeClassName: gvisor`.
* **Zero host access**: `hostNetwork`, `hostPID`, and `hostIPC` must be false or omitted.
  Privileged containers and `hostPath` volumes are strictly prohibited.
* **Identity protection**: `automountServiceAccountToken: false` is required to block
  unauthorized Kubernetes API access from agent scripts.
* **Non-root enforcement**: Containers must enforce `runAsNonRoot: true` and drop all Linux
  capabilities (`capabilities.drop: ["ALL"]`).
* **Resource bounds**: CPU and memory limits are mandatory. For Autopilot compatibility,
  memory requests must be at least 1 GiB per 1 vCPU.
* **Node placement & toleration**: Pods must specify `nodeSelector: sandbox.gke.io/runtime: gvisor`
  and tolerate `sandbox.gke.io/runtime=gvisor:NoSchedule`.

[`agent-sandbox-gvisor-template.yaml`](./agent-sandbox-gvisor-template.yaml) demonstrates
the full GKE Agent Sandbox lifecycle using three cooperating custom resources:
1. `SandboxTemplate`: Defines the blueprint container image, security context, and ComputeClass selector.
2. `SandboxWarmPool`: Keeps pre-started, warm sandbox pods ready to eliminate pod startup latency.
3. `SandboxClaim`: Claims a warm sandbox instantaneously for an incoming agent session.

[`agent-sandbox-gvisor-deploy.yaml`](./agent-sandbox-gvisor-deploy.yaml) provides a baseline
Kubernetes Deployment for testing node auto-creation even if the addon is not yet enabled.

## Enable the addon

Enable the GKE Agent Sandbox addon on your cluster:

```bash
gcloud beta container clusters update <CLUSTER_NAME> \
    --location=<LOCATION> \
    --enable-agent-sandbox
```

Verify that the CRDs and admission policy are installed:

```bash
kubectl get crd sandboxes.agents.x-k8s.io sandboxtemplates.extensions.agents.x-k8s.io sandboxwarmpools.extensions.agents.x-k8s.io sandboxclaims.extensions.agents.x-k8s.io
kubectl get validatingadmissionpolicy sandbox-core-policy
```

## Deploy

```bash
# 1. Apply the ComputeClass
kubectl apply -f agent-sandbox-gvisor-class.yaml

# 2. Deploy via Agent Sandbox warm pools:
kubectl apply -f agent-sandbox-gvisor-template.yaml

# Or deploy the baseline test Deployment:
kubectl apply -f agent-sandbox-gvisor-deploy.yaml
```

## Observe

```bash
# Track Agent Sandbox resources
kubectl get sandboxwarmpool,sandboxclaim,sandbox,pod

# Observe auto-provisioned nodes and runtime
kubectl get nodes -L cloud.google.com/compute-class,sandbox.gke.io/runtime,node.kubernetes.io/instance-type

# Verify gVisor runtime inside the container
POD_NAME=$(kubectl get pod -l sandbox-type=python-agent -o jsonpath='{.items[0].metadata.name}')
kubectl exec "${POD_NAME}" -- dmesg | head -n 5
```

The output will confirm the `runsc` sandbox kernel initializing in user space:
```
[    0.000000] Starting gVisor...
[    0.412000] Installing process table...
```
