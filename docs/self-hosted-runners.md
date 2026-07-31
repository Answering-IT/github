# Self-hosted runners

How Answering runs GitHub Actions on its own hardware, and how to put a repository on it.

Machine-specific values — instance identifier, addresses, capacity — are deliberately not
in this repository, which is public. Everything here takes them from
`CI_HOST_INSTANCE_ID` or a flag, and the operational notes for the actual host live in a
private repository.

## The shape of it

Without a GitHub organisation, **runner registration is per repository**. Organisations can
register one runner that serves every repo; user accounts cannot. So a host running CI for
five repositories runs five runner processes, one per repository, from a single systemd
template.

That has a consequence worth understanding before adding the sixth: **GitHub does not
coordinate runners.** Five runners are five independent queues, and all five can start a job
in the same second. Jobs queue *per runner*, not per host. More runners means more
concurrency, not more queueing — the opposite of what people usually expect.

Two things bound the damage:

- **A shared memory cgroup.** Every runner container runs inside one parent cgroup with a
  single memory limit, so the cap applies to the *sum* rather than to each container. Swap is
  denied outright, which converts "silently page the neighbours out to disk" into "this build
  fails" — visible and diagnosable.
- **cpu-shares.** The runners are weighted below anything else on the box, so they get CPU
  that nothing else is asking for.

CPU is the one that is not really bounded. Shares change who wins a contended slice; they do
not stop three builds wanting four cores that do not exist. On a small host, concurrency is
what has to be managed, and the only lever without an organisation is how many runners are
registered.

## Adding a repository

```shell
export CI_HOST_INSTANCE_ID=i-...
python3 scripts/runner-add.py --repo <repo> --token-stdin
```

The token comes from the repository's own settings — it is not an organisation token and one
repository's token will not register another:

```
https://github.com/<owner>/<repo>/settings/actions/runners/new
```

Then, in that order:

1. **Open a pull request** setting every job's runner from a variable:

   ```yaml
   runs-on: ${{ vars.RUNNER_LABEL || 'ubuntu-latest' }}
   ```

   If it calls the shared gates in this repository, point them here too and pass `runner` the
   same way. Check what version it was pinned to first: moving forward can carry a runtime
   change with it, which is why the gates take `node_version` and `python_version`.

2. **Set the variable** once the runner shows as idle:

   ```shell
   gh api -X POST /repos/<owner>/<repo>/actions/variables \
     -f name=RUNNER_LABEL -f value=self-hosted
   ```

**Never set the variable before the runner is listening.** A job whose label matches no
registered runner does not fail — it queues for 24 hours and then dies, with no error to read.

Variables are per repository, not per branch. Setting one early makes the pull request's own
checks run on the runner, which is useful for verifying the change — and moves every other
job in that repository at the same moment.

Removing is the inverse: `runner-add.py --repo <repo> --remove`. It drops the local
credentials without contacting GitHub, so an offline entry is left on the repository's
runners page to delete by hand.

## Host setup

The pieces in `runner/`, installed once per host:

| File | Where | Does |
|---|---|---|
| `Dockerfile` | build as `gh-actions-runner:local` | The runtime. Needed when the host's glibc is older than the runner's bundled Node requires — the .NET listener runs fine on an old glibc, so the runner connects and then every JavaScript action fails, which reads like a broken workflow rather than a broken host |
| `gh-runner-budget.sh` + `.service` | `/usr/local/sbin`, `/etc/systemd/system` | Creates the shared memory cgroup at boot |
| `gh-runner@.service` | `/etc/systemd/system` | One template, one instance per repository |
| `publish-metrics.sh` + `.service` + `.timer` | as above | Publishes host metrics every five minutes |
| `apply-dashboard.sh`, `apply-alarms.sh` | run from anywhere | CloudWatch dashboard and alarms |

Resource limits belong on `docker run`, not in the unit's `[Service]` section. A systemd
limit there applies to the `docker run` client process while the work happens in Docker's own
cgroup — measured, not assumed: during a full build `systemctl show -p MemoryCurrent`
reported 0 while the machine was using 3.4 GB and swapping.

## Watching it

```shell
CI_HOST_INSTANCE_ID=i-... python3 scripts/runner-status.py --open
```

A snapshot: containers and what each is using, the memory split, every runner's unit state
and last log line, the shared budget. A snapshot rather than a live page because the host has
no public address.

Continuous monitoring is `publish-metrics.sh` on a timer, publishing to the
`Answering/CIHost` namespace. It needs no CloudWatch agent and no new IAM: the SSM instance
role already carries `cloudwatch:PutMetricData` through `AmazonEC2RoleforSSM`.

| Metric | Why |
|---|---|
| `RunnersDown` | The quietest failure: registered but not listening, so that repo's jobs queue 24 hours and die |
| `JenkinsUp` | Whatever else shares the host must survive CI |
| `SwapUsedMB` | The runners cannot swap, so growth here is something else being paged out |
| `LoadPerVCPU` | Raw load means nothing without the core count. Added after a check showed load 19.3 on 2 vCPUs while memory looked healthy — the memory budget stops runners exhausting RAM, and nothing stops them all wanting CPU |
| `RunnersBusy` | How much of the concurrency is actually in use |
| `MemoryAvailableMB`, `DiskUsedPercent`, `RunnerBudgetUsedMB`, `RunnerBudgetPercent`, `RunnersExpected` | Context for the above |

Alarms cover `RunnersDown`, `JenkinsUp`, `SwapUsedMB` and `DiskUsedPercent`. There is
deliberately none on the budget percentage: an alarm on a threshold that is about to be
retuned only teaches people to ignore alarms.

```shell
CI_HOST_INSTANCE_ID=i-... ALARM_TOPIC_ARN=arn:aws:sns:... ./runner/apply-alarms.sh
CI_HOST_INSTANCE_ID=i-... ./runner/apply-dashboard.sh
```

## Sizing, honestly

Five repositories on a two-vCPU host works, and it queues rather than falling over, but three
concurrent builds put the load average around ten times the core count. Memory is the
resource the budget solved; CPU is the one still exposed.

If more repositories are coming, the choices are fewer active runners, a larger host, or an
organisation — which is the only one that also removes the per-repository registration and
lets one runner serve everything.
