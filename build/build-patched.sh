#!/usr/bin/env bash
# Build patched OAI AMF and PCF v2.2.1 images (see ../patches/).
set -uo pipefail
cd "$(dirname "$0")"
while pgrep -f "docker compose build" >/dev/null; do sleep 5; done   # let other builds finish first
NFS="${NFS:-amf pcf}"
for nf in $NFS; do
  start=$(date +%s)
  if docker build --build-arg TARGETPLATFORM=linux/amd64 --target oai-$nf -t testbed/oai-$nf:v2.2.1-fix \
       -f oai-cn5g-$nf/docker/Dockerfile.$nf.testbed oai-cn5g-$nf > build-$nf.log 2>&1; then
    echo "BUILD_OK $nf $(( $(date +%s) - start ))s"
  else
    echo "BUILD_FAILED $nf (see build/build-$nf.log)"; tail -20 build-$nf.log
  fi
done
