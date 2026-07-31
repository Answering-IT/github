#!/usr/bin/env bash
#
# Puts AWS CLI v2 on PATH for the remaining steps of a job, and fails loudly if it
# is not available.
#
# Why this exists: the self-hosted runner's machine also runs Jenkins, and its
# system `aws` is CLI 1.18.147 on Python 2.7, which Jenkins jobs use. Deploy.yml
# needs `aws ecr get-login-password`, a command that only exists in v2 -- in v1 it
# was `aws ecr get-login`. So v2 is installed side by side at AWS_CLI_V2_DIR and
# prepended to PATH here, for this job only. The system `aws` is never replaced,
# renamed or upgraded, so nothing Jenkins depends on changes.
#
# GitHub-hosted runners already ship v2, so on those this script only verifies.

set -euo pipefail

# Overridable so the install location is a setting rather than a hard-coded path.
AWS_CLI_V2_DIR="${AWS_CLI_V2_DIR:-/opt/aws-cli-v2/bin}"

if [[ -x "$AWS_CLI_V2_DIR/aws" ]]; then
  # GITHUB_PATH applies to later steps; exporting covers the check below, in this one.
  echo "$AWS_CLI_V2_DIR" >> "$GITHUB_PATH"
  export PATH="$AWS_CLI_V2_DIR:$PATH"
  echo "Using the side-by-side AWS CLI at $AWS_CLI_V2_DIR."
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "::error::No aws command found. Install AWS CLI v2 -- see docs/self-hosted-runner.md." >&2
  exit 1
fi

version="$(aws --version 2>&1)"
echo "$version"

case "$version" in
  aws-cli/2.*)
    ;;
  *)
    {
      echo "::error::AWS CLI v2 is required but found: $version"
      echo "  ecr get-login-password does not exist in v1, so the deploy cannot log in to ECR."
      echo "  Install v2 alongside the existing CLI -- do not replace it, Jenkins uses it."
      echo "  See docs/self-hosted-runner.md."
    } >&2
    exit 1
    ;;
esac
