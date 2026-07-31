#!/usr/bin/env python3
"""Render a status page for the shared CI host.

The Jenkins EC2 runs two CI systems side by side: Jenkins, which is being migrated
away from, and a GitHub Actions self-hosted runner. Both share 2 vCPUs and 3.8 GB,
so the question worth answering at a glance is not "is it up" but "how much room is
left".

This is a snapshot, not a live feed. The box has no public address, so the page
cannot poll it; re-run this script to refresh. Every number is read through SSM with
read-only commands.

    python3 .github/scripts/runner-status.py --open

Writes runner-status.html next to itself unless --out says otherwise.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

# This repository is public, so the host is not named here. Set CI_HOST_INSTANCE_ID,
# or pass --instance-id. There is no default on purpose: a wrong default would point
# these commands at somebody else's machine.
INSTANCE_ID = os.environ.get("CI_HOST_INSTANCE_ID", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")

# Read-only throughout: free, df, ps, docker ps/stats/inspect, systemctl is-active.
# Nothing here starts, stops or reconfigures anything, and nothing touches Jenkins.
PROBE = r"""
echo ---HOST---
echo "hostname=$(hostname)"
echo "uptime=$(uptime -p 2>/dev/null || uptime)"
echo "load=$(cut -d' ' -f1-3 /proc/loadavg)"
echo "vcpu=$(nproc)"
free -m | awk '/^Mem:/{print "mem_total="$2"\nmem_used="$3"\nmem_avail="$7} /^Swap:/{print "swap_total="$2"\nswap_used="$3}'
df -h / | awk 'NR==2{print "disk_size="$2"\ndisk_used="$3"\ndisk_pct="$5}'
echo ---CONTAINERS---
docker ps -a --format '{{.Names}}|{{.Image}}|{{.State}}|{{.Status}}'
echo ---STATS---
docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.PIDs}}'
echo ---RUNNER---
# One block per registered runner. The single-runner names this used to assume
# (container gh-runner, directory actions-runner) no longer exist: there is one
# systemd template instance and one container per repository.
for unit in /etc/systemd/system/multi-user.target.wants/gh-runner@*.service; do
  [ -e "$unit" ] || continue
  inst=$(basename "$unit" .service); inst=${inst#gh-runner@}
  echo "runner|$inst|$(systemctl is-active "gh-runner@$inst" 2>&1)|$(docker logs "gh-runner-$inst" 2>&1 | tr -d '\r' | grep -v '^$' | tail -1)"
done
echo "budget|$(cat /sys/fs/cgroup/memory/ghrunners/memory.limit_in_bytes 2>/dev/null || echo 0)|$(cat /sys/fs/cgroup/memory/ghrunners/memory.usage_in_bytes 2>/dev/null || echo 0)|$(ls -d /sys/fs/cgroup/memory/ghrunners/*/ 2>/dev/null | wc -l)"
# Flags are identical across instances because they come from one template unit, so one
# sample describes them all.
first=$(ls -1 /etc/systemd/system/multi-user.target.wants/gh-runner@*.service 2>/dev/null | head -1)
if [ -n "$first" ]; then
  i=$(basename "$first" .service); i=${i#gh-runner@}
  docker inspect "gh-runner-$i" --format 'flags|{{.HostConfig.Memory}}|{{.HostConfig.MemorySwap}}|{{.HostConfig.CpuShares}}|{{.HostConfig.CgroupParent}}|{{.HostConfig.Init}}' 2>/dev/null
fi
echo ---BINFMT---
ls /proc/sys/fs/binfmt_misc/ 2>/dev/null | tr '\n' ' '
"""


def run_probe(profile: str | None) -> str:
    if not shutil.which("aws"):
        sys.exit("aws CLI not found on PATH.")
    base = ["aws", "ssm"]
    env_args = ["--region", REGION]
    if profile:
        env_args += ["--profile", profile]

    send = subprocess.run(
        base
        + [
            "send-command",
            "--instance-ids",
            INSTANCE_ID,
            "--document-name",
            "AWS-RunShellScript",
            "--comment",
            "read-only: CI host status page",
            "--parameters",
            json.dumps({"commands": [PROBE]}),
            "--query",
            "Command.CommandId",
            "--output",
            "text",
        ]
        + env_args,
        capture_output=True,
        text=True,
    )
    if send.returncode != 0:
        sys.exit(f"send-command failed:\n{send.stderr.strip()}")
    command_id = send.stdout.strip()

    for _ in range(30):
        time.sleep(4)
        got = subprocess.run(
            base
            + [
                "get-command-invocation",
                "--command-id",
                command_id,
                "--instance-id",
                INSTANCE_ID,
                "--query",
                "[Status,StandardOutputContent]",
                "--output",
                "json",
            ]
            + env_args,
            capture_output=True,
            text=True,
        )
        if got.returncode != 0:
            continue
        status, out = json.loads(got.stdout)
        if status in ("Success", "Failed", "Cancelled", "TimedOut"):
            if status != "Success":
                sys.exit(f"probe finished as {status}")
            return out
    sys.exit("probe did not finish in time")


def parse(raw: str) -> dict:
    sections: dict[str, list[str]] = {}
    current = None
    for line in raw.splitlines():
        m = re.fullmatch(r"---([A-Z]+)---", line.strip())
        if m:
            current = m.group(1)
            sections[current] = []
        elif current:
            sections[current].append(line.rstrip())

    def kv(name: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in sections.get(name, []):
            if "=" in line:
                k, _, v = line.partition("=")
                out.setdefault(k.strip(), v.strip())
        return out

    host = kv("HOST")

    runners, budget, flags = [], {}, {}
    for line in sections.get("RUNNER", []):
        parts = line.split("|")
        if parts[0] == "runner" and len(parts) >= 4:
            runners.append({"repo": parts[1], "unit": parts[2], "log": "|".join(parts[3:])})
        elif parts[0] == "budget" and len(parts) >= 4:
            budget = {
                "limit_mb": int(parts[1] or 0) // 1024 // 1024,
                "used_mb": int(parts[2] or 0) // 1024 // 1024,
                "count": int(parts[3] or 0),
            }
        elif parts[0] == "flags" and len(parts) >= 6:
            flags = {
                "mem_mb": int(parts[1] or 0) // 1024 // 1024,
                "swap_mb": int(parts[2] or 0) // 1024 // 1024,
                "shares": parts[3],
                "parent": parts[4],
                "init": parts[5],
            }

    containers = {}
    for line in sections.get("CONTAINERS", []):
        parts = line.split("|")
        if len(parts) == 4:
            containers[parts[0]] = {"image": parts[1], "state": parts[2], "status": parts[3]}
    for line in sections.get("STATS", []):
        parts = line.split("|")
        if len(parts) == 5 and parts[0] in containers:
            containers[parts[0]].update(
                {"cpu": parts[1], "mem": parts[2], "mem_pct": parts[3], "pids": parts[4]}
            )

    binfmt = " ".join(sections.get("BINFMT", [])).split()

    return {
        "host": host,
        "runners": runners,
        "budget": budget,
        "flags": flags,
        "containers": containers,
        "binfmt": binfmt,
        "collected": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def mib(text: str) -> float:
    """Docker prints '839.2MiB' or '1.296GiB'. Return MiB."""
    m = re.match(r"([\d.]+)\s*([KMG])iB", text.strip(), re.I)
    if not m:
        return 0.0
    value, unit = float(m.group(1)), m.group(2).upper()
    return {"K": value / 1024, "M": value, "G": value * 1024}[unit]


# ── rendering ───────────────────────────────────────────────────────────────────

CSS = """
:root {
  color-scheme: light dark;
  --paper:#f5f7f9; --card:#ffffff; --ink:#101820; --ink-soft:#4a5a68;
  --line:#d8e0e6; --line-soft:#e9eef1;
  --accent:#0d7d8c; --accent-soft:#e4f1f3;
  --ok:#2e7d32; --warn:#a85c00; --crit:#b3261e;
  --jenkins:#5b7c99; --runner:#0d7d8c; --other:#9aa8b2; --free:#dfe6ea;
  --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper:#0d141a; --card:#151f27; --ink:#e6edf2; --ink-soft:#9bb0bf;
    --line:#25333d; --line-soft:#1c272f;
    --accent:#3aa8b8; --accent-soft:#123035;
    --ok:#6fbf73; --warn:#e0a049; --crit:#f2837a;
    --jenkins:#7e9fbb; --runner:#3aa8b8; --other:#5c6b76; --free:#1f2a33;
  }
}
:root[data-theme="dark"] {
  --paper:#0d141a; --card:#151f27; --ink:#e6edf2; --ink-soft:#9bb0bf;
  --line:#25333d; --line-soft:#1c272f;
  --accent:#3aa8b8; --accent-soft:#123035;
  --ok:#6fbf73; --warn:#e0a049; --crit:#f2837a;
  --jenkins:#7e9fbb; --runner:#3aa8b8; --other:#5c6b76; --free:#1f2a33;
}
:root[data-theme="light"] {
  --paper:#f5f7f9; --card:#ffffff; --ink:#101820; --ink-soft:#4a5a68;
  --line:#d8e0e6; --line-soft:#e9eef1;
  --accent:#0d7d8c; --accent-soft:#e4f1f3;
  --ok:#2e7d32; --warn:#a85c00; --crit:#b3261e;
  --jenkins:#5b7c99; --runner:#0d7d8c; --other:#9aa8b2; --free:#dfe6ea;
}

body { margin:0; background:var(--paper); color:var(--ink); font-family:var(--sans);
       font-size:15px; line-height:1.55; -webkit-font-smoothing:antialiased; }
.wrap { max-width:1080px; margin:0 auto; padding:40px 24px 72px; }
.num { font-family:var(--mono); font-variant-numeric:tabular-nums; }

.eyebrow { font-family:var(--mono); font-size:11px; letter-spacing:.14em;
           text-transform:uppercase; color:var(--ink-soft); }
h1 { font-size:26px; line-height:1.2; margin:6px 0 4px; text-wrap:balance; letter-spacing:-.01em; }
.lede { color:var(--ink-soft); max-width:64ch; margin:0; }
.snapshot { display:inline-flex; align-items:center; gap:8px; margin-top:14px;
            font-family:var(--mono); font-size:12px; color:var(--ink-soft);
            border:1px solid var(--line); border-radius:999px; padding:4px 12px; }
.snapshot b { color:var(--ink); font-weight:600; }

h2 { font-size:13px; font-family:var(--mono); letter-spacing:.1em; text-transform:uppercase;
     color:var(--ink-soft); margin:0; font-weight:600; }
section { margin-top:38px; display:flex; flex-direction:column; gap:14px; }

.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:14px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:16px 18px; display:flex; flex-direction:column; gap:6px;
        border-left:3px solid var(--line); }
.card.ok   { border-left-color:var(--ok); }
.card.warn { border-left-color:var(--warn); }
.card.crit { border-left-color:var(--crit); }
.card .label { font-family:var(--mono); font-size:11px; letter-spacing:.1em;
               text-transform:uppercase; color:var(--ink-soft); }
.card .value { font-family:var(--mono); font-size:22px; font-variant-numeric:tabular-nums;
               letter-spacing:-.02em; }
.card .note { font-size:13px; color:var(--ink-soft); }

.pill { display:inline-flex; align-items:center; gap:6px; align-self:flex-start;
        font-family:var(--mono); font-size:11px; letter-spacing:.06em; text-transform:uppercase;
        padding:2px 9px; border-radius:999px; border:1px solid currentColor; }
.pill.ok{color:var(--ok);} .pill.warn{color:var(--warn);} .pill.crit{color:var(--crit);}
.dot { width:6px; height:6px; border-radius:50%; background:currentColor; }

.budget { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:18px; }
.bar { display:flex; height:34px; border-radius:6px; overflow:hidden; background:var(--free);
       border:1px solid var(--line); }
.bar span { display:block; }
.legend { display:flex; flex-wrap:wrap; gap:16px; margin-top:14px;
          font-family:var(--mono); font-size:12px; color:var(--ink-soft); }
.legend i { display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:6px; }
.legend b { color:var(--ink); font-weight:600; font-variant-numeric:tabular-nums; }
.swapline { margin-top:16px; padding-top:14px; border-top:1px solid var(--line-soft);
            display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap;
            font-family:var(--mono); font-size:12px; color:var(--ink-soft); }

.tablewrap { overflow-x:auto; border:1px solid var(--line); border-radius:10px; background:var(--card); }
table { border-collapse:collapse; width:100%; min-width:640px; }
th { font-family:var(--mono); font-size:10px; letter-spacing:.1em; text-transform:uppercase;
     color:var(--ink-soft); text-align:left; padding:11px 14px; border-bottom:1px solid var(--line);
     font-weight:600; white-space:nowrap; }
td { padding:11px 14px; border-bottom:1px solid var(--line-soft); vertical-align:middle; }
tr:last-child td { border-bottom:none; }
td.n { font-family:var(--mono); font-variant-numeric:tabular-nums; white-space:nowrap; }
.who { display:flex; flex-direction:column; gap:1px; }
.who b { font-family:var(--mono); font-weight:600; }
.who span { font-size:12px; color:var(--ink-soft); }
.meter { width:100%; min-width:90px; height:6px; background:var(--free); border-radius:3px; overflow:hidden; }
.meter i { display:block; height:100%; background:var(--accent); }
.tag { font-family:var(--mono); font-size:10px; letter-spacing:.06em; text-transform:uppercase;
       color:var(--ink-soft); border:1px solid var(--line); border-radius:4px; padding:1px 6px; }

dl { margin:0; display:grid; grid-template-columns:minmax(150px,auto) 1fr; gap:1px 20px;
     background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px 18px; }
dt { font-family:var(--mono); font-size:11px; letter-spacing:.08em; text-transform:uppercase;
     color:var(--ink-soft); padding:7px 0; }
dd { margin:0; padding:7px 0; font-family:var(--mono); font-size:13px; word-break:break-word; }

.log { background:var(--card); border:1px solid var(--line); border-radius:10px;
       padding:14px 16px; font-family:var(--mono); font-size:12.5px; color:var(--ink-soft);
       display:flex; flex-direction:column; gap:5px; overflow-x:auto; }
.log b { color:var(--ink); font-weight:600; }

footer { margin-top:44px; padding-top:18px; border-top:1px solid var(--line);
         color:var(--ink-soft); font-size:13px; }
footer code { font-family:var(--mono); font-size:12px; background:var(--accent-soft);
              padding:1px 5px; border-radius:3px; }
"""


def sev(value: float, warn: float, crit: float) -> str:
    return "crit" if value >= crit else "warn" if value >= warn else "ok"


def render(d: dict) -> str:
    h = d["host"]
    e = html.escape

    total = float(h.get("mem_total", 0) or 0)
    avail = float(h.get("mem_avail", 0) or 0)
    swap_used = float(h.get("swap_used", 0) or 0)
    swap_total = float(h.get("swap_total", 0) or 0)
    vcpu = int(h.get("vcpu", 2) or 2)
    load1 = float((h.get("load", "0 0 0").split() or ["0"])[0])

    cs = d["containers"]
    jenkins_names = {"jenkins", "agent"}
    jenkins_mem = sum(mib(c.get("mem", "0MiB")) for n, c in cs.items() if n in jenkins_names)
    # Anything else that is not our runner is a Jenkins build container.
    build_mem = sum(
        mib(c.get("mem", "0MiB")) for n, c in cs.items() if n not in jenkins_names and not n.startswith("gh-runner")
    )
    runner_mem = sum(mib(c.get("mem", "0MiB")) for n, c in cs.items() if n.startswith("gh-runner"))
    other = max(total - avail - jenkins_mem - build_mem - runner_mem, 0)

    def pct(v: float) -> float:
        return round(100 * v / total, 2) if total else 0

    runners = d["runners"]
    runners_up = [r for r in runners if r["unit"] == "active"]
    runners_busy = [r for r in runners if "Running job" in r["log"]]
    jenkins_up = any(n == "jenkins" and c.get("state") == "running" for n, c in cs.items())

    mem_sev = sev(100 - pct(avail), 75, 90)
    swap_sev = sev(100 * swap_used / swap_total if swap_total else 0, 20, 40)
    load_sev = sev(load1 / vcpu, 0.8, 1.2)


    rows = []
    for name, c in sorted(cs.items(), key=lambda kv: -mib(kv[1].get("mem", "0MiB"))):
        is_runner = name.startswith("gh-runner")
        role = "GitHub runner" if is_runner else "Jenkins" if name in jenkins_names else "Jenkins build"
        cpu_raw = c.get("cpu", "0%").rstrip("%")
        try:
            cpu_val = float(cpu_raw)
        except ValueError:
            cpu_val = 0.0
        m = mib(c.get("mem", "0MiB"))
        rows.append(
            f"""<tr>
  <td><div class="who"><b>{e(name)}</b><span>{e(c.get('image','?'))}</span></div></td>
  <td><span class="tag">{e(role)}</span></td>
  <td class="n">{e(c.get('status','?'))}</td>
  <td class="n">{cpu_val:,.1f}%</td>
  <td class="n">{m:,.0f} MiB</td>
  <td><div class="meter"><i style="width:{min(100, 100*m/total if total else 0):.1f}%"></i></div></td>
  <td class="n">{e(c.get('pids','–'))}</td>
</tr>"""
        )

    seg = lambda w, var, label: (
        f'<span style="width:{w:.2f}%;background:var(--{var})" title="{label}"></span>' if w > 0.4 else ""
    )

    runner_rows = "".join(
        f'<tr><td><div class="who"><b>{e(r["repo"])}</b></div></td>'
        f'<td><span class="pill {"ok" if r["unit"] == "active" else "crit"}">'
        f'<span class="dot"></span>{e(r["unit"])}</span></td>'
        f'<td class="n" style="font-size:12px">{e(r["log"][:96])}</td></tr>'
        for r in sorted(runners, key=lambda x: x["repo"])
    ) or '<tr><td colspan="3">no runners registered</td></tr>'
    b, fl = d["budget"], d["flags"]

    return f"""<div class="wrap">
<header>
  <p class="eyebrow">Shared CI host &middot; {e(h.get('hostname','?'))}</p>
  <h1>One machine, two CI systems</h1>
  <p class="lede">Jenkins and a GitHub Actions self-hosted runner share this
  t3.medium &mdash; {vcpu} vCPUs and {total:,.0f} MiB. The question that matters is not
  whether each is up, but how much room is left when both want it at once.</p>
  <p class="snapshot"><span class="dot" style="color:var(--accent)"></span>
     Snapshot taken <b>{e(d['collected'])}</b> &middot; not live</p>
</header>

<section>
  <h2>State</h2>
  <div class="cards">
    <div class="card {'ok' if jenkins_up else 'crit'}">
      <span class="label">Jenkins</span>
      <span class="pill {'ok' if jenkins_up else 'crit'}"><span class="dot"></span>{'running' if jenkins_up else 'down'}</span>
      <span class="note">{len([n for n in cs if n in jenkins_names])} service containers,
        {len([n for n in cs if n not in jenkins_names and not n.startswith('gh-runner')])} build container(s) active</span>
    </div>
    <div class="card {'crit' if len(runners_up) < len(runners) else 'ok'}">
      <span class="label">GitHub runners</span>
      <span class="value">{len(runners_up)}<span style="font-size:13px;color:var(--ink-soft)"> / {len(runners)}</span></span>
      <span class="note">{len(runners_busy)} running a job &middot;
        {len(runners) - len(runners_up)} down</span>
    </div>
    <div class="card {mem_sev}">
      <span class="label">Memory available</span>
      <span class="value">{avail:,.0f}<span style="font-size:13px;color:var(--ink-soft)"> MiB</span></span>
      <span class="note">of {total:,.0f} MiB total</span>
    </div>
    <div class="card {load_sev}">
      <span class="label">Load, 1 min</span>
      <span class="value">{load1:.2f}</span>
      <span class="note">across {vcpu} vCPUs &middot; {load1/vcpu*100:.0f}% of capacity</span>
    </div>
  </div>
</section>

<section>
  <h2>Memory budget</h2>
  <div class="budget">
    <div class="bar">
      {seg(pct(jenkins_mem), 'jenkins', 'Jenkins services')}
      {seg(pct(build_mem), 'other', 'Jenkins builds')}
      {seg(pct(runner_mem), 'runner', 'GitHub runner')}
      {seg(pct(other), 'other', 'Kernel and everything else')}
    </div>
    <div class="legend">
      <span><i style="background:var(--jenkins)"></i>Jenkins services <b>{jenkins_mem:,.0f} MiB</b></span>
      <span><i style="background:var(--other)"></i>Jenkins builds <b>{build_mem:,.0f} MiB</b></span>
      <span><i style="background:var(--runner)"></i>GitHub runner <b>{runner_mem:,.0f} MiB</b></span>
      <span><i style="background:var(--other)"></i>Kernel &amp; rest <b>{other:,.0f} MiB</b></span>
      <span><i style="background:var(--free)"></i>Available <b>{avail:,.0f} MiB</b></span>
    </div>
    <div class="swapline">
      <span>Swap in use <b style="color:var(--{swap_sev})">{swap_used:,.0f} MiB</b> of {swap_total:,.0f} MiB</span>
      <span>Swap growth is the signal to watch: it means Jenkins pages are being evicted to disk.</span>
    </div>
  </div>
</section>

<section>
  <h2>Containers</h2>
  <div class="tablewrap">
    <table>
      <thead><tr><th>Container</th><th>Role</th><th>Uptime</th><th>CPU</th><th>Memory</th>
        <th style="width:120px">Share of host</th><th>PIDs</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </div>
</section>

<section>
  <h2>Runners</h2>
  <div class="tablewrap">
    <table>
      <thead><tr><th>Repository</th><th>Unit</th><th>Last line from its log</th></tr></thead>
      <tbody>{runner_rows}</tbody>
    </table>
  </div>
  <dl>
    <dt>Shared budget</dt><dd>{b.get('used_mb',0):,} MiB used of {b.get('limit_mb',0):,} MiB, across {b.get('count',0)} runner cgroups &mdash; the cap applies to the <em>sum</em>, not to each container</dd>
    <dt>Per container</dt><dd>{fl.get('mem_mb',0):,} MiB, swap capped at {fl.get('swap_mb',0):,} MiB so a runner cannot swap &middot; cpu-shares {e(fl.get('shares','?'))} against Docker's default of 1024</dd>
    <dt>cgroup parent</dt><dd>{e(fl.get('parent','?') or 'none')} &middot; init {e(str(fl.get('init','?')))}</dd>
    <dt>binfmt</dt><dd>{e(' '.join(d['binfmt']) or 'none')}</dd>
    <dt>Host uptime</dt><dd>{e(h.get('uptime','?'))}</dd>
    <dt>Root disk</dt><dd>{e(h.get('disk_used','?'))} of {e(h.get('disk_size','?'))} used ({e(h.get('disk_pct','?'))})</dd>
  </dl>
</section>


<footer>
  <p>Regenerate with <code>python3 .github/scripts/runner-status.py</code>. Every value is read
  through SSM using read-only commands &mdash; nothing here starts, stops or reconfigures
  anything, and nothing touches Jenkins.</p>
  <p>Caps apply to the runner's own processes. Image builds and Testcontainers run in the
  host Docker daemon as sibling containers, so they fall outside them.</p>
</footer>
</div>"""


def main() -> None:
    global INSTANCE_ID
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instance-id", default=INSTANCE_ID,
                    help="EC2 instance running the runners. Defaults to $CI_HOST_INSTANCE_ID")
    ap.add_argument("--profile", default="ans-super", help="AWS profile (default: ans-super)")
    ap.add_argument("--out", default=None, help="output HTML path")
    ap.add_argument("--open", action="store_true", help="open the page in a browser when done")
    ap.add_argument("--raw", default=None, help="parse a saved probe output instead of running SSM")
    args = ap.parse_args()

    INSTANCE_ID = args.instance_id
    if not INSTANCE_ID:
        sys.exit("set CI_HOST_INSTANCE_ID or pass --instance-id")

    raw = Path(args.raw).read_text() if args.raw else run_probe(args.profile)
    data = parse(raw)

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CI host status</title>
<style>{CSS}</style></head><body>
{render(data)}
</body></html>"""

    out = Path(args.out) if args.out else Path(__file__).with_name("runner-status.html")
    out.write_text(page)
    print(f"wrote {out}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
