## CONFIGURE THESE TWO VARIABLES BEFORE RUNNING
CLUSTER_NAME=
LOCATION=

if [[ -z "${CLUSTER_NAME}" || -z "${LOCATION}"  ]]; then
  echo "The value of CLUSTER_NAME and/or LOCATION variable is not set."
  else
    echo "Updating node pools... "

gcloud container node-pools update e2-4-spot-pool \
    --location=${LOCATION} \
    --cluster=${CLUSTER_NAME} \
    --enable-autoscaling \
    --max-nodes=10