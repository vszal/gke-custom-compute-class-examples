# vLLM Gemma inference using a custom compute class

Serves the small `google/gemma-4-E2B-it` model with vLLM on a GPU node that GKE
provisions from a custom ComputeClass instead of the tutorial's accelerator
`nodeSelector`. The `gpu-inference` class follows the recommended inference
ladder (On-Demand primary, Spot as last resort) and hosts one NVIDIA RTX PRO
6000 per `g4-standard-48` node.

> **Check regional availability first.** `g4` (NVIDIA RTX PRO 6000) is not offered in every
> region. GKE does **not** validate machine types at admission, so in a region
> without it this class applies cleanly and then never provisions a node — every
> priority here is `g4`, so no fallback catches the gap. Verify with
> `gcloud compute machine-types list --filter="name~^g4- AND zone~^REGION-"`.

1. Follow the general instructions in the [Google Cloud vLLM Gemma tutorial](https://cloud.google.com/kubernetes-engine/docs/tutorials/serve-gemma-gpu-vllm) through the model-access/credentials section (stop before deploying vLLM)

2. Deploy the custom compute class:

```bash
kubectl apply -f accelerator-class.yaml
```

3. On GKE Standard, you may need to [manually install the NVIDIA device drivers](https://cloud.google.com/kubernetes-engine/docs/how-to/gpus#installing_drivers) on your cluster

4. Deploy `gemma-deploy.yaml` instead of the manifest in the tutorial's Deploy section:

```bash
kubectl apply -f gemma-deploy.yaml
```

5. Continue with the rest of the tutorial (serving, testing the endpoint)

The only change from the tutorial manifest is the `nodeSelector` — it targets
`cloud.google.com/compute-class: gpu-inference` plus the required
`nvidia.com/gpu` toleration, rather than a `cloud.google.com/gke-accelerator`
label. `cuda-deploy.yaml` is a minimal GPU smoke-test Pod against the same class.
