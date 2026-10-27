#!/usr/bin/env bash
# Phase II: create the 3-node kind cluster and deploy the OAI 5GC + UERANSIM with Helm.
#   k8s/deploy.sh            (cluster + core + UERANSIM)
#   k8s/deploy.sh teardown   (delete the cluster)
set -euo pipefail
cd "$(dirname "$0")"
ROOT=..
NS=oai

if [[ "${1:-}" == "teardown" ]]; then kind delete cluster --name oai; exit 0; fi

# same configuration files as the Docker Compose deployment
mkdir -p oai-5gc/files/policies
cp $ROOT/core/conf/config.yaml $ROOT/core/conf/nssf_slice_config.yaml \
   $ROOT/core/database/oai_db.sql $ROOT/core/healthscripts/mysql-healthcheck.sh oai-5gc/files/
cp -r $ROOT/core/policies/* oai-5gc/files/policies/

if ! kind get clusters | grep -qx oai; then
  kind create cluster --config kind-cluster.yaml --wait 180s
fi
for n in $(kind get nodes --name oai); do docker exec "$n" modprobe sctp 2>/dev/null || true; done
# Each kind node is one Docker container whose systemd scope gets DefaultTasksMax (15 % of pid_max,
# 3870 here) shared by ALL pods on the node; UERANSIM needs ~36 threads per UE, so 100 UEs fail
# with "Resource temporarily unavailable". Raise it (the Compose UE container gets the same limit).
for n in $(kind get nodes --name oai); do docker update --pids-limit 32768 "$n" >/dev/null; done

# metrics-server for "kubectl top" (resource benchmarks)
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.8.1/components.yaml >/dev/null
kubectl -n kube-system patch deploy metrics-server --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' >/dev/null 2>&1 || true

# images: locally built ones are loaded into the nodes; public ones are pulled by the nodes.
# (kind load of multi-arch public images fails with Docker 29's containerd image store, because
#  only the host platform's layers exist locally while kind imports with --all-platforms.)
for img in testbed/ueransim:v3.3.0 testbed/oai-amf:v2.2.1-fix testbed/oai-pcf:v2.2.1-fix; do
  kind load docker-image --name oai "$img" >/dev/null
done

kubectl create namespace $NS --dry-run=client -o yaml | kubectl apply -f - >/dev/null
start=$(date +%s.%N)
helm upgrade --install oai-5gc oai-5gc -n $NS --wait --timeout 10m
echo "core_ready_seconds=$(echo "$(date +%s.%N) - $start" | bc)"

kubectl -n $NS create configmap ueransim-conf --from-file=$ROOT/ueransim/gnb.yaml \
  --from-file=$ROOT/ueransim/ue.yaml --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n $NS apply -f ueransim/ueransim.yaml >/dev/null
kubectl -n $NS rollout status deploy/ueransim-gnb --timeout=180s
kubectl -n $NS rollout status deploy/ueransim-ue --timeout=180s
kubectl -n $NS get pods -o wide
