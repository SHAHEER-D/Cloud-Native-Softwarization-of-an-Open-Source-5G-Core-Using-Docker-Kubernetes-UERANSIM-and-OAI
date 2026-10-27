#!/usr/bin/env bash
# Extended-operation (soak) test of the containerized OAI gNB + NR-UE (RF simulator).
#   tools/soak_rfsim.sh [duration_s=1800] [out_dir=results/rfsim-soak]
# Continuous 1 Hz ping through the full RAN + core path, a 10 s iperf3 DL run every 5 min,
# and gNB/UE container state (status, restart count, memory) every 30 s.
set -uo pipefail
DUR=${1:-1800}
OUT=${2:-$(dirname "$0")/../results/rfsim-soak}
DN=192.168.70.135
mkdir -p "$OUT"
docker exec oai-ext-dn sh -c 'pkill iperf3; iperf3 -s -D' >/dev/null 2>&1

state() {
  for c in oai-gnb oai-nr-ue; do
    s=$(docker inspect -f '{{.State.Status}} {{.RestartCount}} {{.State.StartedAt}}' $c 2>/dev/null || echo "missing - -")
    m=$(docker stats --no-stream --format '{{.MemUsage}} {{.CPUPerc}}' $c 2>/dev/null | awk '{print $1, $4}')
    echo "$(date +%s) $c $s $m"
  done
}

start=$(date +%s)
echo "start $(date -Is) duration ${DUR}s" > "$OUT/info.txt"
docker exec oai-nr-ue ping -I oaitun_ue1 -D -i 1 -w "$DUR" "$DN" > "$OUT/ping.log" 2>&1 &
PING=$!
: > "$OUT/state.log"; : > "$OUT/iperf.log"
next_iperf=$((start + 60))
while [ $(( $(date +%s) - start )) -lt "$DUR" ]; do
  state >> "$OUT/state.log"
  if [ "$(date +%s)" -ge "$next_iperf" ]; then
    docker exec oai-nr-ue iperf3 -B 12.1.1.2 -c $DN -t 10 -R -J 2>/dev/null \
      | python3 -c 'import json,sys,time; j=json.load(sys.stdin); print(int(time.time()), round(j["end"]["sum_received"]["bits_per_second"]/1e6,1))' \
      >> "$OUT/iperf.log" 2>/dev/null || echo "$(date +%s) FAILED" >> "$OUT/iperf.log"
    next_iperf=$((next_iperf + 300))
  fi
  sleep 30
done
wait $PING
state >> "$OUT/state.log"

# summary
python3 - "$OUT" <<'EOF'
import re, sys, json, statistics as st
out = sys.argv[1]
ping = open(f"{out}/ping.log").read()
rtts = [float(x) for x in re.findall(r"time=([\d.]+) ms", ping)]
tx = re.search(r"(\d+) packets transmitted, (\d+) received", ping)
gaps = []
seqs = [int(s) for s in re.findall(r"icmp_seq=(\d+)", ping)]
for a, b in zip(seqs, seqs[1:]):
    if b - a > 1:
        gaps.append(b - a - 1)
states = [l.split() for l in open(f"{out}/state.log")]
restarts = {c: max(int(s[3]) for s in states if s[1] == c and s[3].isdigit()) for c in ("oai-gnb", "oai-nr-ue")}
alive = all(s[2] == "running" for s in states)
ip = [l.split() for l in open(f"{out}/iperf.log")]
res = {
    "packets_tx": int(tx.group(1)) if tx else None, "packets_rx": int(tx.group(2)) if tx else None,
    "loss_pct": round(100 * (1 - int(tx.group(2)) / int(tx.group(1))), 3) if tx else None,
    "rtt_ms": {"mean": round(st.mean(rtts), 2), "median": round(st.median(rtts), 2),
               "p95": round(sorted(rtts)[int(0.95 * len(rtts))], 2), "max": max(rtts)} if rtts else None,
    "longest_outage_s": max(gaps) if gaps else 0,
    "containers_running_throughout": alive, "restart_counts": restarts,
    "iperf_dl_mbps": [float(x[1]) for x in ip if x[1] != "FAILED"],
    "iperf_failures": sum(1 for x in ip if x[1] == "FAILED"),
}
json.dump(res, open(f"{out}/summary.json", "w"), indent=1)
print(json.dumps(res, indent=1))
EOF
