#!/usr/bin/env bash
#
# Unwedge the shared CI host.
#
# The failure this exists for: GitHub gives a run up when the runner stops reporting, and
# the worker on the host never finds out. It keeps building for hours, so that repository's
# runner stays busy, its next job queues behind a job nobody is waiting for any more, and
# the host burns its two cores on output nothing will ever read. Measured once: two
# abandoned workers, two hours each, load average 53 on 2 vCPUs, one pull request stuck.
#
# Three actions, read from the environment because AWS-RunShellScript has no argv:
#
#   ACTION=status    read-only. Load, memory, disk, and what every runner is doing
#   ACTION=restart   restart the wedged runners -- or every one, with FORCE=true
#   ACTION=clean     give the root volume its space back
#
# TARGET is one repository name, or "all".
#
# Sent to the host by .github/workflows/ci-host-rescue.yml rather than installed on it, so
# what runs is always what is committed here. The host's copy of publish-metrics.sh has
# already drifted from this repository's; a rescue script that can drift is one you cannot
# reason about from the pull request that changed it.
#
# Plain POSIX shell: the SSM agent decides which shell reads this, and it is not always the
# one in the shebang.

set -u

ACTION="${ACTION:-status}"
TARGET="${TARGET:-all}"
FORCE="${FORCE:-false}"

# An hour. Nothing here legitimately builds that long -- the slowest job on this host is a
# few minutes -- so a worker older than this is not slow, it is abandoned.
WEDGED_SECONDS=3600

# Under this, workspaces stay. Dropping them costs every following build a fresh clone and
# a fresh npm ci, which is exactly the many-small-files I/O this host is worst at.
WORKSPACE_KEEP_BELOW_PCT=80

RUNNERS_DIR=/home/ghrunner/runners

# Every registered runner, or the one asked for. Same source as publish-metrics.sh: the
# wants/ symlinks, so a runner that is enabled but dead still appears -- that is the
# failure worth catching, since its jobs queue for 24 hours and then die with no error.
runners() {
  for unit in /etc/systemd/system/multi-user.target.wants/gh-runner@*.service; do
    [ -e "$unit" ] || continue
    inst=$(basename "$unit" .service); inst=${inst#gh-runner@}
    if [ "$TARGET" = all ] || [ "$TARGET" = "$inst" ]; then
      echo "$inst"
    fi
  done
}

# Seconds the current job has been running; empty when the runner is idle. Through
# `docker top` rather than ps, because ps on the host gives the age but not which
# repository the worker belongs to.
job_age() {
  docker top "gh-runner-$1" -o pid,etimes,args 2>/dev/null |
    awk '/Runner\.Worker/ { print $2; exit }'
}

disk_pct() { df / | awk 'NR==2 { gsub("%","",$5); print $5 }'; }

host_line() {
  echo "load  $(cut -d' ' -f1-3 /proc/loadavg)   ($(nproc) vCPU)"
  free -m | awk '/^Mem:/ {print "mem   "$7"MB available of "$2"MB"} /^Swap:/ {print "swap  "$3"MB used"}'
  df -h / | awk 'NR==2 {print "disk  "$5" of "$2" used, "$4" free"}'
}

status() {
  host_line
  echo
  for inst in $LIST; do
    age=$(job_age "$inst")
    state=$(systemctl is-active "gh-runner@$inst")
    if [ -z "$age" ]; then
      printf '  %-38s %-10s idle\n' "$inst" "$state"
    elif [ "$age" -ge "$WEDGED_SECONDS" ]; then
      printf '  %-38s %-10s WEDGED -- job running %dm\n' "$inst" "$state" "$((age / 60))"
    else
      printf '  %-38s %-10s busy -- job running %dm\n' "$inst" "$state" "$((age / 60))"
    fi
  done
}

restart() {
  pick=
  for inst in $LIST; do
    age=$(job_age "$inst")
    state=$(systemctl is-active "gh-runner@$inst")
    if [ "$state" != active ]; then
      echo "  $inst: $state -- restarting"
    elif [ "$FORCE" = true ]; then
      echo "  $inst: restarting, forced"
    elif [ -n "$age" ] && [ "$age" -ge "$WEDGED_SECONDS" ]; then
      echo "  $inst: job running $((age / 60))m -- restarting"
    elif [ -n "$age" ]; then
      echo "  $inst: busy $((age / 60))m, left alone"
      continue
    else
      echo "  $inst: idle, nothing to do"
      continue
    fi
    pick="$pick gh-runner@$inst"
  done

  if [ -z "$pick" ]; then
    echo
    echo "nothing wedged. FORCE=true restarts anyway, and kills whatever is building."
    return 0
  fi

  # One call, so systemd takes them down in parallel. Each stop waits up to 60s for its
  # container, and nine of those in sequence outlives the invocation that started them.
  echo
  # shellcheck disable=SC2086
  systemctl restart $pick || { echo "systemctl restart failed" >&2; return 1; }
  for unit in $pick; do
    printf '  %-38s %s\n' "$unit" "$(systemctl is-active "$unit")"
  done
  echo
  echo "GitHub re-queues a job whose runner went away, so the freed runner picks it up"
  echo "within seconds. Nothing has to be re-run by hand."
}

clean() {
  before=$(df -m / | awk 'NR==2 { print $4 }')
  pct=$(disk_pct)

  echo "journal:"
  journalctl --vacuum-size=500M 2>&1 | tail -1 | sed 's/^/  /'

  if [ "$pct" -lt "$WORKSPACE_KEEP_BELOW_PCT" ]; then
    echo "workspaces: kept, disk is at ${pct}% (they cost a full clone and npm ci to rebuild)"
  fi

  for inst in $LIST; do
    if [ -n "$(job_age "$inst")" ]; then
      echo "  $inst: busy, left alone"
      continue
    fi
    # ponytail: a job can start between that check and the delete below. The window is
    # seconds and the cost is one confusing build failure, not lost data. If it ever
    # actually happens, stop the unit around the delete.
    find "$RUNNERS_DIR/$inst/_diag" -name '*.log' -mtime +3 -delete 2>/dev/null
    if [ "$pct" -ge "$WORKSPACE_KEEP_BELOW_PCT" ]; then
      rm -rf "${RUNNERS_DIR:?}/$inst/_work/"* 2>/dev/null
      echo "  $inst: diag logs and workspace"
    else
      echo "  $inst: diag logs"
    fi
  done

  # Dangling images and build cache only. `-a` would also take images that no container
  # happens to be running this second but something is about to want, and Docker's data
  # sits on a different volume from the one that fills up.
  echo "docker:"
  docker system prune -f 2>&1 | tail -1 | sed 's/^/  /'

  after=$(df -m / | awk 'NR==2 { print $4 }')
  echo
  echo "freed $((after - before))MB on /  --  $(df -h / | awk 'NR==2 {print $5" used, "$4" free"}')"
}

LIST=$(runners)
if [ -z "$LIST" ]; then
  echo "no runner matches TARGET=$TARGET" >&2
  exit 2
fi

case "$ACTION" in
  status)  status ;;
  restart) restart ;;
  clean)   clean ;;
  *) echo "unknown ACTION=$ACTION -- expected status, restart or clean" >&2; exit 2 ;;
esac
