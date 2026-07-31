# Changelog

Consumers pin tags, so every entry here is a change each repository opts into rather
than one that lands unannounced.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and the
versioning is [semantic](https://semver.org/spec/v2.0.0.html).

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
