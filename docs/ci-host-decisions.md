# CI host: decisions and the evidence behind them

Why the self-hosted runner setup looks the way it does. Written because most of these
choices look arbitrary from the outside, and several of them reverse an obvious first
instinct — including some of the ones taken during this work and then measured out of
existence.

The host is a small shared machine that also runs Jenkins. **Jenkins must keep working**
is the constraint everything else bends around.

---

## The runner runs in a container, not on the host

The host's glibc was older than the runner's bundled Node needs:

```
./externals/node20/bin/node: /lib64/libc.so.6: version `GLIBC_2.28' not found
```

The trap is that the listener is .NET, not Node. So `run.sh` connects, reports
**"Listening for Jobs"**, and looks perfectly healthy — and then every JavaScript action
fails. `actions/checkout` is a JavaScript action, so nothing gets past the first step, and
the failure reads like a broken workflow rather than a broken host.

The unit `svc.sh install` creates fails even earlier, because `runsvc.sh` launches the
listener *through* Node.

Running the runner in a container with a current glibc fixes the whole class at once, and
the host OS is left alone. The runner directory is mounted rather than baked into the image,
so the existing registration survives image rebuilds and no new registration token is needed
to redeploy.

Two alternatives were rejected: adding `container:` to every job leaves image builds
awkward, and upgrading the host OS is a migration on a live Jenkins box.

---

## Resource limits go on `docker run`, never in the unit

This one was wrong first and measured second.

A `MemoryMax=` in the systemd unit's `[Service]` section governs the `docker run` **client
process**. The work happens in Docker's own cgroup, so the limit applies to nothing that
matters. During a full build:

```
systemctl show gh-runner -p MemoryCurrent  ->  0
free -m                                    ->  3438 MB used, 846 MB swapped out
```

Reporting zero while the machine swapped is what gave it away. The limits are `docker run`
flags, and the unit carries a comment saying why, so nobody puts them back.

There is a second reason the systemd approach could not work here anyway: this host runs
systemd 219, which predates `MemoryMax` and `MemoryHigh` entirely, and Docker uses the
`cgroupfs` driver, so a `.slice` is not where containers land.

---

## The memory cap is on a shared parent cgroup, not per container

Without a GitHub organisation, registration is per repository, so *n* repositories mean *n*
runner processes. GitHub does not coordinate them: **n runners are n independent queues and
all of them can start a job in the same second.** A per-container cap of *m* therefore
permits *n × m* in total, which is how a small host ends up swapping.

A parent cgroup caps the sum. Requires `memory.use_hierarchy=1`, which was checked before
relying on it — with it at 0 a parent limit is decorative. Verified afterwards too: the
parent's `memory.usage_in_bytes` rises with its children's.

Swap is denied to the runners outright (`--memory-swap` equal to `--memory`). That converts
the failure mode from *quietly page the neighbours out to disk* into *this build fails*. A
failed build is visible and diagnosable; swap thrashing that degrades Jenkins for everyone
is neither.

What this buys was measured. Before, an unbounded build:

| | Before the cap | After |
|---|---|---|
| Peak memory used | 3438 MB | 2100 MB |
| Minimum available | **179 MB** | 1205 MB |
| Swap | 684 → **1530 MB** | 781 → 774 MB, *falling* |

The row that matters is the last one. Before, memory settled *below* where it started once
the build finished — the swapping had evicted Jenkins' own pages to disk, and Jenkins stayed
slow for minutes afterwards. Surviving is not the same as unaffected.

**A hole worth knowing:** image builds and Testcontainers run in the host Docker daemon as
sibling containers, so they fall outside the runner's cgroup. The cap bounds the runner's own
processes, not everything it starts.

---

## `--init`, because the runner does not forward SIGTERM

`run.sh` as PID 1 did not pass SIGTERM on, so `docker stop` timed out and the container was
SIGKILLed (exit 137). A killed runner leaves its session open server-side, and the
replacement then spends about a minute retrying against *"a session for this runner already
exists"*. `tini` fixes the signal path.

---

## High load with idle CPU means blocked, not busy

The most useful lesson here, and the one that reversed two earlier conclusions.

Three concurrent builds produced a load average of 22 on 2 vCPUs — 11× the core count. The
obvious reading is "not enough cores", and the obvious fix is a bigger instance. Both are
wrong. The containers were at **3% CPU**.

Load counts processes in uninterruptible sleep as well as runnable ones. The host was not
short of cores; it was waiting on the disk. The root volume was still `standard` — magnetic,
roughly 100 IOPS — while three `npm ci` runs wrote thousands of small files:

```
VolumeQueueLength   3.34      (healthy is under 1)
VolumeReadOps       ~57/s
container CPU       3.4%, 3.0%, 0.4%
```

Moving the volume to `gp3` took it to 3000 IOPS. The change is online: nothing restarted, and
Jenkins' `jenkins.war` process age stayed at 16 days across it, which is the proof that
matters. Immediately after:

```
reads/s   57  ->  2559
load      22.58  ->  16.50 and falling
```

It plausibly costs *less*, because `standard` bills per million I/O requests and `gp3` does
not below its baseline. For a 30 GB volume at list price: `standard` is $1.50/month of storage
plus $0.05 per million requests, which at ~50 sustained IOPS is roughly another $6.50;
`gp3` is $2.40/month flat. Worth checking against a real bill rather than taking the
arithmetic on trust.

`VolumeQueueLength` comes free from AWS and needs no publisher. It was available the entire
time and simply was not on the dashboard — which is why `LoadPerVCPU` is now charted directly
beside it. Either number alone is misleading; the pair is diagnostic.

### Afterwards: the bottleneck moved rather than disappeared

Measured once the volume modification finished optimising:

```
reads/s   57  ->  2384        await 11.81ms -> 6.46ms
rkB/s               128198    = 125 MB/s, util 100%
```

That is gp3's **baseline throughput** ceiling, not its IOPS ceiling — 2384 of 3000 available
requests, but every megabyte of the 125 it is allowed. Raising IOPS would achieve nothing.
The two real options are raising throughput (250 MB/s costs about $5/month more) or running
fewer builds at once, and serialising is the better trade: one build is unlikely to want
125 MB/s, and it reuses page cache between stages instead of three builds evicting each
other's.

### A false alarm, recorded so nobody re-raises it

Swap read 1503 MB before the change and 2143 MB after, which looks like the change made
things worse. It did not. That sample was taken **while the volume was still optimising** —
AWS's background migration generates its own I/O — and the reading was a transient measured
at the worst possible moment.

Checked properly afterwards:

```
swap-out over 10s:     0 KB     <- nothing is being paged out
swap-in  over 10s:    60 KB
SwapTotal - SwapFree:  978 MB   <- down from 2143 MB
```

**Swap-out at zero is the number that settles it.** What remains is historical: pages
evicted back when the disk really was the problem, now being faulted back in and released as
the faster volume lets them. Most of it belongs to Jenkins and its agents (~570 MB across
four JVMs; one agent held 150 MB of swap against 1 MB resident, having done nothing for
months). `vm.swappiness` is at its default 60, so the kernel preferring to evict idle
anonymous pages over dropping page cache is ordinary behaviour, not a symptom.

The runner budget cgroup reported `failcnt=0` throughout: the 1800 MB limit has never once
been reached, so no build has been throttled or killed by it.

The lesson is about method rather than storage: a single sample taken during a migration is
not a trend, and *rate* metrics (`pswpin`/`pswpout`) answer "is this happening now" in a way
that a *level* metric like `SwapUsedMB` cannot.

---

## Serialising builds is worth considering, for the right reason

Concurrent builds interleave their I/O into a random access pattern, which is the worst case
for any volume. Serialising them is therefore attractive even on fast storage — and the first
job would finish sooner, since three builds sharing a machine each take roughly three times
as long as one alone.

The runner supports `ACTIONS_RUNNER_HOOK_JOB_STARTED`, so a hook that waits for a free slot
would serialise properly. Two caveats before anyone builds it:

- A leaked lock would stall **all** CI silently, which is worse than slow CI. Any marker must
  expire by age so the worst case is a pause, not a stop.
- GitHub shows a waiting job as *running*, not *queued*, and the wait counts against the
  job's own timeout. There is no way to make it look queued: the runner has already accepted
  it.

---

## What is not solved

**Per-repository registration.** Every new repository needs its own runner, its own variable,
and a workflow change. An organisation would reduce that to nothing — one runner serves every
repo, one organisation variable, and jobs queue across all of them for free. That is a
decision about the GitHub account, not something this tooling can work around.

**Concurrency.** `cpu-shares` decides who wins a contended slice; it does not reduce
contention. The only levers without an organisation are how many runners are registered and,
if someone builds it, the serialising hook.

**Alarm coverage.** There is deliberately no alarm on the runner memory budget: the threshold
was sized before the storage problem was understood and will move. An alarm on a number that
is about to change only teaches people to ignore alarms.
