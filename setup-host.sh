#!/usr/bin/env bash
# One-time privileged host setup for the OAI 5G testbed.
# Run once:  sudo bash /home/sheri/oai-testbed/setup-host.sh
set -euo pipefail

TARGET_USER="${SUDO_USER:-sheri}"

echo "[1/6] Installing packages (Docker, Compose v2, git, test tools)"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  docker.io docker-compose-v2 git iperf3 jq tcpdump iproute2 iputils-ping \
  net-tools sysstat ethtool curl ca-certificates

echo "[2/6] Enabling Docker and adding $TARGET_USER to the docker group"
systemctl enable --now docker
usermod -aG docker "$TARGET_USER"
mkdir -p /etc/docker
if [ ! -f /etc/docker/daemon.json ]; then
  cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "3" }
}
EOF
  systemctl restart docker
fi

echo "[3/6] Loading SCTP (N2) and GTP kernel modules, persistent across reboots"
modprobe sctp
modprobe gtp || true
printf "sctp\n" > /etc/modules-load.d/oai-5g.conf

echo "[4/6] Kernel settings: IP forwarding (UPF/N6) and inotify limits (multi-node kind)"
cat > /etc/sysctl.d/90-oai-5g.conf <<'EOF'
net.ipv4.ip_forward = 1
net.ipv4.conf.all.forwarding = 1
fs.inotify.max_user_instances = 512
fs.inotify.max_user_watches = 524288
vm.swappiness = 60
EOF
sysctl --system >/dev/null

echo "[5/6] Adding an extra 8 GiB swap file (host has only ~5 GiB RAM)"
if ! swapon --show=NAME --noheadings | grep -q '^/swap-oai.img$'; then
  if [ ! -f /swap-oai.img ]; then
    fallocate -l 8G /swap-oai.img
    chmod 600 /swap-oai.img
    mkswap /swap-oai.img >/dev/null
  fi
  swapon /swap-oai.img
  grep -q '^/swap-oai.img' /etc/fstab || echo '/swap-oai.img none swap sw 0 0' >> /etc/fstab
fi

echo "[6/6] Done. Summary:"
docker --version
docker compose version
git --version
lsmod | grep -E '^sctp' || true
sysctl net.ipv4.ip_forward
swapon --show
echo
echo "Log out and back in (or reboot) so '$TARGET_USER' can use docker without sudo."
