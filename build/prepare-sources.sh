#!/usr/bin/env bash
# Fetch OAI AMF/PCF v2.2.1 sources, apply the testbed patches and create the build Dockerfiles.
# Then run build-patched.sh to build testbed/oai-{amf,pcf}:v2.2.1-fix.
set -euo pipefail
cd "$(dirname "$0")"
PATCHES=../patches

for nf in amf pcf; do
  if [ ! -d oai-cn5g-$nf ]; then
    git -c advice.detachedHead=false clone -q --depth 1 --branch v2.2.1 --recurse-submodules \
      --shallow-submodules https://github.com/OPENAIRINTERFACE/oai-cn5g-$nf.git
  fi
  # build-parallelism cap: the build scripts call make -j$(nproc); 4 jobs fit in 5 GiB RAM
  f=oai-cn5g-$nf/docker/Dockerfile.$nf.ubuntu
  awk -v nf=$nf '{print} $0 ~ "^FROM \\$BASE_IMAGE AS oai-"nf"-base" {
    print "# testbed: cap build parallelism (host has 5 GB RAM); build scripts call make -j$(nproc)";
    print "RUN printf \x27#!/bin/sh\\necho 4\\n\x27 > /usr/local/bin/nproc && chmod +x /usr/local/bin/nproc"}' \
    $f > oai-cn5g-$nf/docker/Dockerfile.$nf.testbed
done

git -C oai-cn5g-amf apply --check ../$PATCHES/oai-amf-v2.2.1-nssf-nrf-registration-uri.patch 2>/dev/null &&
  git -C oai-cn5g-amf apply ../$PATCHES/oai-amf-v2.2.1-nssf-nrf-registration-uri.patch
git -C oai-cn5g-pcf apply --check ../$PATCHES/oai-pcf-v2.2.1-nrf-heartbeat.patch 2>/dev/null &&
  git -C oai-cn5g-pcf apply ../$PATCHES/oai-pcf-v2.2.1-nrf-heartbeat.patch
git -C oai-cn5g-pcf/build/common-build apply --check ../../../$PATCHES/oai-common-build-odb-build2-pins.patch 2>/dev/null &&
  git -C oai-cn5g-pcf/build/common-build apply ../../../$PATCHES/oai-common-build-odb-build2-pins.patch
echo "sources ready; next: ./build-patched.sh"
