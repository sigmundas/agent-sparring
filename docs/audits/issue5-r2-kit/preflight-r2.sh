#!/bin/sh
# Issue #5 Plan R2 kit, step 2: prove the implementation environment can run the tests before any run.
# Run as sparring-agent after setup-r2.sh. Starts no Agent Sparring run.
# 1. A linked worktree like the runner's managed worktree (no .venv) is created from the working copy.
# 2. Claude is started exactly as the frozen runner starts a stage agent (-p, stream-json, --verbose,
#    --permission-mode acceptEdits) and asked to run uv sync and one real-Git test file with a junit
#    path this script chose. The file takes about 310 s, longer than Claude's default Bash timeout, so a
#    complete junit also proves the raised timeout from setup-r2.sh took effect.
# 3. The same file is run directly, and the counts must agree.
# PASS needs: Claude's junit exists, was written after this script started, and reports the same test
# count as the direct run; the direct pytest exit is 0 or 1 (tests ran). Failures are recorded, not
# judged: this proves the environment, not the code.
set -u
[ "$(id -un)" = sparring-agent ] || { echo "run as sparring-agent"; exit 1; }
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
KIT=/Users/Shared/sparring-recovery-r2
PLAN_SHA=$(cat "$KIT/PLAN_SHA")
REPO="$HOME/recovery-r2/agent-sparring"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
PROBE="$HOME/recovery-r2/preflight-$STAMP"
OUT="/Users/Shared/sparring-outbox/preflight-r2-$STAMP"
FILE=tests/test_managed_finish.py
die(){ echo "PREFLIGHT NOT RECORDED: $*"; exit 1; }

sh "$KIT/gate.sh" run || die "gate failed"
unset SSH_AUTH_SOCK   # the gate checked the account's ssh-agent above; nothing started below may use it
mkdir -p "$OUT" || die "cannot create $OUT"
: > "$OUT/.start"
git -C "$REPO" worktree add -q --detach "$PROBE" "$PLAN_SHA" || die "probe worktree"
cleanup(){ git -C "$REPO" worktree remove --force "$PROBE" >/dev/null 2>&1; git -C "$REPO" worktree prune; }
trap cleanup EXIT
[ ! -e "$PROBE/.venv" ] || die "probe worktree unexpectedly has a .venv"

PROMPT="This is an environment check for an isolated test setup. Do not edit or create any file in this repository. In the current directory, run exactly these two shell commands, one after the other, and then reply with each command's exit code:
1. uv sync --frozen
2. .venv/bin/python -m pytest -q -p no:cacheprovider $FILE --junitxml=$OUT/claude.xml"
(cd "$PROBE" && claude -p "$PROMPT" --output-format stream-json --verbose --permission-mode acceptEdits) \
  > "$OUT/claude-stream.jsonl" 2> "$OUT/claude-stderr.log"
claude_rc=$?

cd "$PROBE" || die "cd $PROBE"
[ -x .venv/bin/python ] || uv sync --frozen > "$OUT/direct-sync.log" 2>&1
(.venv/bin/python -m pytest -q -p no:cacheprovider "$FILE" --junitxml="$OUT/direct.xml") \
  > "$OUT/direct.log" 2>&1
direct_rc=$?
status_after=$(git -C "$PROBE" status --porcelain --untracked-files=no)

tests_in(){ /usr/bin/python3 -I -c '
import sys, xml.etree.ElementTree as ET
r = ET.parse(sys.argv[1]).getroot(); s = r if r.tag == "testsuite" else r.find("testsuite")
print(s.get("tests"), s.get("failures"), s.get("errors"))' "$1" 2>/dev/null || echo "none"; }
claude_counts=$(tests_in "$OUT/claude.xml")
direct_counts=$(tests_in "$OUT/direct.xml")
claude_result_flag=$(grep -o '"is_error":[a-z]*' "$OUT/claude-stream.jsonl" | tail -1)
fresh=no; [ -f "$OUT/claude.xml" ] && [ "$OUT/claude.xml" -nt "$OUT/.start" ] && fresh=yes
scan=$(cat "$OUT"/*.log "$OUT"/*.jsonl "$OUT"/*.xml 2>/dev/null | grep -c -E 'ghp_[A-Za-z0-9]{20}|github_pat_|gho_[A-Za-z0-9]{20}|glpat-|-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}')

verdict=FAIL
if [ "$fresh" = yes ] && [ "$claude_counts" != none ] && [ "${claude_counts%% *}" = "${direct_counts%% *}" ] \
   && [ "${claude_counts%% *}" -gt 0 ] 2>/dev/null && { [ $direct_rc = 0 ] || [ $direct_rc = 1 ]; } \
   && [ -z "$status_after" ]; then verdict=PASS; fi
{
  echo "Plan R2 preflight at $PLAN_SHA ($(date -u +%Y-%m-%dT%H:%M:%SZ)), run by $(id -un): $verdict"
  echo "claude: $(claude --version 2>&1), exit $claude_rc, last result flag $claude_result_flag"
  echo "claude junit: written after start: $fresh; tests failures errors: $claude_counts"
  echo "direct junit: pytest exit $direct_rc; tests failures errors: $direct_counts"
  echo "tracked files changed in the probe worktree: $([ -z "$status_after" ] && echo 0 || echo "$status_after" | wc -l | tr -d ' ')"
  echo "credential-pattern lines in outputs: $scan"
  echo "The reviewer (Codex, read-only sandbox) is not expected to run these tests; the human gates are."
} > "$OUT/PREFLIGHT.txt"
rm -f "$OUT/.start"
(cd "$OUT" && shasum -a 256 PREFLIGHT.txt *.log *.jsonl *.xml > SHA256SUMS 2>/dev/null)
cat "$OUT/PREFLIGHT.txt"
echo "evidence: $OUT"
[ "$verdict" = PASS ]
