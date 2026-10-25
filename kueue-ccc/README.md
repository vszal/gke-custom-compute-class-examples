# Using Kueue in combination with ComputeClasses

> **Check regional availability first.** `g2` (NVIDIA L4) is not offered in every
> region — `southamerica-east1` has none, for example. GKE does **not** validate machine types at admission, so in a region
> without it this class applies cleanly and then never provisions a node — every
> priority here is `g2`, so no fallback catches the gap. Verify with
> `gcloud compute machine-types list --filter="name~^g2- AND zone~^REGION-"`.

> **Class name.** The class here is `kueue-cost-optimized` (the tutorial calls it
> `cost-optimized`), so it doesn't collide with the `cost-optimized` class in
> [`static-node-pools`](../static-node-pools).

The files in this repo are to be used in conjunction with the tutorial [Practical Guide to Kueue and Custom Compute Classes](https://medium.com/google-cloud/practical-guide-to-kueue-and-custom-compute-classes-85a3fe287487) 