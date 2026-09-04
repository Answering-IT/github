# Changelog

Consumers pin tags, so every entry here is a change each repository opts into rather
than one that lands unannounced.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and the
versioning is [semantic](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

Nothing yet.

---

## [1.6.0] - 2026-09-04

### Added

- `release-tag-reusable.yml` takes `allow_stg`, default true. Set false, an `-rcN` tag is
  refused with a message saying the service has no stg to put it in.

  Found while migrating `form-projection-lambda`, which had written that rejection by
  hand: it has dev and prod and nothing between them, so without this the reusable would
  have sent a release candidate to a stage that does not exist. The two failures worth
  preventing are a tag that deploys nothing and a tag that falls through to prod.

  `scripts/test-release-tag.sh` now covers seventeen shapes, including `allow_stg` unset
  — `set -u` aborts only the command that reads an unset name, so a missing default would
  be a silent empty string rather than a failure, which is how `notify-discord-deploy`
  once shipped an empty embed.

---

## [1.5.0] - 2026-09-04

### Added

- `release-tag-reusable.yml`: the half of a release that was hand-written, character for
  character, in `forms-service`, `metadata-service` and `form-projection-lambda` — parse
  the tag, `-rcN` to stg and a plain `vX.Y.Z` to prod, and refuse a tag that is not
  reachable from `main`. It outputs `stage`, `version` and `sha`; it does not deploy,
  because the region a stage lives in is the consumer's own configuration and a copy of
  that mapping here is what would drift.

  `scripts/test-release-tag.sh` runs the workflow's own parse over thirteen tag shapes,
  extracted from the YAML so the check cannot drift from what ships. Seven of them are
  rejections: `v1.4`, `v1.4.0-rc`, `v1.4.0-rc1-hotfix` and `v1.4.0x` are each a
  production deploy if an anchor is loosened at one end.

- `deploy-cdk-reusable.yml` takes a `ref` — a tag, a branch or a SHA to deploy instead of
  the caller's own commit. **This is the whole of a rollback**: the same workflow aimed at
  an older tag, so going back runs the identical steps that shipped it rather than a
  second code path that only ever executes during an incident. Empty by default, which is
  what `actions/checkout` already did, so no consumer moves.

  The deploy summary and the Discord embed now name the ref, and the summary's commit is
  read off the checkout rather than `github.sha` — a rollback is dispatched from a branch,
  so `github.sha` is that branch's head and not the commit that just shipped.

- `ci-host-rescue.yml` and `runner/ci-host-rescue.sh`: `status`, `restart` and `clean`
  against the shared CI host, dispatched from the Actions tab instead of needing the AWS
  profile and the instance id on somebody's laptop.

  What it is for: GitHub gives a run up when the runner stops reporting, and the worker on
  the host never finds out. It keeps building, its runner stays busy, and the next job for
  that repository queues behind work nobody is waiting for. Found with two of them at once
  — load 53 on 2 vCPUs, both builds two hours past the point GitHub had abandoned them,
  a pull request stuck in *queued*, and the root volume at 100%.

  `restart` leaves runners that are genuinely building and only takes the wedged and the
  inactive, so pressing it during a normal build is not destructive; `force` is the escape
  hatch that is. `clean` keeps the workspaces until the disk passes 80%, because dropping
  them costs every later build a fresh clone and `npm ci`, the exact I/O this host is
  worst at.

  Runs on `ubuntu-latest`, never on the host: a rescue that queues behind the queue it is
  clearing is not a rescue. The script is sent from the checkout rather than installed, so
  what runs is what is committed — the host's `publish-metrics.sh` has already drifted from
  the copy here, which is the argument.

---

## [1.4.0] - 2026-09-02

> **The `v1.4.0` tag was moved**, minutes after it was first pushed. The original
> commit's `action.yml` used a `secrets.*` expression inside an input
> *description*, as an example of what to pass. GitHub evaluates expressions
> everywhere in an action manifest and a composite action has no `secrets`
> context, so the manifest failed to load and four consumers' deploys died in
> "Set up job".
>
> Moved rather than released as `v1.4.1` because nothing could have depended on
> the original: every run that reached it failed before executing a step.
> A new number would have meant re-opening a pull request in seven repositories
> to move a pin that never worked.
>
> `scripts/test-notify-discord.sh` now rejects any expression in the manifest
> using a context a composite action does not have. The file is valid YAML, so
> nothing but a runner could see it.

### Added

- `notify-discord-deploy`: a composite action posting one embed per deploy to
  `DISCORD_RELEASE_WEBHOOK_URL` — stage, region, ref, author, and the commit subjects that
  shipped, with links to the compare range and the run. `deploy-cdk-reusable.yml` calls it
  as its last step, so every CDK consumer gets it by bumping the tag.

  An action rather than a second reusable workflow, because the two places a deploy happens
  are not the same shape. Several services release by hand — SAM, an ECS task, a script —
  and never call `deploy-cdk-reusable.yml`; a reusable workflow could only have served the
  ones that do, leaving the rest to copy the curl.

  Posted on failure too, in red. A channel that only reports successes is one nobody reads
  as authoritative, and it never fails the job it runs in: a notification that can break a
  deploy is worse than no notification.

  The commit list is read from `$GITHUB_EVENT_PATH` with `jq`, not interpolated through
  `toJSON(github.event)`. A commit message containing a quote or a literal `${{` breaks the
  expression, and one containing a backtick would otherwise reach a shell.
  `scripts/test-notify-discord.sh` covers the four event shapes a deploy arrives in.

### Changed

- `deploy-cdk-reusable.yml`: takes an optional `DISCORD_RELEASE_WEBHOOK_URL` secret. Absent,
  the notify step logs that it skipped and the deploy carries on, so this tag is safe to
  adopt before the secret exists in a repository.

  Consumers wanting the notification add `secrets: inherit` to the call. That line cannot be
  avoided from here — a called workflow cannot read the caller's secrets unless they are
  passed — and it is the only change a CDK consumer needs.

---

## [1.3.0] - 2026-08-20

### Added

- `deploy-cdk-reusable.yml`: one `cdk deploy` against one stage, over OIDC. Moved out of
  a service repository that had a `Deploy.yml` doing exactly this, called both by
  merge-to-main and by a label-gated pull request job — two entry points that only stay
  identical while the steps live in one file. Every other CDK repository was going to
  hand-write the same twelve steps.

  `role_to_assume` is required and has no default. An account id and role name committed
  to a public repository, beside the description of what they deploy, is reconnaissance —
  the same reason the runner tooling here takes its host from the environment.

  No `concurrency` block, deliberately. Serialising deploys per stack is necessary, but a
  group declared both in the caller and in the called workflow deadlocks: the caller holds
  it while the job it called queues for it. Documented in the README so the group lands in
  the caller instead.

  Post-deploy checks are not an input. They know something about the service being
  deployed, so they belong in a `needs: deploy` job in the consumer.

---

## [1.2.0] - 2026-07-31

### Added

- `runner/` and `scripts/`: the self-hosted runner toolchain — container image, a systemd
  template unit taking the repository as its instance, the shared memory cgroup, a metrics
  publisher, and scripts to register a repository, check the host, and apply the CloudWatch
  dashboard and alarms. Documented in `docs/self-hosted-runners.md`.

  Moved here from a service repository, where infrastructure shared by every service did not
  belong. Machine-specific values are parameterised rather than committed: this repository is
  public, and an instance identifier beside a description of what runs on it is
  reconnaissance.

  `LoadPerVCPU` and `RunnersBusy` were added to the metrics after using the status tool
  caught load at 19.3 on 2 vCPUs while memory read as healthy. The shared memory cgroup stops
  runners exhausting RAM; nothing stops them all wanting CPU at once, and without an
  organisation GitHub cannot be asked to queue across repositories. Memory alone would have
  reported the host as fine.

---

## [1.1.0] - 2026-07-31

### Added

- `build-typescript-reusable.yml`: `node_version` input, defaulting to `24` — the version
  the file already hardcoded, so nothing changes for existing callers.
- `build-python-reusable.yml`: `python_version` input, defaulting to `3.11`, same reasoning.

  Both exist so that moving a repository to this one stays a move. The workflows here were
  copied from `answering-automation-infra` at `v1.0.19`, but consumers are pinned further
  back — `project-service-app` and `inventory-service-app` at `v1.0.10`, which used Node
  20. Repointing them without this input would bundle a Node 20 to 24 upgrade into what is
  supposed to be a change of address, in two repositories that declare neither `engines`
  nor `.nvmrc` to check it against. They can now pass `node_version: "20"` and upgrade the
  runtime as its own decision.

---

## [1.0.0] - 2026-07-31

First release. The four build gates now live in one place.

### Added

- `build-kotlin-reusable.yml`, new. Kotlin was the largest stack at nine repositories and
  had no shared gate, which is why each of those repositories carries its own hand-written
  workflow.

  One job and one Gradle invocation, rather than splitting lint, build, test and coverage
  across separate jobs. That split is free on GitHub-hosted runners, where the jobs run in
  parallel, and expensive on a single self-hosted runner, where they serialise and each
  repeats its own checkout and dependency resolution. The workflow it was extracted from
  also ran the unit test suite twice — once in `test`, again in the coverage job.

  Inputs: `runner`, `java_version`, `distribution`, `tasks`, `gradle_args`, `jvm_args`,
  `gradle_cache`, `report_paths`. The `tasks` default is conservative — `clean build`,
  since `build` already depends on `check` — so repositories add `integrationTest` or
  `jacocoTestReport` themselves instead of the workflow assuming every project has them.

- `build-go-reusable.yml`: `runner` input, defaulting to `ubuntu-latest`. The TypeScript
  and Python workflows already carried it; Go did not, and the inconsistency was not worth
  keeping.

### Moved

- `build-typescript-reusable.yml`, `build-python-reusable.yml` and
  `build-go-reusable.yml` copied verbatim from `answering-automation-infra` at its tag
  `v1.0.19`. Nothing was deleted there, so both copies work while consumers migrate one at
  a time.

### Notes

- Input names use underscores. GitHub expressions parse `inputs.java-version` as a
  subtraction, so a hyphenated name would force `inputs['java-version']` at every use
  site — the same reason the runner input is `runner` and not `runs-on`.
- The three copied workflows still pin `actions/checkout@v4` and `actions/setup-node@v4`,
  the versions present in `v1.0.19`. Copied unchanged so the move stayed a move; bumping
  them is a separate change.
