# github

Reusable GitHub Actions workflows for Answering services. One place where the build
gates live, so a convention changes here rather than in every repository.

This repository is **public on purpose**. `Answering-IT` is a user account, not an
organisation, and a user account cannot share workflows between private repositories —
organisations have an Actions access setting for that, user accounts do not. Public is
what lets the private service repositories call these.

Nothing secret belongs here. No instance identifiers, no hostnames, no capacity figures.
The runner tooling below does live here, but it reads the host from the environment rather
than carrying it.

## Available workflows

| Workflow | Jobs | For |
|---|---|---|
| `build-kotlin-reusable.yml` | `gradle` | Kotlin services — 9 repositories, previously with no shared gate at all |
| `build-typescript-reusable.yml` | `lint`, `build`, `test` | TypeScript services and front-ends |
| `build-python-reusable.yml` | `lint`, `unit-tests` | Python services and Lambdas |
| `build-go-reusable.yml` | `lint`, `build`, `test` | Go services |
| `deploy-cdk-reusable.yml` | `deploy` | AWS CDK apps — one stage per call, OIDC, no long-lived keys |

Every one accepts a `runner` input, defaulting to `ubuntu-latest`.

There is also one composite action:

| Action | For |
|---|---|
| `notify-discord-deploy` | One Discord embed per deploy — stage, region, ref, and the commits that shipped |

## Calling one

```yaml
jobs:
  gates:
    uses: Answering-IT/github/.github/workflows/build-kotlin-reusable.yml@v1.0.0
```

Pin a tag, never `@main`. A tag is what makes a change here a decision each consumer
opts into rather than something that lands unannounced.

## Deploying an AWS CDK app

`deploy-cdk-reusable.yml` is one `cdk deploy` against one stage. It takes the role to
assume — required, with no default, because this repository is public — plus stage,
region and the usual knobs:

```yaml
jobs:
  deploy:
    uses: Answering-IT/github/.github/workflows/deploy-cdk-reusable.yml@v1.4.0
    with:
      role_to_assume: arn:aws:iam::<account>:role/<role>
      stage: dev
      region: us-east-1
      runner: ${{ vars.RUNNER_LABEL || 'ubuntu-latest' }}
      docker_assets: true          # only if the app builds container image assets
      working_directory: infrastructure
    secrets: inherit               # for the Discord notification, see below
```

The caller needs `permissions: id-token: write` — a called workflow can lower the
token's permissions but never raise them, so declaring it here would not help.

**Post-deploy checks belong to the consumer.** A reachability probe, a smoke test, a
cache purge — those know something about the service, so they go in a `needs: deploy`
job in the calling repository, not behind another input here.

### Deploying a pull request branch

The pattern worth copying is a label gate, so shipping a branch to dev is one click and
not a second workflow that can drift from the real one:

```yaml
# .github/workflows/Build.yml
on:
  pull_request:
    # `labeled` on top of the defaults, so adding the label re-runs Build and
    # picks up the deploy job without needing a new push.
    types: [opened, synchronize, reopened, labeled]

jobs:
  deploy-dev:
    needs: cdk-build              # gates stay in front of the deploy
    # Fork PRs get read-only tokens, so OIDC cannot work there.
    if: |
      contains(github.event.pull_request.labels.*.name, 'deploy-to-dev') &&
      github.event.pull_request.head.repo.full_name == github.repository
    uses: ./.github/workflows/Deploy.yml
    with:
      stage: dev
```

Two things this surprises people with:

**The deploy does not appear as its own run.** A called workflow is a nested job of the
caller, so a labelled PR deploy shows up inside the *Build* run as
`Deploy PR to Dev / …` and never in the Actions list under "Deploy". Filtering by
workflow name finds nothing and the deploy looks like it never fired.

**Nesting is capped at four levels.** `Build.yml` → the repository's `Deploy.yml` →
`deploy-cdk-reusable.yml` is three, which leaves one. A consumer with a deeper chain
should call this one directly from `Build.yml`.

### Concurrency is the caller's

`deploy-cdk-reusable.yml` declares no `concurrency`, on purpose. Two runs against one
CloudFormation stack do need serialising — the second fails with
`UPDATE_IN_PROGRESS` — but the group has to be declared in exactly one place. Declared
both in the caller and here, the caller's run holds the group while the job it called
queues for that same group, and neither ever finishes. Put it in the caller:

```yaml
concurrency:
  group: deploy-${{ inputs.stage || 'dev' }}
  cancel-in-progress: false      # queue, so every merge ships
```

## Telling Discord what was deployed

`deploy-cdk-reusable.yml` posts one embed per deploy to the webhook in
`DISCORD_RELEASE_WEBHOOK_URL` — stage, region, ref, who pushed, and the commit subjects
that went out, with links to the compare range and the run. A failed deploy is posted
too, in red; a channel that only reports successes is one you stop trusting.

Two things are needed in the consumer, and neither can be avoided from here:

**`secrets: inherit` on the call.** A called workflow cannot read the caller's secrets
unless they are passed, so without this line the step no-ops and nothing appears. This is
the reason a repository has to be touched at all.

**The secret in the repository.** `Answering-IT` is a user account, not an organisation,
so there are no shared Actions secrets — every repository holds its own copy:

```bash
gh secret set DISCORD_RELEASE_WEBHOOK_URL -R Answering-IT/<repo> --body "$WEBHOOK"
```

Absent, the step logs that it skipped and the deploy carries on. That is what lets the
workflow change land before the secret is everywhere.

### A deploy that is not a CDK deploy

Several services release by hand — SAM, an ECS task, a script — and those never call
`deploy-cdk-reusable.yml`. They use the same composite action directly, which is why the
notification is an action and not a second reusable workflow:

```yaml
      - name: Notify Discord
        if: always()             # the failed deploy is the one worth a message
        uses: Answering-IT/github/notify-discord-deploy@v1.4.0
        with:
          webhook_url: ${{ secrets.DISCORD_RELEASE_WEBHOOK_URL }}
          stage: prod
          region: us-east-2
          details: '**Versión** v1.4.0'   # optional, appended to the embed
```

Inside the consumer's own workflow the secret is read directly, so no `secrets: inherit`
is involved. The status comes from `job.status`, which is why the step wants `always()`
and belongs last in the job.

The step never fails the job. A notification that can break a deploy is worse than no
notification, so a webhook that 404s becomes a warning annotation and nothing more.

**The commit list comes from the event payload**, read from `$GITHUB_EVENT_PATH` rather
than interpolated through `toJSON(github.event)` — a commit message with a quote or a
literal `${{` in it breaks the expression, and one with a backtick would otherwise reach
a shell. `scripts/test-notify-discord.sh` runs the step against the four event shapes a
deploy arrives in (merge to main, tag, labelled pull request, and a merge too big for one
embed) with those characters in the messages.

**AutoMaintain is deliberately not wired.** It opens dependency pull requests; it deploys
nothing, so it has nothing to announce.

## Running on the self-hosted runner

Every workflow takes `runner`. The pattern that has worked is to drive it from a
repository variable so one setting moves the whole repo, and deleting the variable
reverts it:

```yaml
jobs:
  gates:
    uses: Answering-IT/github/.github/workflows/build-kotlin-reusable.yml@v1.0.0
    with:
      runner: ${{ vars.RUNNER_LABEL || 'ubuntu-latest' }}
```

Two things to know before setting `RUNNER_LABEL`:

**Register the runner first.** A job whose label matches no registered runner does not
fail — it queues for 24 hours and then dies. Register, confirm the runner shows as idle,
then set the variable.

**A runner is bound to one repository.** Without an organisation, runner registration is
per repository, so each repo needs its own registration even when they share a machine.
Jobs then queue automatically when the machine is busy; that part needs no configuration.

### Kotlin, on a small self-hosted host

```yaml
    with:
      runner: ${{ vars.RUNNER_LABEL || 'ubuntu-latest' }}
      tasks: clean build detekt integrationTest jacocoTestReport
      gradle_cache: false
      jvm_args: -Xmx1024m -XX:+UseParallelGC -XX:MaxMetaspaceSize=384m
```

`gradle_cache: false` because `~/.gradle` already persists on a long-lived runner, so
round-tripping it through Actions storage is pure cost. `jvm_args` because
`gradle.properties` is usually tuned for a developer machine; on a host sharing 3.8 GB
with something else, an unbounded Gradle heap drives the machine into swap.

## Versioning

Semantic versioning, and consumers pin tags, so a change here is a coordinated decision.

- `v1.x.x` — backward compatible: fixes, and new inputs that default to today's behaviour
- `v2.x.x` — breaking: an input removed or renamed, a job renamed, a default changed

Workflow changes get a bump. Documentation alone does not.

```bash
git tag v1.1.0 && git push --tags
```

Adding an input with a default that reproduces existing behaviour is the cheap way to
extend one of these: no consumer has to move, and those that want the new behaviour opt
in. That is how `runner` was added.

## Relationship to answering-automation-infra

These four started in `answering-automation-infra`, which also holds a Python issue
parser, repository templates and scripts. The workflows were copied here verbatim at its
tag `v1.0.19`; nothing was deleted there.

That means two copies exist during the transition, which is deliberate and temporary.
Consumers keep working against the old path until each is moved. Once they have all
moved, the workflow files should be removed from `answering-automation-infra` so there is
one source of truth again.

Known follow-up: the three copied workflows still pin `actions/checkout@v4` and
`actions/setup-node@v4`, the versions in `v1.0.19`. They were copied unchanged so the
move stayed a move; bumping them is its own change.

## Self-hosted runners

`runner/` and `scripts/` hold everything for running these workflows on Answering hardware:
the container image, the systemd units, the shared memory budget, the metrics publisher, and
tools to register a repository and to check the host.

How to run and extend it: [docs/self-hosted-runners.md](docs/self-hosted-runners.md), including [how to read the dashboard](docs/self-hosted-runners.md#reading-the-dashboard).
Why it is built this way, with the measurements: [docs/ci-host-decisions.md](docs/ci-host-decisions.md).

Machine-specific values are not committed here. Everything takes the host from
`CI_HOST_INSTANCE_ID` or a flag, with no default — a wrong default would point these commands
at somebody else's machine, and this repository is public.

## What is deliberately not here

**No Terraform workflow.** It would have no consumers: the IAC repositories are CDK in
TypeScript. `build-go-reusable.yml` is already an example of the confusion a reusable
with no consumers causes — it predates any Go service.

**No instance identifiers, addresses or capacity figures.** Not secrets exactly, but a
machine identifier next to a description of what runs on it is reconnaissance, and leaving it
out costs nothing.
