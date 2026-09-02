#!/usr/bin/env bash
# The one check behind notify-discord-deploy/action.yml. The step is shell inside
# a composite action, so nothing else exercises it — a broken quote or an event
# shape nobody thought of shows up as a silent no-message in a deploy channel.
#
# Runs the action's own script (extracted from the YAML, so the test cannot drift
# from what ships) against the four event shapes a deploy actually arrives in,
# pointing the webhook at a closed port. Asserts on the JSON payload it builds.
#
#   ./scripts/test-notify-discord.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

python3 - "$ROOT" "$WORK" <<'PY'
import sys, pathlib, yaml
root, work = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
action = yaml.safe_load((root / "notify-discord-deploy/action.yml").read_text())
step, = action["runs"]["steps"]
(work / "notify.sh").write_text(step["run"])
# Every value the script reads has to come from the step's env block; one that
# does not would work here and be empty in Actions.
missing = [v for v in ("WEBHOOK_URL", "JOB_STATUS", "REPO", "REF_NAME", "SHA", "ACTOR",
                       "STAGE", "REGION", "DETAILS", "RUN_URL", "SERVER_URL")
           if v not in step["env"]]
assert not missing, f"not declared in the action's env: {missing}"
PY

# 0. Every expression in the manifest uses a context a composite action actually
#    has. `secrets` is not one, and GitHub evaluates expressions inside
#    descriptions too, so one used as an example of what to pass fails the whole
#    manifest to load and kills every job in "Set up job" — v1.4.0 shipped
#    exactly that. Invisible to a YAML parse (the file is valid YAML) and only
#    visible on a runner, which is why this is its own check.
python3 - "$ROOT" <<'CONTEXTS'
import sys, pathlib, re
manifest = (pathlib.Path(sys.argv[1]) / "notify-discord-deploy/action.yml").read_text()
ALLOWED = {"inputs", "github", "job", "runner", "steps", "env"}
bad = sorted({m for m in re.findall(r"\$\{\{\s*([A-Za-z_]+)", manifest) if m not in ALLOWED})
assert not bad, ("contexts a composite action manifest cannot use, and GitHub fails the entire "
                 f"manifest over them \u2014 descriptions included: {bad}")
CONTEXTS

SCRIPT="$WORK/notify.sh"
export RUNNER_TEMP="$WORK" SERVER_URL="https://github.com" REPO="Answering-IT/example" \
  SHA="759c59b1234567890abcdef1234567890abcdef1" ACTOR="someone" DETAILS="" \
  RUN_URL="https://github.com/Answering-IT/example/actions/runs/42" \
  WEBHOOK_URL="http://127.0.0.1:1/unreachable"
PAYLOAD="$WORK/discord-deploy.json"

fail() { echo "FAIL: $1" >&2; exit 1; }
check() { jq -e "$1" "$PAYLOAD" >/dev/null || fail "$2"; }

# 1. Merge to main. The commit messages carry a quote, a backtick and a `${{ }}`
#    on purpose: all three have broken this step in some other project.
cat > "$WORK/push.json" <<'J'
{"compare":"https://github.com/Answering-IT/example/compare/aaa...bbb",
 "commits":[{"message":"fix: enmudecía al pedir \"más detalle\"\n\nlong body"},
            {"message":"feat: `route()` and a literal ${{ nothing }}"},
            {"message":"chore: bump"}],
 "head_commit":{"message":"Merge pull request #312 from Answering-IT/fix/algo"}}
J
GITHUB_EVENT_PATH="$WORK/push.json" GITHUB_EVENT_NAME=push GITHUB_REF=refs/heads/main \
  REF_NAME=main JOB_STATUS=success STAGE=dev REGION=us-east-1 bash "$SCRIPT" >/dev/null
check '.embeds[0].title | test("✅") and test("dev \\(us-east-1\\)")' "push: title"
check '.embeds[0].description | test("fix: enmudecía") and test("route\\(\\)") and test("chore: bump")' \
  "push: every commit subject in the body"
check '.embeds[0].description | test("compare/aaa\\.\\.\\.bbb")' "push: compare link"
check '.embeds[0].color == 3066993' "push: green"

# 2. Tag push. `commits` is empty and the payload's compare url is useless
#    (its `before` is all zeroes), so the tag has to supply both.
cat > "$WORK/tag.json" <<'J'
{"compare":"https://github.com/x/compare/000000...bbb","commits":[],
 "head_commit":{"message":"chore: release v1.4.0"}}
J
GITHUB_EVENT_PATH="$WORK/tag.json" GITHUB_EVENT_NAME=push GITHUB_REF=refs/tags/v1.4.0 \
  REF_NAME=v1.4.0 JOB_STATUS=success STAGE=prod REGION=us-east-2 bash "$SCRIPT" >/dev/null
check '.embeds[0].description | test("chore: release v1.4.0") and test("releases/tag/v1.4.0")' \
  "tag: head commit and tag link"
check '.embeds[0].description | test("000000") | not' "tag: no zeroed compare url"

# 3. Label-gated pull request deploy, and a failed one — the case worth a message.
echo '{"pull_request":{"title":"feat: notify deploys","html_url":"https://github.com/x/pull/9"}}' \
  > "$WORK/pr.json"
GITHUB_EVENT_PATH="$WORK/pr.json" GITHUB_EVENT_NAME=pull_request GITHUB_REF=refs/pull/9/merge \
  REF_NAME=9/merge JOB_STATUS=failure STAGE=dev REGION=us-east-1 bash "$SCRIPT" >/dev/null
check '.embeds[0].title | test("❌")' "pull_request: red title on failure"
check '.embeds[0].color == 15158332' "pull_request: red"
check '.embeds[0].description | test("feat: notify deploys") and test("pull/9")' "pull_request: title and link"

# 4. A merge far too big for one embed. Discord rejects an over-long description
#    rather than trimming, and the links must survive the cut.
python3 -c "
import json, sys
print(json.dumps({'commits': [{'message': 'feat: ' + 'x' * 400 + ' #%d' % i} for i in range(40)],
                  'compare': 'https://github.com/x/compare/aaa...bbb'}))" > "$WORK/big.json"
GITHUB_EVENT_PATH="$WORK/big.json" GITHUB_EVENT_NAME=push GITHUB_REF=refs/heads/main \
  REF_NAME=main JOB_STATUS=success STAGE=dev REGION=us-east-1 bash "$SCRIPT" >/dev/null
check '.embeds[0].description | length < 4096' "big: under Discord's description limit"
check '.embeds[0].description | test("compare/aaa\\.\\.\\.bbb") and test("actions/runs/42")' \
  "big: links survive the truncation"

# 5. No secret. Every repository gets this step before every repository holds
#    the webhook, so the empty case has to be a clean no-op.
rm -f "$PAYLOAD"
out=$(GITHUB_EVENT_PATH="$WORK/push.json" GITHUB_EVENT_NAME=push GITHUB_REF=refs/heads/main \
  REF_NAME=main JOB_STATUS=success STAGE=dev REGION=us-east-1 WEBHOOK_URL="" bash "$SCRIPT")
[ -f "$PAYLOAD" ] && fail "no webhook: built a payload anyway"
echo "$out" | grep -q "skipping" || fail "no webhook: no explanation in the log"

echo "notify-discord-deploy: 5 event shapes OK"
