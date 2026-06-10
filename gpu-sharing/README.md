# GPU sharing — many pods, one physical GPU

Packs four pods onto a single L4 using **GPU time-sharing**, set directly in the
ComputeClass `gpu.gpuSharing` block. A whole L4 is wasted on a small, bursty, or
low-QPS inference pod; time-sharing reclaims that idle silicon.

`maxSharedClientsPerGPU: 4` makes a one-L4 node advertise **4** allocatable
`nvidia.com/gpu`, so four pods that each request `gpu: 1` schedule onto the same
card and the GPU context-switches between them.

## The three sharing strategies

| Strategy | Field | Isolation | Use for |
|----------|-------|-----------|---------|
| Time-sharing | `sharingStrategy: TIME_SHARING` | none (context-switch) | trusted, bursty, low-util inference / dev |
| MPS | `sharingStrategy: MPS` | soft (mem + SM caps) | concurrent steady small workloads |
| MIG | `gpuPartitionSize: 1g.5gb` | hard (HW partition) | strong isolation; A100 / H100 only |

> **No isolation with time-sharing.** Pods share GPU memory with no limit — one
> pod can OOM the card for its neighbors. Use it only for trusted workloads. Need
> isolation? Switch `sharingStrategy` to `MPS`, or set `gpuPartitionSize` for MIG.

## Deploy & observe

```bash
kubectl apply -f gpu-sharing-class.yaml
kubectl apply -f gpu-sharing-deploy.yaml

# One GPU node, advertising 4 shared GPUs, runs all 4 pods:
kubectl get pods -o wide -l app=gpu-shared-worker
kubectl get nodes -L cloud.google.com/compute-class \
  -o custom-columns=NAME:.metadata.name,GPUS:.status.allocatable.'nvidia\.com/gpu'
```

You should see a single node with allocatable `nvidia.com/gpu: 4` hosting all
four replicas.
