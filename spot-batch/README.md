# Spot-primary for fault-tolerant batch

The deliberate **inverse** of the serving rule in
[../gpu-accelerator](../gpu-accelerator): there, Spot must never be primary
because a preemption breaks a latency SLA. For fault-tolerant batch the opposite
holds — Spot is ~60-90% cheaper and a preemption just reschedules work, so **Spot
is priority 1** with On-Demand as a guarantee-of-progress fallback.

## When Spot-primary is correct

- Stateless or checkpointing workers; work reschedules without data loss
- Replica-redundant (losing some replicas degrades, doesn't break)
- Throughput matters more than per-pod latency

## Designing for preemption

- Spot sends **SIGTERM ~25s before reclamation** — `terminationGracePeriodSeconds:
  25` plus a SIGTERM trap lets a worker checkpoint and exit cleanly.
- A **PodDisruptionBudget** (`minAvailable: 4` of 6) bounds voluntary disruptions.
- `activeMigration.optimizeRulePriority` moves pods off the On-Demand fallback
  back onto Spot once cheap capacity returns.

## Deploy & observe

```bash
kubectl apply -f spot-batch-class.yaml
kubectl apply -f spot-batch-deploy.yaml

# SPOT=true while on the primary; flips to the On-Demand fallback under Spot starvation:
kubectl get nodes -L cloud.google.com/compute-class,cloud.google.com/gke-spot
kubectl get pdb batch-workers-pdb
```
