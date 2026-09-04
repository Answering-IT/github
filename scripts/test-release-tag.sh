#!/usr/bin/env bash
# The one check behind release-tag-reusable.yml. The parse is a regex table in
# shell, and the thing it decides is whether a push goes to stg or to prod — a
# loosened anchor there sends `v1.4.0-rc1-hotfix` to production and nothing else
# in CI would notice.
#
# Runs the workflow's own script (extracted from the YAML, so the test cannot
# drift from what ships) over the shapes that matter.
#
#   ./scripts/test-release-tag.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

python3 - "$ROOT" "$WORK" <<'PY'
import sys, pathlib, yaml
root, work = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
wf = yaml.safe_load((root / ".github/workflows/release-tag-reusable.yml").read_text())
step, = [s for s in wf["jobs"]["resolve"]["steps"] if s.get("id") == "parse"]
(work / "parse.sh").write_text(step["run"])
assert "INPUT_TAG" in step["env"], "INPUT_TAG is not in the step's env; it would be empty in Actions"
PY

SCRIPT="$WORK/parse.sh"
fail() { echo "FAIL: $1" >&2; exit 1; }

# $1 expected stage or the word `reject`, $2 INPUT_TAG, $3 GITHUB_REF, $4 ALLOW_STG
expect() {
  local want=$1 tag=$2 ref=${3:-refs/heads/main} allow=${4:-true} out
  : > "$WORK/out"
  if out=$(cd "$ROOT" && GITHUB_OUTPUT="$WORK/out" INPUT_TAG="$tag" GITHUB_REF="$ref" \
             ALLOW_STG="$allow" bash "$SCRIPT" 2>&1); then
    [ "$want" = reject ] && fail "'${tag:-$ref}' was accepted as $(grep '^stage=' "$WORK/out")"
    grep -qx "stage=$want" "$WORK/out" || fail "'${tag:-$ref}' -> $(grep '^stage=' "$WORK/out"), wanted $want"
    grep -qx "version=${tag:-${ref#refs/tags/}}" "$WORK/out" || fail "'${tag:-$ref}': version not echoed back"
    grep -q '^sha=[0-9a-f]\{40\}$' "$WORK/out" || fail "'${tag:-$ref}': no commit sha"
  else
    [ "$want" = reject ] || fail "'${tag:-$ref}' was rejected: $out"
    echo "$out" | grep -q '^::error::' || fail "'${tag:-$ref}': rejected without an ::error:: annotation"
  fi
}

# 1. The two shapes that deploy, dispatched (rollback) and pushed (release).
expect prod  v1.4.0
expect stg   v1.4.0-rc2
expect prod  ''  refs/tags/v1.4.0
expect stg   ''  refs/tags/v1.4.0-rc2

# 2. Anchors. Each of these is a production deploy if the regex is loosened at
#    one end, and every one of them is a plausible typo or branch name.
expect reject v1.4
expect reject release-1.4.0
expect reject 1.4.0
expect reject v1.4.0-rc
expect reject v1.4.0-rc1-hotfix
expect reject xv1.4.0
expect reject v1.4.0x

# 3. A dispatch with no tag. GITHUB_REF is then the branch, and the only safe
#    reading of that is "refused" — never "deploy main to prod".
expect reject '' refs/heads/main

# 4. A service with only dev and prod. An rc tag there has nowhere to go, and
#    the two failures worth preventing are a tag that deploys nothing and a tag
#    that falls through to prod. Plain releases must be unaffected.
expect reject v1.4.0-rc2 refs/heads/main false
expect reject ''         refs/tags/v1.4.0-rc2 false
expect prod   v1.4.0     refs/heads/main      false

# 5. `allow_stg` unset entirely. `set -u` aborts only the command that reads an
#    unset name, so a missing default here would be a silent empty string, not a
#    failure -- which is how notify-discord-deploy shipped an empty embed.
out=$(cd "$ROOT" && GITHUB_OUTPUT="$WORK/out" INPUT_TAG=v1.4.0-rc2 GITHUB_REF=refs/heads/main \
        bash "$SCRIPT" 2>&1) || fail "unset ALLOW_STG: rejected an rc tag; the default is meant to be stg"
grep -qx "stage=stg" "$WORK/out" || fail "unset ALLOW_STG: did not default to stg"

echo "release-tag-reusable: 17 tag shapes OK"
