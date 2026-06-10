# Arm (Axion) preferred, x86 fallback

Prefers Google **Axion `c4a` (Arm64)** nodes for their price/performance edge on
scale-out, stateless services, and falls back to x86 **`c4`** when Arm capacity is
short. `activeMigration.optimizeRulePriority` pulls pods back onto Arm once `c4a`
capacity returns.

## Two gotchas this example shows

1. **Multi-arch image is mandatory.** Pods can land on `c4a` (arm64) *or* `c4`
   (amd64), so the image must be a multi-arch manifest list. `nginx:1.27` is. An
   amd64-only image will fail on the Arm node — if that's your case, don't use
   this class; pin `amd64` with a `kubernetes.io/arch` nodeAffinity instead.
2. **Arm nodes are tainted.** GKE taints Arm nodes
   `kubernetes.io/arch=arm64:NoSchedule`. The pod must tolerate it (see the
   deploy) or it stays Pending on the Arm priority.

## Deploy & observe

```bash
kubectl apply -f arm-class.yaml
kubectl apply -f arm-deploy.yaml

# ARCH column shows arm64 when scheduled on Axion, amd64 on the fallback:
kubectl get nodes -L cloud.google.com/compute-class,kubernetes.io/arch,node.kubernetes.io/instance-type
kubectl get pods -o wide -l app=arm-web
```
