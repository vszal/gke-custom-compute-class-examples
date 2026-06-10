## CONFIGURE THESE TWO VARIABLES BEFORE RUNNING
CLUSTER_NAME=
LOCATION=

if [[ -z "${CLUSTER_NAME}" || -z "${LOCATION}"  ]]; then
  echo "The value of CLUSTER_NAME and/or LOCATION variable is not set."
  else
    echo "Creating node pools... "
    
    # Create e2-standard-4 spot pool
    gcloud container node-pools create e2-4-spot-pool \
        --location=${LOCATION} \
        --cluster=${CLUSTER_NAME} \
        --machine-type=e2-standard-4 \
        --spot \
        --enable-autoscaling \
        --max-nodes=1 \
        --node-labels="cloud.google.com/compute-class=cost-optimized" \
        --node-taints="cloud.google.com/compute-class=cost-optimized:NoSchedule"

    # Create n2-standard-4 spot pool
    gcloud container node-pools create n2-4-spot-pool \
        --location=${LOCATION} \
        --cluster=${CLUSTER_NAME} \
        --machine-type=n2-standard-4 \
        --spot \
        --enable-autoscaling \
        --max-nodes=2 \
        --node-labels="cloud.google.com/compute-class=cost-optimized" \
        --node-taints="cloud.google.com/compute-class=cost-optimized:NoSchedule"

    # Create n2d-standard-4 spot pool
    gcloud container node-pools create n2d-4-spot-pool \
        --location=${LOCATION} \
        --cluster=${CLUSTER_NAME} \
        --machine-type=n2d-standard-4 \
        --spot \
        --enable-autoscaling \
        --max-nodes=5 \
        --node-labels="cloud.google.com/compute-class=cost-optimized" \
        --node-taints="cloud.google.com/compute-class=cost-optimized:NoSchedule"

    fi