#!/usr/bin/env python3
"""Benchmark the OAI 5GC on Docker Compose or Kubernetes with UERANSIM.

Measures (same procedure on both platforms):
  attach    registration time and PDU-session setup time of one UE, repeated
  load      N UEs registering at once: completion time, success rate, AMF/SMF CPU peak
  latency   ICMP RTT UE -> data network through N3/UPF/N6
  tput      iperf3 TCP UL/DL and UDP DL at fixed rates (loss, jitter)
  resources CPU and memory of every core NF, idle and under iperf3 load
  recovery  crash an NF (kill its PID 1) and time until it is ready and a UE attaches again

Usage:  bench.py compose|k8s [test ...] [--reps N] [--out DIR]
Results are written as CSV + JSON under results/<platform>/.
"""
import argparse
import csv
import json
import os
import re
import statistics as st
import subprocess
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_NFS = ["oai-nrf", "oai-amf", "oai-smf", "oai-upf", "oai-ausf", "oai-udm", "oai-udr",
            "oai-pcf", "oai-nssf", "mysql"]


def sh(cmd, check=True, timeout=600):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"command failed ({r.returncode}): {cmd}\n{r.stdout}\n{r.stderr}")
    return r.stdout


class Platform:
    def stats(self, window=1.0):
        """{nf: (cpu_percent_of_one_core, mem_MiB)} from cgroup v2 counters over `window` seconds.
        Same method on both platforms: cpu.stat usage_usec delta / wall time, and memory.current
        minus inactive_file (as `docker stats` reports)."""
        def snap():
            out = {}
            for nf, raw in self._read_cgroups().items():
                usage = int(re.search(r"usage_usec (\d+)", raw).group(1))
                cur = int(re.search(r"^MEMCUR (\d+)", raw, re.M).group(1))
                inact = int((re.search(r"^inactive_file (\d+)", raw, re.M) or [0, 0])[1])
                out[nf] = (usage, (cur - inact) / 2 ** 20)
            return out, time.time()
        a, ta = snap()
        time.sleep(window)
        b, tb = snap()
        return {nf: (round(100 * (b[nf][0] - a[nf][0]) / ((tb - ta) * 1e6), 2), round(b[nf][1], 1))
                for nf in b if nf in a}

    def deregister_ues(self):
        """Gracefully deregister every UERANSIM UE so the SMF releases its N4 sessions at the UPF."""
        out = self.exec_ue("nr-cli --dump", check=False)
        imsis = re.findall(r"imsi-\d{15}", out)
        for imsi in imsis:
            self.exec_ue(f"nr-cli {imsi} --exec 'deregister switch-off'", check=False)
        time.sleep(2 + 0.05 * len(imsis))
        return len(imsis)


# ------------------------------------------------------------------ platform adapters
class Compose(Platform):
    name = "compose"

    def __init__(self):
        self.ue_compose = f"docker compose -f {ROOT}/ueransim/docker-compose.yaml"

    dn_ip = "192.168.70.135"

    def exec_ue(self, cmd, **kw):
        return sh(f"docker exec ueransim-ue {cmd}", **kw)

    def exec_dn(self, cmd, **kw):
        return sh(f"docker exec oai-ext-dn {cmd}", **kw)

    def ue_logs(self):
        return sh("docker logs ueransim-ue 2>&1")

    def start_ues(self, n, graceful=True):
        self.stop_ues(graceful)
        sh(f"NUM_UE={n} {self.ue_compose} up -d ueransim-ue")

    def stop_ues(self, graceful=True):
        running = sh("docker ps -q -f name=^ueransim-ue$", check=False).strip()
        if running and graceful:
            self.deregister_ues()
        sh(f"{self.ue_compose} rm -sf ueransim-ue", check=False)

    def restart_nf(self, nf):
        sh(f"docker restart {nf}")
        return wait(lambda: self.ready(nf) and "healthy" in sh(
            f"docker inspect -f '{{{{.State.Health.Status}}}}' {nf}", check=False), 120)

    def ensure_gnb(self):
        sh(f"{self.ue_compose} up -d ueransim-gnb")

    def restart_gnb(self):
        sh(f"{self.ue_compose} up -d --force-recreate ueransim-gnb")
        return wait(lambda: "NG Setup procedure is successful" in sh("docker logs ueransim-gnb 2>&1"), 60)

    def crash(self, nf):
        # SIGKILL the NF from outside its PID namespace (a signal sent to PID 1 from inside the
        # container is ignored); Docker sees an unexpected exit and applies the restart policy
        pid = sh(f"docker inspect -f '{{{{.State.Pid}}}}' {nf}").strip()
        sh(f"docker run --rm --pid=host --security-opt apparmor=unconfined busybox:1.36 kill -9 {pid}",
           check=False)   # unconfined: AppArmor blocks signals to privileged (unconfined) NFs

    def ready(self, nf):
        out = sh(f"docker inspect -f '{{{{.State.Status}}}} {{{{if .State.Health}}}}{{{{.State.Health.Status}}}}{{{{end}}}}' {nf}", check=False)
        return out.strip() in ("running healthy", "running")

    def restart_count(self, nf):
        return int(sh(f"docker inspect -f '{{{{.RestartCount}}}}' {nf}").strip() or 0)

    def _read_cgroups(self):
        ids = sh("docker inspect -f '{{.Name}} {{.Id}}' " + " ".join(CORE_NFS), check=False)
        cmd = []
        for line in ids.split("\n"):
            if not line.strip():
                continue
            name, cid = line.strip().lstrip("/").split()
            d = f"/sys/fs/cgroup/system.slice/docker-{cid}.scope"
            cmd.append(f"echo '@@{name}'; cat {d}/cpu.stat; echo MEMCUR $(cat {d}/memory.current); cat {d}/memory.stat")
        return _split_cgroup_dump(sh("; ".join(cmd), check=False))


class K8s(Platform):
    name = "k8s"
    ns = "oai"

    def _pod(self, app):
        return sh(f"kubectl -n {self.ns} get pod -l app.kubernetes.io/name={app} "
                  "-o jsonpath='{.items[0].metadata.name}'").strip()

    @property
    def dn_ip(self):
        return sh(f"kubectl -n {self.ns} get pod -l app.kubernetes.io/name=oai-traffic-server "
                  "-o jsonpath='{.items[0].status.podIP}'").strip()

    def exec_ue(self, cmd, **kw):
        return sh(f"kubectl -n {self.ns} exec deploy/ueransim-ue -- {cmd}", **kw)

    def exec_dn(self, cmd, **kw):
        return sh(f"kubectl -n {self.ns} exec deploy/oai-traffic-server -- {cmd}", **kw)

    def ue_logs(self):
        return sh(f"kubectl -n {self.ns} logs deploy/ueransim-ue")

    def start_ues(self, n, graceful=True):
        self.stop_ues(graceful)
        sh(f"kubectl -n {self.ns} set env deploy/ueransim-ue NUM_UE={n}")
        sh(f"kubectl -n {self.ns} scale deploy/ueransim-ue --replicas=1")
        sh(f"kubectl -n {self.ns} rollout status deploy/ueransim-ue --timeout=180s")

    def stop_ues(self, graceful=True):
        running = sh(f"kubectl -n {self.ns} get pod -l app.kubernetes.io/name=ueransim-ue "
                     "--field-selector=status.phase=Running -o name", check=False).strip()
        if running and graceful:
            self.deregister_ues()
        sh(f"kubectl -n {self.ns} scale deploy/ueransim-ue --replicas=0", check=False)
        sh(f"kubectl -n {self.ns} wait --for=delete pod -l app.kubernetes.io/name=ueransim-ue "
           "--timeout=120s", check=False)

    def restart_nf(self, nf):
        sh(f"kubectl -n {self.ns} rollout restart deploy/{nf}")
        sh(f"kubectl -n {self.ns} rollout status deploy/{nf} --timeout=180s")
        return wait(lambda: self.ready(nf), 120)

    def ensure_gnb(self):
        sh(f"kubectl -n {self.ns} rollout status deploy/ueransim-gnb --timeout=180s")

    def restart_gnb(self):
        sh(f"kubectl -n {self.ns} rollout restart deploy/ueransim-gnb")
        sh(f"kubectl -n {self.ns} rollout status deploy/ueransim-gnb --timeout=180s")
        return wait(lambda: "NG Setup procedure is successful"
                    in sh(f"kubectl -n {self.ns} logs deploy/ueransim-gnb", check=False), 60)

    def crash(self, nf):
        # same crash as on Compose: SIGKILL the NF process from its kind node; the kubelet
        # restarts the container (restartPolicy Always)
        pod = self._pod(nf)
        node = sh(f"kubectl -n {self.ns} get pod {pod} -o jsonpath='{{.spec.nodeName}}'").strip()
        cid = sh(f"kubectl -n {self.ns} get pod {pod} "
                 "-o jsonpath='{.status.containerStatuses[0].containerID}'").strip().split("//")[-1]
        pid = sh(f"docker exec {node} crictl inspect -o go-template --template '{{{{.info.pid}}}}' {cid}").strip()
        sh(f"docker exec {node} kill -9 {pid}", check=False)

    def ready(self, nf):
        out = sh(f"kubectl -n {self.ns} get pod -l app.kubernetes.io/name={nf} "
                 "-o jsonpath='{.items[0].status.containerStatuses[0].ready}'", check=False)
        return out.strip() == "true"

    def restart_count(self, nf):
        out = sh(f"kubectl -n {self.ns} get pod -l app.kubernetes.io/name={nf} "
                 "-o jsonpath='{.items[0].status.containerStatuses[0].restartCount}'")
        return int(out.strip() or 0)

    def _read_cgroups(self):
        pods = json.loads(sh(f"kubectl -n {self.ns} get pods -o json"))["items"]
        per_node = {}
        for pod in pods:
            app = pod["metadata"].get("labels", {}).get("app.kubernetes.io/name", "")
            if app not in CORE_NFS or not pod["status"].get("containerStatuses"):
                continue
            cid = pod["status"]["containerStatuses"][0].get("containerID", "").split("//")[-1]
            if cid:
                per_node.setdefault(pod["spec"]["nodeName"], []).append((app, cid))
        dump = ""
        for node, items in per_node.items():
            cmd = "; ".join(
                f"d=$(find /sys/fs/cgroup/kubelet.slice -name 'cri-containerd-{cid}.scope' | head -1); "
                f"echo '@@{app}'; cat $d/cpu.stat; echo MEMCUR $(cat $d/memory.current); cat $d/memory.stat"
                for app, cid in items)
            # pipe the script into the node's shell so $d / $(find) expand inside the node
            dump += subprocess.run(["docker", "exec", "-i", node, "sh"], input=cmd, text=True,
                                   capture_output=True, timeout=60).stdout
        return _split_cgroup_dump(dump)


def _split_cgroup_dump(dump):
    out = {}
    for block in dump.split("@@")[1:]:
        name, _, rest = block.partition("\n")
        out[name.strip()] = rest
    return out


def to_mib(s):
    m = re.match(r"([\d.]+)\s*([KMG]i?B?)", s)
    if not m:
        return 0.0
    v, u = float(m.group(1)), m.group(2)[0]
    return v / 1024 if u == "K" else v * 1024 if u == "G" else v


# ------------------------------------------------------------------ log parsing
TS = r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]"


def ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f").timestamp()


def parse_attach(log):
    """Per-UE timestamps -> list of (reg_ms, pdu_ms). Works for -n N (lines tagged with SUPI)."""
    # With many UEs, UERANSIM threads can interleave two records on one output line, so events
    # are matched anywhere in the log rather than at line starts.
    events = {"Sending Initial Registration": "start", "Initial Registration is successful": "reg",
              "PDU Session establishment is successful": "pdu"}
    pat = re.compile(TS + r" \[(?:(\d{15})\|)?nas\] \[\w+\] (" + "|".join(events) + ")")
    ues = {}
    for m in pat.finditer(log):
        d = ues.setdefault(m.group(2) or "single", {})
        d.setdefault(events[m.group(3)], ts(m.group(1)))
    return ues


def summary(xs):
    if not xs:
        return {"n": 0}
    xs = sorted(xs)
    return {"n": len(xs), "mean": round(st.mean(xs), 2), "median": round(st.median(xs), 2),
            "stdev": round(st.stdev(xs), 2) if len(xs) > 1 else 0.0, "min": round(xs[0], 2),
            "p95": round(xs[min(len(xs) - 1, int(0.95 * len(xs)))], 2), "max": round(xs[-1], 2)}


def wait(pred, timeout, step=0.2):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return time.time() - t0
        time.sleep(step)
    return None


def ue_tun_count(p):
    out = p.exec_ue("ip -o -4 addr show", check=False)
    return len(set(re.findall(r"uesimtun\d+", out)))   # each name appears twice per line


# ------------------------------------------------------------------ tests
def t_attach(p, reps, w):
    regs, pdus = [], []
    rows = []
    for i in range(reps):
        p.start_ues(1)
        if wait(lambda: ue_tun_count(p) >= 1, 60) is None:
            rows.append([i, "", "", "timeout"])
            continue
        d = next(iter(parse_attach(p.ue_logs()).values()), {})
        if {"start", "reg", "pdu"} <= d.keys():
            r, q = (d["reg"] - d["start"]) * 1000, (d["pdu"] - d["reg"]) * 1000
            regs.append(r), pdus.append(q)
            rows.append([i, round(r, 1), round(q, 1), "ok"])
        time.sleep(1)
    w.csv("attach.csv", ["rep", "registration_ms", "pdu_session_ms", "status"], rows)
    return {"registration_ms": summary(regs), "pdu_session_ms": summary(pdus)}


def t_load(p, reps, w, sizes=(1, 10, 25, 50, 100)):
    res, rows = {}, []
    for n in sizes:
        p.stop_ues()
        time.sleep(3)
        p.start_ues(n)
        t0 = time.time()
        peak = {"oai-amf": 0.0, "oai-smf": 0.0}
        done = None
        while time.time() - t0 < 30 + 3 * n:
            for nf, (cpu, _) in p.stats().items():
                if nf in peak:
                    peak[nf] = max(peak[nf], cpu)
            if ue_tun_count(p) >= n:
                done = time.time() - t0
                break
        up = ue_tun_count(p)              # ground truth: PDU-session TUN interfaces
        ues = parse_attach(p.ue_logs())   # timing only
        ok = [d for d in ues.values() if "pdu" in d]
        span = (max(d["pdu"] for d in ok) - min(d["start"] for d in ok if "start" in d)) if ok else None
        e2e = [(d["pdu"] - d["start"]) * 1000 for d in ok if "start" in d]
        row = {"ues": n, "sessions_up": up, "success_rate": round(up / n, 3), "timed_ues": len(e2e),
               "all_up_s": round(span, 2) if span else None,
               "per_ue_attach_ms": summary(e2e), "amf_cpu_peak": peak["oai-amf"],
               "smf_cpu_peak": peak["oai-smf"], "timed_out": done is None}
        res[n] = row
        rows.append([n, up, row["success_rate"], len(e2e), row["all_up_s"], row["per_ue_attach_ms"].get("median"),
                     row["per_ue_attach_ms"].get("p95"), peak["oai-amf"], peak["oai-smf"]])
    w.csv("load.csv", ["ues", "sessions_up", "success_rate", "timed_ues", "all_up_s", "attach_median_ms",
                       "attach_p95_ms", "amf_cpu_peak_pct", "smf_cpu_peak_pct"], rows)
    p.start_ues(1)
    wait(lambda: ue_tun_count(p) >= 1, 60)
    return res


def t_latency(p, reps, w, count=200):
    rows, res = [], []
    for i in range(reps):
        out = p.exec_ue(f"ping -I uesimtun0 -c {count} -i 0.05 -q {p.dn_ip}")
        loss = float(re.search(r"([\d.]+)% packet loss", out).group(1))
        mn, avg, mx, md = map(float, re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", out).groups())
        rows.append([i, count, loss, mn, avg, mx, md])
        res.append(avg)
    w.csv("latency.csv", ["rep", "packets", "loss_pct", "rtt_min_ms", "rtt_avg_ms", "rtt_max_ms",
                          "rtt_mdev_ms"], rows)
    return {"rtt_avg_ms": summary(res), "loss_pct_max": max(r[2] for r in rows)}


def t_tput(p, reps, w, dur=10):
    p.exec_dn("sh -c 'pkill iperf3; iperf3 -s -D'", check=False)
    time.sleep(1)
    ue_ip = re.search(r"inet ([\d.]+)/", p.exec_ue("ip -4 addr show uesimtun0")).group(1)
    base = f"iperf3 -B {ue_ip} -c {p.dn_ip} -t {dur} -J"
    rows, res = [], {}
    for i in range(reps):
        for label, extra in (("tcp_ul", ""), ("tcp_dl", "-R")):
            j = json.loads(p.exec_ue(f"{base} {extra}"))
            mbps = j["end"]["sum_received"]["bits_per_second"] / 1e6
            retr = j["end"]["sum_sent"].get("retransmits", 0)
            rows.append([i, label, "", round(mbps, 1), "", "", retr])
            res.setdefault(label, []).append(mbps)
            time.sleep(2)
        for rate in (50, 100, 200, 300):
            j = json.loads(p.exec_ue(f"{base} -u -b {rate}M -R"))
            s = j["end"]["sum"]
            rows.append([i, "udp_dl", rate, round(s["bits_per_second"] / 1e6, 1),
                         round(s["lost_percent"], 3), round(s["jitter_ms"], 3), ""])
            res.setdefault(f"udp_dl_{rate}_loss", []).append(s["lost_percent"])
            time.sleep(2)
    w.csv("throughput.csv", ["rep", "test", "offered_mbps", "achieved_mbps", "loss_pct", "jitter_ms",
                             "tcp_retransmits"], rows)
    return {k: summary(v) for k, v in res.items()}


def t_resources(p, reps, w):
    def sample():
        return p.stats(window=10.0)

    idle = sample()
    p.exec_dn("sh -c 'pkill iperf3; iperf3 -s -D'", check=False)
    ue_ip = re.search(r"inet ([\d.]+)/", p.exec_ue("ip -4 addr show uesimtun0")).group(1)
    proc = subprocess.Popen(
        ("docker exec ueransim-ue " if p.name == "compose" else f"kubectl -n {K8s.ns} exec deploy/ueransim-ue -- ")
        + f"iperf3 -B {ue_ip} -c {p.dn_ip} -t 15 -R", shell=True, stdout=subprocess.DEVNULL)
    time.sleep(4)
    load = sample()
    proc.wait()
    rows = [[nf, *idle.get(nf, ("", "")), *load.get(nf, ("", ""))] for nf in CORE_NFS]
    w.csv("resources.csv", ["nf", "idle_cpu_pct", "idle_mem_mib", "load_cpu_pct", "load_mem_mib"], rows)
    tot = lambda d, k: round(sum(v[k] for v in d.values()), 1)
    return {"idle_total_cpu_pct": tot(idle, 0), "idle_total_mem_mib": tot(idle, 1),
            "load_total_cpu_pct": tot(load, 0), "load_total_mem_mib": tot(load, 1)}


def t_recovery(p, reps, w, nfs=("oai-amf", "oai-smf", "oai-upf", "oai-nrf")):
    rows, res = [], {}
    for nf in nfs:
        for i in range(reps):
            p.stop_ues()
            before = p.restart_count(nf)
            t0 = time.time()
            p.crash(nf)
            restarted = wait(lambda: p.restart_count(nf) > before, 120, 0.1)
            ready = wait(lambda: p.ready(nf), 180, 0.2)
            ready_s = time.time() - t0 if ready is not None else None
            # UERANSIM's gNB does not re-establish N2 after the AMF restarts, so reconnect it
            if nf == "oai-amf":
                p.restart_gnb()
            # service restored = a fresh UE can register and get a PDU session again
            served = None
            for _ in range(10):
                p.start_ues(1)
                if wait(lambda: ue_tun_count(p) >= 1, 25) is not None:
                    served = time.time() - t0
                    break
            rows.append([nf, i, round(restarted, 2) if restarted else "", round(ready_s, 2) if ready_s else "",
                         round(served, 2) if served else ""])
            if served:
                res.setdefault(nf, []).append(served)
            time.sleep(5)
    w.csv("recovery.csv", ["nf", "rep", "restart_detected_s", "ready_s", "service_restored_s"], rows)
    return {nf: summary(v) for nf, v in res.items()}


def t_stale(p, reps, w, counts=(0, 25, 50, 100)):
    """SMF crash with N orphaned UPF sessions (UEs gone without deregistration): time until a new
    UE gets a PDU session. OAI's UPF keeps the old SMF's sessions after the SMF restarts, and the
    restarted SMF allocates UE IPs from the start of the pool again. Worst case modelled here: the
    orphaned sessions were created by an SMF that had itself just (re)started."""
    rows, res = [], {}
    for n in counts:
        for i in range(reps):
            # clean start: no leftover UE contexts in the AMF (a gNB disconnect with stale contexts
            # blocks N2 for ~1 s per context), no leftover UPF sessions, and an SMF whose UE-IP
            # allocator starts at the pool start, so orphaned IPs overlap the next SMF's
            # the NRF is restarted too: it never expires subscriptions, and on Kubernetes every
            # restarted SMF pod (new IP) leaves a dead callback that stalls the NRF
            for nf in ("oai-nrf", "oai-amf", "oai-upf", "oai-smf"):
                p.restart_nf(nf)
            time.sleep(15)                     # NFs re-register with the fresh NRF
            p.restart_gnb()
            if n:
                p.start_ues(n)
                wait(lambda: ue_tun_count(p) >= n, 30 + 3 * n)
            p.stop_ues(graceful=False)         # UEs vanish; their sessions stay on the UPF
            p.crash("oai-smf")
            wait(lambda: p.ready("oai-smf"), 120)
            t0 = time.time()
            p.start_ues(1, graceful=False)
            got = wait(lambda: ue_tun_count(p) >= 1, 60 + 2 * n)
            rejects = len(re.findall(r"PDU Session Establishment Reject|rejected", p.ue_logs()))
            rows.append([n, i, round(got, 2) if got is not None else "", rejects])
            if got is not None:
                res.setdefault(n, []).append(got)
    for nf in ("oai-nrf", "oai-amf", "oai-upf", "oai-smf"):
        p.restart_nf(nf)
    time.sleep(15)
    p.restart_gnb()
    w.csv("stale_sessions.csv", ["orphaned_sessions", "rep", "restore_s", "ue_reject_lines"], rows)
    return {n: summary(v) for n, v in res.items()}


TESTS = {"attach": t_attach, "load": t_load, "latency": t_latency, "tput": t_tput,
         "resources": t_resources, "recovery": t_recovery, "stale": t_stale}
DEFAULT_REPS = {"attach": 20, "load": 1, "latency": 3, "tput": 3, "resources": 1, "recovery": 3, "stale": 2}


class Writer:
    def __init__(self, out):
        self.out = out
        os.makedirs(out, exist_ok=True)

    def csv(self, name, header, rows):
        with open(os.path.join(self.out, name), "w", newline="") as f:
            c = csv.writer(f)
            c.writerow(header)
            c.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("platform", choices=["compose", "k8s"])
    ap.add_argument("tests", nargs="*", default=list(TESTS))
    ap.add_argument("--reps", type=int)
    ap.add_argument("--out")
    a = ap.parse_args()
    p = Compose() if a.platform == "compose" else K8s()
    w = Writer(a.out or os.path.join(ROOT, "results", a.platform))
    summ_path = os.path.join(w.out, "summary.json")
    summ = json.load(open(summ_path)) if os.path.exists(summ_path) else {}
    p.ensure_gnb()
    if p.restart_gnb() is None:
        sys.exit("gNB could not complete NG Setup with the AMF")
    for t in a.tests:
        print(f"[{datetime.now():%H:%M:%S}] {a.platform}: {t} ...", flush=True)
        t0 = time.time()
        summ[t] = TESTS[t](p, a.reps or DEFAULT_REPS[t], w)
        summ[t]["_duration_s"] = round(time.time() - t0, 1) if isinstance(summ[t], dict) else None
        print(json.dumps(summ[t], indent=1), flush=True)
        json.dump(summ, open(summ_path, "w"), indent=1)


if __name__ == "__main__":
    sys.exit(main())
