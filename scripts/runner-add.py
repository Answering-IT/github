#!/usr/bin/env python3
"""Register a repository on the shared self-hosted runner host.

Without a GitHub organisation, runner registration is per repository: each repo needs its
own runner process even though they share one machine. This does the three steps that
implies, in the order that matters.

    python3 .github/scripts/runner-add.py --repo inventory-service --token BKXAJ...
    python3 .github/scripts/runner-add.py --repo inventory-service --token-stdin

Get the token from the repository's own settings page — it is not an organisation token and
one repository's token will not register another:

    https://github.com/Answering-IT/<repo>/settings/actions/runners/new

Tokens last about an hour and are single use.

Order is not cosmetic. A job whose `runs-on` label matches no registered runner does not
fail: it queues for 24 hours and then dies. So the runner is registered and confirmed
listening *before* anything points at it. --set-variable is offered but off by default for
that reason; pass it only once you also have the workflow change merged, or the repo's
existing jobs will start queueing against a runner they were never tested on.

To remove a repository:

    python3 .github/scripts/runner-add.py --repo inventory-service --remove
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

# This repository is public, so the host is not named here. Set CI_HOST_INSTANCE_ID,
# or pass --instance-id. There is no default on purpose: a wrong default would point
# these commands at somebody else's machine.
INSTANCE_ID = os.environ.get("CI_HOST_INSTANCE_ID", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")
OWNER = os.environ.get("GH_OWNER", "Answering-IT")
# Any already-registered runner's tarball; every repo reuses the same archive.
TARBALL_GLOB = "/home/ghrunner/runners/*/actions-runner-linux-x64-*.tar.gz"


def ssm(commands: list[str], profile: str, timeout: int = 900) -> str:
    if not shutil.which("aws"):
        sys.exit("aws CLI not found on PATH.")
    env = ["--region", REGION] + (["--profile", profile] if profile else [])
    send = subprocess.run(
        ["aws", "ssm", "send-command", "--instance-ids", INSTANCE_ID,
         "--document-name", "AWS-RunShellScript",
         "--comment", "register or remove a self-hosted runner",
         "--parameters", json.dumps({"commands": commands}),
         "--timeout-seconds", str(timeout),
         "--query", "Command.CommandId", "--output", "text"] + env,
        capture_output=True, text=True,
    )
    if send.returncode != 0:
        sys.exit(f"send-command failed:\n{send.stderr.strip()}")
    cid = send.stdout.strip()

    for _ in range(90):
        time.sleep(5)
        got = subprocess.run(
            ["aws", "ssm", "get-command-invocation", "--command-id", cid,
             "--instance-id", INSTANCE_ID,
             "--query", "[Status,StandardOutputContent,StandardErrorContent]",
             "--output", "json"] + env,
            capture_output=True, text=True,
        )
        if got.returncode != 0:
            continue
        status, out, err = json.loads(got.stdout)
        if status in ("Success", "Failed", "Cancelled", "TimedOut"):
            if out:
                print(out.rstrip())
            if status != "Success":
                if err:
                    print(err.rstrip(), file=sys.stderr)
                sys.exit(f"remote command finished as {status}")
            return out
    sys.exit("remote command did not finish in time")


def add(repo: str, token: str, profile: str) -> None:
    q = shlex.quote
    script = f"""
set -euo pipefail
repo={q(repo)}
dir=/home/ghrunner/runners/$repo
tarball=$(ls -1 {TARBALL_GLOB} 2>/dev/null | head -1)
if [ -z "$tarball" ]; then echo "no runner tarball found on the host" >&2; exit 1; fi

install -d -o ghrunner -g ghrunner "$dir"
if [ ! -f "$dir/config.sh" ]; then
  cp "$tarball" "$dir/"
  sudo -u ghrunner tar xzf "$dir/$(basename "$tarball")" -C "$dir"
  printf 'LANG=en_US.UTF-8\\n' > "$dir/.env"
fi
chown -R ghrunner:ghrunner "$dir"

sudo -u ghrunner env HOME=/home/ghrunner "$dir/config.sh" \\
  --url https://github.com/{OWNER}/$repo --token {q(token)} \\
  --name jenkins-box --labels jenkins-box --work _work --unattended --replace \\
  2>&1 | grep -E 'Runner successfully|Settings Saved|Error|error|failed' | head -4

systemctl enable --now "gh-runner@$repo"

for i in $(seq 1 24); do
  if docker logs "gh-runner-$repo" 2>&1 | tail -4 | grep -q 'Listening for Jobs'; then
    echo "LISTENING after $((i*8))s"; break
  fi
  sleep 8
done
docker logs "gh-runner-$repo" 2>&1 | tail -2

m=/sys/fs/cgroup/memory/ghrunners
echo "shared budget: $(( $(cat $m/memory.usage_in_bytes)/1024/1024 ))MB used of $(( $(cat $m/memory.limit_in_bytes)/1024/1024 ))MB across $(ls -d $m/*/ 2>/dev/null | wc -l) runners"
echo "jenkins alive: $(pgrep -fc 'jenkins\\.war' || echo 0)"
"""
    ssm(["cat > /tmp/add.sh <<'EOS'\n" + script + "\nEOS", "bash /tmp/add.sh", "rm -f /tmp/add.sh"], profile)


def remove(repo: str, profile: str) -> None:
    q = shlex.quote
    # No removal token is minted here: --local drops the credentials without contacting
    # GitHub, which leaves an offline entry on the repo's runners page to delete by hand.
    # That is the trade for not needing repo admin.
    script = f"""
set -uo pipefail
repo={q(repo)}
dir=/home/ghrunner/runners/$repo
systemctl disable --now "gh-runner@$repo" 2>&1 | tail -1 || true
if [ -d "$dir" ]; then
  sudo -u ghrunner env HOME=/home/ghrunner "$dir/config.sh" remove --local 2>&1 | tail -2 || true
fi
echo "unit: $(systemctl is-active gh-runner@$repo 2>&1)"
echo "the runner directory is left at $dir; delete it by hand once you are sure"
echo "and remove the now-offline entry from the repository's runners page"
"""
    ssm(["cat > /tmp/rm.sh <<'EOS'\n" + script + "\nEOS", "bash /tmp/rm.sh", "rm -f /tmp/rm.sh"], profile)


def set_variable(repo: str) -> None:
    if not shutil.which("gh"):
        print("gh not found; set RUNNER_LABEL by hand", file=sys.stderr)
        return
    r = subprocess.run(
        ["gh", "api", "-X", "POST", f"/repos/{OWNER}/{repo}/actions/variables",
         "-f", "name=RUNNER_LABEL", "-f", "value=self-hosted"],
        capture_output=True, text=True,
    )
    print("RUNNER_LABEL set" if r.returncode == 0 else f"could not set RUNNER_LABEL: {r.stderr.strip()}")


def main() -> None:
    global INSTANCE_ID
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repo", required=True, help=f"repository name under {OWNER}")
    ap.add_argument("--token", help="registration token from the repo's runners page")
    ap.add_argument("--token-stdin", action="store_true",
                    help="read the token from a prompt, keeping it out of shell history")
    ap.add_argument("--remove", action="store_true", help="deregister instead of register")
    ap.add_argument("--set-variable", action="store_true",
                    help="also set RUNNER_LABEL=self-hosted. Only once the workflow change is merged")
    ap.add_argument("--instance-id", default=INSTANCE_ID,
                    help="EC2 instance running the runners. Defaults to $CI_HOST_INSTANCE_ID")
    ap.add_argument("--profile", default="ans-super", help="AWS profile (default: ans-super)")
    args = ap.parse_args()

    INSTANCE_ID = args.instance_id
    if not INSTANCE_ID:
        sys.exit("set CI_HOST_INSTANCE_ID or pass --instance-id")

    if args.remove:
        remove(args.repo, args.profile)
        return

    token = args.token
    if args.token_stdin or not token:
        token = getpass.getpass(f"registration token for {OWNER}/{args.repo}: ").strip()
    if not token:
        sys.exit("a registration token is required")

    add(args.repo, token, args.profile)

    if args.set_variable:
        set_variable(args.repo)
    else:
        print(f"""
Still to do for {OWNER}/{args.repo}:

  1. Open a pull request setting every job's runner from a variable:

         runs-on: ${{{{ vars.RUNNER_LABEL || 'ubuntu-latest' }}}}

     and, if it calls the shared gates, point them at Answering-IT/github and pass
     `runner` the same way. Check what version it is pinned to first -- moving to a newer
     one can carry a runtime change with it.

  2. Merge it, then set the variable:

         gh api -X POST /repos/{OWNER}/{args.repo}/actions/variables \\
           -f name=RUNNER_LABEL -f value=self-hosted

     Or re-run this with --set-variable. Variables are per repository, not per branch, so
     setting it early makes the pull request's own checks run on the runner -- useful for
     verifying, but it also moves the repo's other jobs at the same moment.
""")


if __name__ == "__main__":
    main()
