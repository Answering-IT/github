#!/usr/bin/env bash
#
# Applies cloudwatch-dashboard.json, substituting the instance id.
#
# The dashboard definition carries a placeholder rather than the real instance id because
# this repository is public. Nothing here is secret in the cryptographic sense, but a
# machine identifier plus a description of what runs on it is reconnaissance, and it costs
# nothing to leave it out.
#
#     CI_HOST_INSTANCE_ID=i-... ./runner/apply-dashboard.sh
#
# Idempotent: put-dashboard replaces by name.

set -euo pipefail

INSTANCE="${CI_HOST_INSTANCE_ID:-${1:-}}"
NAME="${DASHBOARD_NAME:-ci-host-jenkins-ec2}"
REGION="${AWS_REGION:-us-east-1}"

if [[ -z "$INSTANCE" ]]; then
  echo "usage: CI_HOST_INSTANCE_ID=i-... $0   (or pass the id as the first argument)" >&2
  exit 1
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The EBS widget needs the root volume, which is looked up from the instance rather than
# configured: one fewer thing to keep in sync, and it cannot drift.
ROOT_VOL="${CI_HOST_ROOT_VOLUME:-$(aws ec2 describe-volumes --region "$REGION" \
  --filters "Name=attachment.instance-id,Values=${INSTANCE}" \
  --query 'Volumes[?Attachments[0].Device==`/dev/xvda`].VolumeId' --output text 2>/dev/null)}"
if [[ -z "$ROOT_VOL" ]]; then
  echo "could not resolve the root volume; set CI_HOST_ROOT_VOLUME" >&2; exit 1
fi

body="$(sed -e "s/INSTANCE_ID_PLACEHOLDER/${INSTANCE}/g" \
            -e "s/ROOT_VOLUME_PLACEHOLDER/${ROOT_VOL}/g" "$here/cloudwatch-dashboard.json")"

aws cloudwatch put-dashboard \
  --region "$REGION" \
  --dashboard-name "$NAME" \
  --dashboard-body "$body" \
  --query 'DashboardValidationMessages' --output json

echo "dashboard ${NAME} applied in ${REGION}"
echo "https://${REGION}.console.aws.amazon.com/cloudwatch/home?region=${REGION}#dashboards/dashboard/${NAME}"
