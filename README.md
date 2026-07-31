# github

Reusable GitHub Actions workflows for Answering services. One place where the build
gates live, so a convention changes here rather than in every repository.

This repository is **public on purpose**. `Answering-IT` is a user account, not an
organisation, and a user account cannot share workflows between private repositories —
organisations have an Actions access setting for that, user accounts do not. Public is
what lets the private service repositories call these.

Nothing secret belongs here. No instance identifiers, no hostnames, no infrastructure
topology. Tooling that needs those lives in a private repository.

## Available workflows

| Workflow | Jobs | For |
|---|---|---|
| `build-kotlin-reusable.yml` | `gradle` | Kotlin services — 9 repositories, previously with no shared gate at all |
| `build-typescript-reusable.yml` | `lint`, `build`, `test` | TypeScript services and front-ends |
| `build-python-reusable.yml` | `lint`, `unit-tests` | Python services and Lambdas |
| `build-go-reusable.yml` | `lint`, `build`, `test` | Go services |

Every one accepts a `runner` input, defaulting to `ubuntu-latest`.

## Calling one

```yaml
jobs:
  gates:
    uses: Answering-IT/github/.github/workflows/build-kotlin-reusable.yml@v1.0.0
```

Pin a tag, never `@main`. A tag is what makes a change here a decision each consumer
opts into rather than something that lands unannounced.

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

## What is deliberately not here

**No Terraform workflow.** It would have no consumers: the IAC repositories are CDK in
TypeScript. `build-go-reusable.yml` is already an example of the confusion a reusable
with no consumers causes — it predates any Go service.

**No runner tooling.** The script that reports the CI host's state needs the instance
identifier and describes how the machine is accessed. That belongs in a private
repository, not here.
