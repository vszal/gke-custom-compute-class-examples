# Dynamic Workload Scheduler flex-start — queued capacity for GPU batch & training

Gets scarce accelerators by accepting a **queue wait** instead of competing for
on-demand capacity. Dynamic Workload Scheduler **flex-start** leases the
whole gang of GPUs all-at-once, for a bounded time (up to 7 days), then reclaims
it — capacity you often can't get on-demand becomes reachable.

Set with `flexStart.enabled: true` on a priority rule. This example queues for an
L4 via flex-start, then falls back to Spot if the queue is slow.

## flex-start vs the serving ladder

| | flex-start (this example) | On-Demand serving ([../gpu-accelerator](../gpu-accelerator)) |
|---|---|---|
| Workload | batch / training (a **Job**) | latency-sensitive serving (a Deployment) |
| Provisioning | **queued**, all-or-nothing, time-bounded | immediate, persistent |
| Why | reach scarce capacity; queue wait is OK | a queue wait would break the SLA |

- **Use a Job, not a Deployment** — flex-start capacity is time-bounded and meant
  to be released.
- `capacityCheckWaitTimeSeconds: 1800` caps the wait at this priority before
  falling through to Spot.
- For multi-day runs, uncomment `flexStart.nodeRecycling.leadTimeSeconds` so GKE
  drains a node ahead of its lease end for a clean checkpoint handoff.

## Deploy & observe

```bash
kubectl apply -f flexstart-class.yaml
kubectl apply -f flexstart-job.yaml

# The Job's pod stays Pending while GKE queues for flex-start capacity, then runs:
kubectl get pods -l app=gpu-training -w
kubectl get nodes -L cloud.google.com/compute-class
```
