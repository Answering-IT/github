#!/usr/bin/env bash
#
# Publishes the handful of numbers that actually predict trouble on the shared CI host,
# as CloudWatch custom metrics under the namespace Answering/CIHost.
#
# Why custom metrics rather than the CloudWatch agent: EC2 publishes neither memory nor
# swap by default, and those are the two that matter here -- swap growth is what evicts
# Jenkins' pages to disk. Installing the agent would work, but it is a package on a box we
# are trying not to disturb, and the instance role already carries cloudwatch:PutMetricData
# through AmazonEC2RoleforSSM. This needs no new permission and no new software.
#
# Run from a systemd timer every five minutes. Costs roughly $2/month at $0.30 per metric.
#
# The point of publishing rather than just looking: alarms. A snapshot page tells you the
# state when you remember to look; an alarm tells you when swap starts climbing at 3am.

set -uo pipefail

REGION="${AWS_REGION:-us-east-1}"
AWS=/opt/aws-cli-v2/bin/aws
NS=Answering/CIHost
BUDGET_CG=/sys/fs/cgroup/memory/ghrunners
INSTANCE="$(curl -s --max-time 3 http://169.254.169.254/latest/meta-data/instance-id || echo unknown)"

read -r mem_total mem_avail < <(free -m | awk '/^Mem:/{print $2, $7}')
swap_used=$(free -m | awk '/^Swap:/{print $3}')
disk_pct=$(df / | awk 'NR==2{gsub("%","",$5); print $5}')

# Load per vCPU, because raw load is meaningless without the core count. This was added
# after a status check showed load 19.3 on 2 vCPUs -- three runners building at once -- while
# memory looked healthy. The memory budget stops the runners exhausting RAM; nothing stops
# them all wanting CPU at the same time, and without an organisation GitHub cannot be told
# to queue across repositories. Memory alone would have said the host was fine.
vcpu=$(nproc)
load1=$(cut -d' ' -f1 /proc/loadavg)
load_per_vcpu=$(awk -v l="$load1" -v c="$vcpu" 'BEGIN{printf "%.2f", (c>0? l/c : l)}')
runners_busy=$(docker ps --filter 'name=gh-runner-' --format '{{.Names}}' 2>/dev/null | while read -r n; do
  docker logs --tail 3 "$n" 2>&1 | tail -1 | grep -q 'Running job' && echo x
done | wc -l | tr -d ' ')

# The budget cgroup's usage is the sum across every runner container, which is the number
# that decides whether a simultaneous burst is survivable.
budget_used_mb=0
budget_limit_mb=0
if [[ -r "$BUDGET_CG/memory.usage_in_bytes" ]]; then
  budget_used_mb=$(( $(cat "$BUDGET_CG/memory.usage_in_bytes") / 1024 / 1024 ))
  budget_limit_mb=$(( $(cat "$BUDGET_CG/memory.limit_in_bytes") / 1024 / 1024 ))
fi
budget_pct=0
if (( budget_limit_mb > 0 )); then
  budget_pct=$(( 100 * budget_used_mb / budget_limit_mb ))
fi

# A runner that is enabled but not active is the failure mode a snapshot page would only
# catch by luck: the repo's jobs queue for 24 hours and then die.
runners_expected=0
runners_down=0
for unit in /etc/systemd/system/multi-user.target.wants/gh-runner@*.service; do
  [[ -e "$unit" ]] || continue
  name=$(basename "$unit")
  runners_expected=$((runners_expected + 1))
  systemctl is-active --quiet "$name" || runners_down=$((runners_down + 1))
done

jenkins_up=0
pgrep -f 'jenkins\.war' >/dev/null 2>&1 && jenkins_up=1

put() {
  $AWS cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --dimensions "InstanceId=$INSTANCE" \
    --metric-name "$1" --value "$2" --unit "$3" >/dev/null 2>&1 \
    || echo "warn: failed to publish $1" >&2
}

put MemoryAvailableMB   "$mem_avail"        Megabytes
put SwapUsedMB          "$swap_used"        Megabytes
put DiskUsedPercent     "$disk_pct"         Percent
put RunnerBudgetUsedMB  "$budget_used_mb"   Megabytes
put RunnerBudgetPercent "$budget_pct"       Percent
put RunnersExpected     "$runners_expected" Count
put RunnersDown         "$runners_down"     Count
put JenkinsUp           "$jenkins_up"       Count
put LoadPerVCPU         "$load_per_vcpu"    None
put RunnersBusy         "$runners_busy"     Count

echo "published: mem_avail=${mem_avail}MB swap=${swap_used}MB disk=${disk_pct}% " \
     "budget=${budget_used_mb}/${budget_limit_mb}MB(${budget_pct}%) " \
     "runners=${runners_expected} down=${runners_down} busy=${runners_busy} " \
     "load/vcpu=${load_per_vcpu} jenkins=${jenkins_up}"
