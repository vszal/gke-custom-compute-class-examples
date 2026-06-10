#!/bin/bash

## CONFIGURE THESE TWO VARIABLES BEFORE RUNNING
CLUSTER_NAME=
LOCATION=

echo 'Node pools'
gcloud container node-pools list --cluster ${CLUSTER_NAME} --location ${LOCATION}
echo
echo 'Nodes'
kubectl get nodes -o=custom-columns=NAME:.metadata.name,INSTANCE-TYPE:".metadata.labels.node\.kubernetes\.io/instance-type",SPOT:".metadata.labels.cloud\.google\.com/gke-spot" 
echo
echo 'Pods'
kubectl get pods -o=wide