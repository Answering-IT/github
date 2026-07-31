#!/usr/bin/env bash
#
# Creates the shared memory budget that every GitHub Actions runner container on this
# host runs inside.
#
# Why a shared parent cgroup rather than a per-container limit: without an organisation,
# each repository needs its own runner, and GitHub does not coordinate them. Eight
# runners are eight independent queues that can all start a job at the same instant. A
# per-container cap of N therefore permits 8N in total, which is how you drive a 3.8 GB
# box into swap and evict Jenkins' pages to disk.
#
# A parent cgroup caps the *sum*. This host has cgroup v1 with memory.use_hierarchy=1,
# so a limit here binds every descendant's combined usage.
#
# Why not a systemd slice: systemd here is version 219, which has no MemoryMax or
# MemoryHigh (those arrived in 231), and Docker uses the cgroupfs driver rather than
# systemd, so a .slice would not be where the containers actually land. Writing the
# cgroup directly is the mechanism that works on this box.
#
# The budget is deliberately smaller than a single runner might want. Measured on this
# host: Jenkins services 906 MB, a Jenkins build container up to 795 MB, kernel and OS
# about 300 MB. That leaves roughly 1860 MB. One runner build peaks around 1.3 GiB and
# fits; two collide and one is OOM-killed. That is the intended trade -- a failed build
# is visible and diagnosable, swap thrashing is neither, and Jenkins staying responsive
# matters more than a colliding build surviving.

set -euo pipefail

BUDGET_MB="${GH_RUNNER_BUDGET_MB:-1800}"
BUDGET_BYTES=$((BUDGET_MB * 1024 * 1024))
NAME="ghrunners"

mem="/sys/fs/cgroup/memory/${NAME}"
cpu="/sys/fs/cgroup/cpu/${NAME}"

mkdir -p "$mem" "$cpu"

if [[ "$(cat /sys/fs/cgroup/memory/memory.use_hierarchy)" != "1" ]]; then
  echo "refusing to continue: memory.use_hierarchy is 0, so a parent limit would not" >&2
  echo "bind its children and this budget would be decorative." >&2
  exit 1
fi

# Order matters: memsw must be raised only after limit_in_bytes, and setting them to the
# same value is what denies the runners swap entirely.
echo "$BUDGET_BYTES" > "$mem/memory.limit_in_bytes"
if [[ -f "$mem/memory.memsw.limit_in_bytes" ]]; then
  echo "$BUDGET_BYTES" > "$mem/memory.memsw.limit_in_bytes"
fi

# 256 against the default 1024, so the runners only get CPU that Jenkins is not asking
# for. Unlike a memory cap this cannot kill anything; it only reorders.
echo 256 > "$cpu/cpu.shares"

echo "ghrunners budget: $(( $(cat "$mem/memory.limit_in_bytes") / 1024 / 1024 )) MB memory," \
     "swap denied, cpu.shares=$(cat "$cpu/cpu.shares")"
