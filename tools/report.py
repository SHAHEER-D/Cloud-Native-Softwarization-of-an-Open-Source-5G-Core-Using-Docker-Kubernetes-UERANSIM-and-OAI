#!/usr/bin/env python3
"""Build the Docker Compose vs. Kubernetes comparison from results/{compose,k8s}/.

Writes results/comparison.md (tables used in the paper) and results/comparison.json.
Every value is read from the raw CSV/JSON files written by tools/bench.py.
"""
import csv
import json
import os
import re
import statistics as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "results")


def rows(platform, name):
    path = os.path.join(RES, platform, name)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(st.median(xs), 2) if xs else None


def p95(xs):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, int(0.95 * len(xs)))], 2) if xs else None


def coldstart(platform):
    path = os.path.join(RES, f"{platform}-coldstart.txt")
    if not os.path.exists(path):
        return []
    return [float(x) for x in re.findall(r"(\d+\.\d+)\s*s?", open(path).read())
            if 5 < float(x) < 600]


def collect(platform):
    d = {}
    a = rows(platform, "attach.csv")
    d["reg_ms"] = [num(r["registration_ms"]) for r in a if r["status"] == "ok"]
    d["pdu_ms"] = [num(r["pdu_session_ms"]) for r in a if r["status"] == "ok"]
    d["attach_ok"] = f'{sum(r["status"] == "ok" for r in a)}/{len(a)}'
    lat = rows(platform, "latency.csv")
    d["rtt_ms"] = [num(r["rtt_avg_ms"]) for r in lat]
    d["rtt_loss"] = max((num(r["loss_pct"]) for r in lat), default=None)
    tp = rows(platform, "throughput.csv")
    d["tcp_ul"] = [num(r["achieved_mbps"]) for r in tp if r["test"] == "tcp_ul"]
    d["tcp_dl"] = [num(r["achieved_mbps"]) for r in tp if r["test"] == "tcp_dl"]
    d["udp_loss"] = {}
    for r in tp:
        if r["test"] == "udp_dl":
            d["udp_loss"].setdefault(int(r["offered_mbps"]), []).append(num(r["loss_pct"]))
    d["load"] = rows(platform, "load.csv")
    d["resources"] = rows(platform, "resources.csv")
    d["recovery"] = {}
    for r in rows(platform, "recovery.csv"):
        d["recovery"].setdefault(r["nf"], []).append(num(r["service_restored_s"]))
    d["stale"] = {}
    for r in rows(platform, "stale_sessions.csv"):
        d["stale"].setdefault(int(r["orphaned_sessions"]), []).append(num(r["restore_s"]))
    d["coldstart"] = coldstart(platform)
    return d


def fmt(x, unit=""):
    return "–" if x is None else f"{x:g}{unit}"


def main():
    c, k = collect("compose"), collect("k8s")
    out = ["# Docker Compose vs. Kubernetes (3-node kind) — OAI 5GC v2.2.1", ""]

    out += ["## Control plane, data plane and start-up", "",
            "| Metric | Docker Compose | Kubernetes |", "|---|---|---|"]
    rows_ = [
        ("UE attach success", c["attach_ok"], k["attach_ok"]),
        ("Registration time, median (p95) [ms]",
         f'{fmt(med(c["reg_ms"]))} ({fmt(p95(c["reg_ms"]))})', f'{fmt(med(k["reg_ms"]))} ({fmt(p95(k["reg_ms"]))})'),
        ("PDU session setup, median (p95) [ms]",
         f'{fmt(med(c["pdu_ms"]))} ({fmt(p95(c["pdu_ms"]))})', f'{fmt(med(k["pdu_ms"]))} ({fmt(p95(k["pdu_ms"]))})'),
        ("RTT UE->DN, median of 3x200 pings [ms]", fmt(med(c["rtt_ms"])), fmt(med(k["rtt_ms"]))),
        ("ICMP loss, worst run [%]", fmt(c["rtt_loss"]), fmt(k["rtt_loss"])),
        ("TCP uplink, median of 3 [Mbit/s]", fmt(med(c["tcp_ul"])), fmt(med(k["tcp_ul"]))),
        ("TCP downlink, median of 3 [Mbit/s]", fmt(med(c["tcp_dl"])), fmt(med(k["tcp_dl"]))),
    ]
    for rate in sorted(set(c["udp_loss"]) | set(k["udp_loss"])):
        rows_.append((f"UDP DL loss at {rate} Mbit/s, max of 3 [%]",
                      fmt(max(c["udp_loss"].get(rate, [None]), key=lambda v: v or 0)),
                      fmt(max(k["udp_loss"].get(rate, [None]), key=lambda v: v or 0))))
    rows_.append(("Cold start to all-ready [s]",
                  " / ".join(f"{x:.1f}" for x in c["coldstart"]) or "–",
                  " / ".join(f"{x:.1f}" for x in k["coldstart"]) or "–"))
    out += [f"| {a} | {b} | {cc} |" for a, b, cc in rows_] + [""]

    out += ["## Simultaneous UE attach (UERANSIM)", "",
            "| UEs | Compose: up / time all up [s] / per-UE median [ms] | Kubernetes: up / time all up [s] / per-UE median [ms] |",
            "|---|---|---|"]
    kl = {r["ues"]: r for r in k["load"]}
    for r in c["load"]:
        q = kl.get(r["ues"], {})
        out.append(f'| {r["ues"]} | {r["sessions_up"]} / {r["all_up_s"]} / {r["attach_median_ms"]} | '
                   f'{q.get("sessions_up", "–")} / {q.get("all_up_s", "–")} / {q.get("attach_median_ms", "–")} |')
    out.append("")

    out += ["## Resource use (cgroup v2, 10 s windows)", "",
            "| NF | Compose idle CPU % / MiB | Compose load CPU % | K8s idle CPU % / MiB | K8s load CPU % |",
            "|---|---|---|---|---|"]
    kr = {r["nf"]: r for r in k["resources"]}
    tot = {"c_mem": 0.0, "k_mem": 0.0}
    for r in c["resources"]:
        q = kr.get(r["nf"], {})
        out.append(f'| {r["nf"]} | {r["idle_cpu_pct"]} / {r["idle_mem_mib"]} | {r["load_cpu_pct"]} | '
                   f'{q.get("idle_cpu_pct", "–")} / {q.get("idle_mem_mib", "–")} | {q.get("load_cpu_pct", "–")} |')
        tot["c_mem"] += num(r["idle_mem_mib"]) or 0
        tot["k_mem"] += num(q.get("idle_mem_mib")) or 0
    out.append(f'| **total memory** | {tot["c_mem"]:.0f} MiB | | {tot["k_mem"]:.0f} MiB | |')
    out.append("")

    out += ["## Recovery after an NF crash (SIGKILL; time until a new UE gets a PDU session)", "",
            "| NF | Compose, 3 crashes [s] | Kubernetes, 3 consecutive crashes [s] |", "|---|---|---|"]
    for nf in ("oai-amf", "oai-smf", "oai-upf", "oai-nrf"):
        out.append(f'| {nf} | {" / ".join(fmt(x) for x in c["recovery"].get(nf, []))} | '
                   f'{" / ".join(fmt(x) for x in k["recovery"].get(nf, []))} |')
    out += ["", "Kubernetes values grow with each consecutive crash because of CrashLoopBackOff.", ""]

    out += ["## SMF crash with orphaned UPF sessions (time until a new UE gets a PDU session)", "",
            "| Orphaned sessions | Compose [s] | Kubernetes [s] |", "|---|---|---|"]
    for n in sorted(set(c["stale"]) | set(k["stale"])):
        out.append(f'| {n} | {" / ".join(fmt(x) for x in c["stale"].get(n, []))} | '
                   f'{" / ".join(fmt(x) for x in k["stale"].get(n, []))} |')
    out.append("")

    with open(os.path.join(RES, "comparison.md"), "w") as f:
        f.write("\n".join(out))
    json.dump({"compose": c, "k8s": k}, open(os.path.join(RES, "comparison.json"), "w"), indent=1, default=str)
    print("\n".join(out))


if __name__ == "__main__":
    main()
