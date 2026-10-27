# UERANSIM v3.3.0 (gNB + UE emulator) built from source.
FROM ubuntu:24.04 AS build
ARG UERANSIM_VERSION=v3.3.0
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl make gcc g++ cmake libsctp-dev lksctp-tools && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl -sSL https://codeload.github.com/aligungr/UERANSIM/tar.gz/refs/tags/${UERANSIM_VERSION} | tar xz --strip-components=1 \
 && make -j"$(nproc)"

FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libsctp1 iproute2 iputils-ping iperf3 procps netcat-openbsd && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/build/nr-gnb /src/build/nr-ue /src/build/nr-cli /src/build/libdevbnd.so /ueransim/
ENV PATH=/ueransim:$PATH
WORKDIR /ueransim
