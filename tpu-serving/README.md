# TPU slice from a ComputeClass

Provisions a **single-host TPU v6e (Trillium)** slice through node auto-creation.
A priority's `tpu` block works just like a `gpu` block — `type` + `topology` +
`count` define the slice:

```yaml
tpu:
  type: tpu-v6e-slice   # Trillium; tpu-v5-lite-podslice for v5e, tpu-v5p-slice for v5p
  count: 8              # chips in the slice
  topology: 2x4         # single-host (8 chips on one node)
```

`2x4` v6e is the simplest TPU shape to run: 8 chips on **one** host, so no
multi-host coordination (JobSet / leader pod) is needed. The compute-class
`nodeSelector` replaces the usual `gke-tpu-accelerator` / `gke-tpu-topology`
labels — GKE derives the slice from the class. The pod requests the chips via the
`google.com/tpu` resource.

> **TPU capacity is usually reserved.** On-demand TPU is scarce — consume a
> reservation in production (uncomment the top priority) and keep Spot as the
> fallback for preemptible/batch work.

## Deploy & observe

```bash
kubectl apply -f tpu-class.yaml
kubectl apply -f tpu-deploy.yaml

kubectl get pods -o wide -l app=tpu-workload
kubectl get nodes -L cloud.google.com/compute-class,cloud.google.com/gke-tpu-accelerator,cloud.google.com/gke-tpu-topology
```

For larger models, scale to a **multi-host** slice (e.g. v6e `4x4`) — that needs a
leader/worker `JobSet` so the pods coordinate across hosts; this example stays
single-host on purpose.
