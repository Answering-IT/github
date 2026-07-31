#!/usr/bin/env bash
#
# Creates the alarms on the CI host metrics.
#
#     CI_HOST_INSTANCE_ID=i-... ALARM_TOPIC_ARN=arn:aws:sns:... ./runner/apply-alarms.sh
#
# A dashboard tells you when you look; an alarm tells you when you do not. These four are
# the ones worth waking up for, and none of them depends on how the runner memory budget is
# tuned -- that one is deliberately left out until the budget settles, because an alarm on a
# threshold you are about to change only teaches people to ignore alarms.
#
# Idempotent: put-metric-alarm replaces by name.

set -euo pipefail

INSTANCE="${CI_HOST_INSTANCE_ID:-}"
TOPIC="${ALARM_TOPIC_ARN:-}"
REGION="${AWS_REGION:-us-east-1}"
NS=Answering/CIHost
PREFIX="${ALARM_PREFIX:-ci-host}"

if [[ -z "$INSTANCE" ]]; then
  echo "set CI_HOST_INSTANCE_ID" >&2; exit 1
fi

actions=()
if [[ -n "$TOPIC" ]]; then
  actions=(--alarm-actions "$TOPIC" --ok-actions "$TOPIC")
else
  echo "note: ALARM_TOPIC_ARN not set, so these alarms will evaluate and stay visible in the" >&2
  echo "      console but notify nobody. That is worse than it sounds -- set it." >&2
fi

alarm() {
  local name="$1" metric="$2" stat="$3" op="$4" threshold="$5" periods="$6" missing="$7" desc="$8"
  aws cloudwatch put-metric-alarm \
    --region "$REGION" \
    --alarm-name "${PREFIX}-${name}" \
    --alarm-description "$desc" \
    --namespace "$NS" --metric-name "$metric" \
    --dimensions "Name=InstanceId,Value=${INSTANCE}" \
    --statistic "$stat" --period 300 \
    --evaluation-periods "$periods" --datapoints-to-alarm "$periods" \
    --threshold "$threshold" --comparison-operator "$op" \
    --treat-missing-data "$missing" \
    "${actions[@]}"
  echo "  ${PREFIX}-${name}"
}

# A runner registered but not listening is the quietest failure on this host: that
# repository's jobs queue for 24 hours and then die, with no failed run to notice.
alarm runner-down RunnersDown Maximum GreaterThanOrEqualToThreshold 1 2 breaching \
  "A GitHub Actions runner is registered but not listening. Jobs for that repository will queue for 24 hours and then fail."

# The host stops publishing if it dies, so missing data has to alarm here.
alarm jenkins-down JenkinsUp Minimum LessThanThreshold 1 2 breaching \
  "Jenkins is not running on the CI host, or the host stopped reporting."

# Swap growth means Jenkins pages are being evicted: the runners are denied swap by their
# cgroup, so anything paged out belongs to something else. Threshold sits above the
# observed baseline, not at zero -- this box swaps a little at rest.
alarm swap-climbing SwapUsedMB Maximum GreaterThanThreshold 1300 3 notBreaching \
  "Swap on the CI host is above its baseline for 15 minutes. The runners cannot swap, so this is Jenkins being paged out."

alarm disk-filling DiskUsedPercent Maximum GreaterThanThreshold 80 2 notBreaching \
  "Root disk on the CI host is over 80%. Each runner keeps its own copy of the release archive and its caches."

echo
echo "left out on purpose: an alarm on RunnerBudgetPercent. Add it once the budget is settled:"
echo "  the current 1800 MB was sized for one build and five idle runners, and the box has"
echo "  been running at 99% of it, so the threshold would move before anyone trusted it."
