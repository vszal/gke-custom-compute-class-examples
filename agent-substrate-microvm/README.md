# Agent Substrate with microVMs and nested virtualization

Run high-density autonomous AI agent workloads with sub-second state suspension and
restoration using **Agent Substrate** and hardware-isolated **microVMs** on GKE.

> **Requires GKE 1.37.0-gke.4713000+** for `sandbox.type: microvm` and
> `enableNestedVirtualization: true` in ComputeClasses.

## What this example shows

Autonomous AI agents frequently cycle between active compute bursts and idle waiting periods
(e.g., waiting for LLM inference, external tool API calls, or human feedback). Running a
dedicated, permanent pod per agent wastes significant compute capacity.

**Agent Substrate** decouples stateful agents (*actors*) from underlying compute nodes
(*workers*). When an agent becomes idle, Substrate snapshots its active memory to Google Cloud
Storage, scales the sandbox to zero, and frees the machine. When a request arrives, Substrate
claims a warm worker and restores the agent's memory snapshot in under 500 milliseconds.

For workloads requiring virtual-machine boundary isolation (e.g., untrusted binary execution
or custom OS kernels), Substrate uses lightweight **microVMs** (Kata Containers with Cloud
Hypervisor over `/dev/kvm`).

This example configures a custom ComputeClass that automates node auto-provisioning for
microVM nodes with nested virtualization:

[`agent-substrate-microvm-class.yaml`](./agent-substrate-microvm-class.yaml):

```yaml
spec:
  nodePoolConfig:
    sandbox:
      type: microvm
    nodeLabels:
      ate.dev/sandbox-runtime: microvm
  priorityDefaults:
    enableNestedVirtualization: true
  priorities:
  # Tier 1: 8-core nested-virt compatible shapes
  - machineType: n4-standard-8
  - machineType: c4-standard-8
  - machineType: c3-standard-8
  - machineType: n4d-standard-8
  - machineType: n2-standard-8
  # Tier 2: 16-core shapes for dense microVM consolidation
  - machineType: n4-standard-16
  - machineType: c4-standard-16
  - machineType: c3-standard-16
  - machineType: n2-standard-16
  # Tier 3: Bounded family catch-all for broad zone elasticity
  - machineFamily: n2
    minCores: 8
    minMemoryGb: 32
  activeMigration:
    optimizeRulePriority: true
  nodePoolAutoCreation:
    enabled: true
  whenUnsatisfiable: DoNotScaleUp
```

### CRD CEL validation rules for microVMs

The ComputeClass OpenAPI schema enforces strict Common Expression Language (CEL) validation
when `sandbox.type: microvm` is requested:

1. **At least one priority rule required**:
   ```cel
   (self.nodePoolConfig.sandbox.type == 'microvm') ? (size(self.priorities) > 0) : true
   ```
2. **Nested virtualization mandatory across all auto-created priorities**:
   ```cel
   (self.nodePoolConfig.sandbox.type == 'microvm') ?
     self.priorities.all(p, has(p.nodepools) || p.enableNestedVirtualization || self.priorityDefaults.enableNestedVirtualization) : true
   ```
   Setting `priorityDefaults.enableNestedVirtualization: true` cleanly satisfies this condition
   for all priority tiers without duplicating the field per rule.

### Machine series compatibility

Nested virtualization requires hardware virtualization extensions (Intel VT-x or AMD-V):

* **Supported families**: General-purpose and compute-optimized Intel series (`n4`, `c4`, `c3`,
  `n2`, `c2`) and select AMD series (`n4d`).
* **Unsupported series**: `e2` (shared-core), Arm-based instances (`c4a`, `t2a`), and
  memory-optimized shapes (`m1`, `m2`, `m3`) do **not** support nested virtualization and
  are rejected by GKE admission control when nested virtualization is enabled.

## Agent Substrate architecture: WorkerPool & ActorTemplate

[`agent-substrate-microvm-pool.yaml`](./agent-substrate-microvm-pool.yaml) defines the
workload execution blueprint for Agent Substrate:

* **`WorkerPool`**: Manages a fleet of pre-warmed worker pods running `ateworker-microvm`.
  Its `nodeSelector` targets `cloud.google.com/compute-class: agent-substrate-microvm` and
  `sandbox.gke.io/runtime: microvm`, with matching tolerations.
* **`ActorTemplate`**: Defines the user-facing agent container specification (e.g. a Python
  interpreter or coding assistant) and binds to the `WorkerPool` via `workerSelector`.

[`agent-substrate-microvm-deploy.yaml`](./agent-substrate-microvm-deploy.yaml) provides a
standard Kubernetes Deployment to test the ComputeClass and node auto-creation independently.

## Install Agent Substrate

See [Agent Substrate on GKE](https://cloud.google.com/blog/products/containers-kubernetes/agent-substrate-available-on-gke)
and the [substrate-gke repository](https://github.com/ai-on-gke/substrate-gke) for full setup
details on GKE Standard (1.37+ with Workload Identity and Dataplane V2 enabled):

```bash
curl -sSL https://raw.githubusercontent.com/ai-on-gke/substrate-gke/main/install.sh | bash
```

## Deploy

```bash
# 1. Apply the ComputeClass
kubectl apply -f agent-substrate-microvm-class.yaml

# 2. Deploy the baseline test Deployment
kubectl apply -f agent-substrate-microvm-deploy.yaml

# 3. If running Agent Substrate on your cluster, apply the WorkerPool and ActorTemplate:
kubectl apply -f agent-substrate-microvm-pool.yaml
```

## Observe

```bash
# Verify auto-created nodes carry microVM runtime and nested virtualization
kubectl get nodes -L cloud.google.com/compute-class,sandbox.gke.io/runtime,node.kubernetes.io/instance-type

# Verify microVM RuntimeClass details
kubectl get runtimeclass microvm -o yaml

# Inspect priority fulfillment
kubectl get nodes -o custom-columns=NAME:.metadata.name,CLASS:.metadata.labels.cloud\.google\.com/compute-class,TYPE:.metadata.labels.node\.kubernetes\.io/instance-type,PRIORITY_IDX:.metadata.annotations.ccc_priority_index

# Inspect the isolated guest VM kernel inside the running microVM pod
POD_NAME=$(kubectl get pod -l app=agent-substrate-microvm-worker -o jsonpath='{.items[0].metadata.name}')
kubectl exec "${POD_NAME}" -- uname -a
```

Inside a `microvm` pod, the container runs under a guest Linux kernel managed by Kata Containers
and Cloud Hypervisor, isolated from the underlying node host kernel.

---

## Interactive demo: GKE Agent Substrate - scale performance control tower (`./scale-demo`)

For a full end-to-end interactive control tower and observability dashboard demonstrating `0` to `2,000` agent surge scaling across dual sandbox runtimes (`microVM (Kata)` and `gVisor (runsc)`), a 5-tier `ComputeClass` (`c3` &rarr; `n4` &rarr; `c4` &rarr; `c2` &rarr; `n2`), per-family `CapacityQuota`s, and hybrid Active + S3-suspended Standby `CapacityBuffer`s on port `8881`, see **[`./scale-demo/README.md`](./scale-demo/README.md)**.

The control tower allows toggling between `microVM (Kata)` and `gVisor (runsc)` live from the UI or via `./run-local.sh --runtime gvisor`, dynamically reconciling `Deployment`s, `WorkerPool`s, and `CapacityBuffer` templates across runtime tiers.

![GKE Agent Substrate - scale performance control tower](./scale-demo/images/screenshot-timeseries.png)

```bash
cd scale-demo
./install.sh
./run-local.sh
# Open http://localhost:8881
```

