#!/bin/sh
# Issue #5 Plan L kit: test one exact Plan L candidate. Run as sparring-agent, while no run is active.
# Usage: sh test-l.sh <full 40-hex commit sha> <accepted stage 1-5> [--require-ids FILE]
#   accepted stage: the Plan L stage whose accepted candidate this is (5 for the final gate). The kit's
#   black-box probes for stages 1..N run against it (probe-l.py).
#   --require-ids: a file of junit test ids (classname::name, one per line) from the handoff's
#   traceability table; each must be present and passing in the full run.
# Derived from issue5-recovery-kit/test-r.sh, with the same reporting. The candidate comes from the run's
# local remote (~/recovery-l/remote.git), fetched with fsck into a fresh disposable clone with no remote.
# It must descend from the Plan L base. The focused set is the eight Stage 1 baseline files plus Plan R's
# two new files; then the full suite. One run each; a failure is recorded, never retried; no code is
# edited. Dependencies come from uv.lock into a fresh uv cache, so nothing cached by an earlier agent
# turn is reused.
# Evidence: /Users/Shared/sparring-outbox/test-l-<short sha>-<UTC time>/. Copy it to the owner's account
# before answering a gate: this account can write the outbox.
set -u
[ "$(id -un)" = sparring-agent ] || { echo "run as sparring-agent"; exit 1; }
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
KIT=/Users/Shared/sparring-recovery-l
SOURCE="$HOME/recovery-l/remote.git"
BASE=$(cat "$KIT/PLAN_SHA") # Plan L base; must be an ancestor
CAND="${1:-}"
STAGE="${2:-}"
IDS=""
[ "${3:-}" = --require-ids ] && IDS="${4:-}"
usage(){ echo "usage: sh test-l.sh <full 40-hex commit sha> <accepted stage 1-5> [--require-ids FILE]"; exit 2; }
echo "$CAND" | grep -Eq '^[0-9a-f]{40}$' || usage
echo "$STAGE" | grep -Eq '^[1-5]$' || usage
if [ -n "$IDS" ]; then [ -f "$IDS" ] || { echo "--require-ids file $IDS missing"; exit 2; }; IDS=$(cd "$(dirname "$IDS")" && pwd)/$(basename "$IDS"); fi
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
SHORT=$(echo "$CAND" | cut -c1-7)
WORK="$HOME/recovery-l/test-l-$SHORT-$STAMP"
OUT="/Users/Shared/sparring-outbox/test-l-$SHORT-$STAMP"
FOCUSED="tests/test_engine_git_authority.py tests/test_push_authorization.py tests/test_managed_finish.py
tests/test_acceptance.py tests/test_accept_advanced_head.py tests/test_sparring_acceptance_concurrency.py
tests/test_finalization.py tests/test_intake_branch.py
tests/test_no_live_remote_config.py tests/test_fail_closed_pins.py"
die(){ echo "RESULTS NOT RECORDED: $*"; exit 1; }

sh "$KIT/gate.sh" baseline || die "gate failed; run sh $KIT/gate.sh baseline to see why"
unset SSH_AUTH_SOCK   # the gate checked the account's ssh-agent above; nothing started below may use it
# A test must not overlap a live run: no agent may be changing anything while it runs.
if pgrep -u "$(id -u)" -f 'claude -p|codex exec|sparring (run|resume)-plan' >/dev/null 2>&1; then
  die "a provider or run process of $(id -un) is still running; wait for the run to stop"
fi
[ -e "$WORK" ] && die "$WORK already exists"
[ -e "$OUT" ] && die "$OUT already exists"
[ -d "$SOURCE" ] || die "$SOURCE missing"
mkdir -p "$OUT" || die "cannot create $OUT"

git -c transfer.fsckObjects=true clone -q --no-local --no-checkout "file://$SOURCE" "$WORK" || die "clone"
full=$(git -C "$WORK" rev-parse --verify "$CAND^{commit}" 2>/dev/null) || die "$CAND not on the local remote"
[ "$full" = "$CAND" ] || die "resolved $full, expected $CAND"
git -C "$WORK" merge-base --is-ancestor "$BASE" "$full" || die "$BASE is not an ancestor of $full"
git -C "$WORK" remote remove origin || die "remove origin"
git -C "$WORK" -c core.hooksPath=/dev/null checkout -q --detach "$full" || die "checkout $full"
[ "$(git -C "$WORK" rev-parse HEAD)" = "$full" ] || die "HEAD is not $full"
[ -z "$(git -C "$WORK" status --porcelain)" ] || die "checkout is not clean"
[ -z "$(git -C "$WORK" remote)" ] || die "the clone still has a remote"

cd "$WORK" || die "cd $WORK"
export UV_CACHE_DIR="$WORK.uv-cache" PYTHONNOUSERSITE=1
{
  echo "candidate_commit: $full"
  echo "base_commit: $BASE"
  echo "source: $SOURCE"
  echo "checkout: $WORK"
  echo "date_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "user: $(id -un) uid=$(id -u)"
  echo "macos: $(sw_vers -productVersion) build $(sw_vers -buildVersion)"
  echo "kernel: $(uname -mrs)"
  echo "git: $(git --version)"
  echo "uv: $(uv --version 2>&1)"
  echo "uv_cache_dir: $UV_CACHE_DIR (fresh)"
  echo "uv_lock_sha256: $(shasum -a 256 uv.lock | cut -d' ' -f1)"
  echo "pyproject_sha256: $(shasum -a 256 pyproject.toml | cut -d' ' -f1)"
  echo "env_var_names: $(env | cut -d= -f1 | LC_ALL=C sort | tr '\n' ' ')"
} > "$OUT/ENVIRONMENT.txt"

uv sync --frozen > "$OUT/uv-sync.log" 2>&1; sync_rc=$?
echo "uv_sync_exit: $sync_rc" >> "$OUT/ENVIRONMENT.txt"
[ $sync_rc = 0 ] || die "uv sync --frozen failed (exit $sync_rc); see $OUT/uv-sync.log. No tests were run."
[ -z "$(git status --porcelain)" ] || die "checkout is dirty after uv sync. No tests were run."
interp=$(.venv/bin/python -c 'import os, sys; print(os.path.realpath(sys.executable))' 2>/dev/null)
echo "python: $(.venv/bin/python --version 2>&1) at $interp sha256=$(shasum -a 256 "$interp" 2>/dev/null | cut -d' ' -f1)" >> "$OUT/ENVIRONMENT.txt"
echo "pytest: $(.venv/bin/python -m pytest --version 2>&1)" >> "$OUT/ENVIRONMENT.txt"
uv pip list --python .venv/bin/python > "$OUT/packages.txt" 2>&1 || echo "uv pip list failed" >> "$OUT/packages.txt"
for f in $FOCUSED; do [ -f "$f" ] || die "test file $f missing at $full. No tests were run."; done

# shellcheck disable=SC2086
.venv/bin/python -m pytest -q -p no:cacheprovider -rfEs --junitxml="$OUT/focused.xml" $FOCUSED > "$OUT/focused.log" 2>&1
focused_rc=$?
.venv/bin/python -m pytest -q -p no:cacheprovider -rfEs --junitxml="$OUT/full.xml" > "$OUT/full.log" 2>&1
full_rc=$?

# The kit's own black-box probes for stages 1..STAGE, with fake providers in disposable repositories.
# Legacy run state for the migration probes is made by the frozen pre-Plan L runner.
mkdir -p "$WORK.probe-tmp"
TMPDIR="$WORK.probe-tmp" /usr/bin/python3 -I "$KIT/probe-l.py" --engine "$WORK/.venv/bin/sparring" \
  --legacy-engine /Users/Shared/sparring-bootstrap/runner/bin/sparring \
  --candidate-python "$WORK/.venv/bin/python" --candidate-src "$WORK/src" \
  --accepted-stage "$STAGE" --out "$OUT" > "$OUT/probes.log" 2>&1
probes_rc=$?
# Traceability: every id the handoff names must have run and passed in the full suite.
required_missing=0
if [ -n "$IDS" ]; then
  /usr/bin/python3 -I - "$OUT/full.xml" "$IDS" > "$OUT/required-ids.txt" 2>&1 <<'EOF2'
import sys, xml.etree.ElementTree as ET
passed = {c.get("classname", "") + "::" + c.get("name", "") for c in ET.parse(sys.argv[1]).iter("testcase")
          if c.find("failure") is None and c.find("error") is None and c.find("skipped") is None}
want = [l.strip() for l in open(sys.argv[2]) if l.strip() and not l.startswith("#")]
for w in want:
    print(("ok      " if w in passed else "MISSING ") + w)
EOF2
  required_missing=$(grep -c '^MISSING' "$OUT/required-ids.txt")
fi
git status --porcelain --untracked-files=all > "$OUT/status-after.txt" 2>&1
tracked_changed=$(grep -c -v '^??' "$OUT/status-after.txt")
head_after=$(git rev-parse HEAD)

counts(){ /usr/bin/python3 -I -c '
import sys, xml.etree.ElementTree as ET
r = ET.parse(sys.argv[1]).getroot()
s = r if r.tag == "testsuite" else r.find("testsuite")
print(" ".join(f"{k}={s.get(k)}" for k in ("tests", "failures", "errors", "skipped", "time")))
' "$1" 2>/dev/null || echo "unparseable junit xml"; }
failed(){ /usr/bin/python3 -I -c '
import sys, xml.etree.ElementTree as ET
for c in ET.parse(sys.argv[1]).iter("testcase"):
    if c.find("failure") is not None or c.find("error") is not None:
        print("  " + c.get("classname", "") + "::" + c.get("name", ""))
' "$1" 2>/dev/null; }

scan=$(cat "$OUT"/*.log "$OUT"/*.xml "$OUT"/*.txt 2>/dev/null | grep -c -E 'ghp_[A-Za-z0-9]{20}|github_pat_|gho_[A-Za-z0-9]{20}|glpat-|-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}')
ok=no; [ $focused_rc = 0 ] && [ $full_rc = 0 ] && [ "$head_after" = "$full" ] && [ "$tracked_changed" = 0 ] \
  && [ $probes_rc = 0 ] && [ "$required_missing" = 0 ] && ok=yes

# Weakening flags (plan, "No weakening"). Statically checkable, so a person answers the gate only after
# matching every flag against the handoff's list of replaced assertions and renamed tests.
PLAN_R_FULL="/Users/Shared/sparring-recovery-r2/evidence/test-r-36e2cea-20261022T174121Z/full.xml"
git diff -U0 "$BASE" "$full" -- tests/ > "$OUT/tests.diff" 2>&1
added_skips=$(grep -E '^\+' "$OUT/tests.diff" | grep -v '^+++' | grep -c -E 'skip(If|Unless)?\(|pytest\.skip|unittest\.skip|xfail|expectedFailure')
removed_asserts=$(grep -E '^-' "$OUT/tests.diff" | grep -v '^---' | grep -c -E '\bassert|self\.(assert|fail)')
added_asserts=$(grep -E '^\+' "$OUT/tests.diff" | grep -v '^+++' | grep -c -E '\bassert|self\.(assert|fail)')
/usr/bin/python3 -I - "$PLAN_R_FULL" "$OUT/full.xml" > "$OUT/removed-tests.txt" 2>&1 <<'EOF'
import sys, xml.etree.ElementTree as ET
ids = lambda p: {c.get("classname", "") + "::" + c.get("name", "") for c in ET.parse(p).iter("testcase")}
before, after = ids(sys.argv[1]), ids(sys.argv[2])
for t in sorted(before - after): print(t)
EOF
removed_tests=$(grep -c '::' "$OUT/removed-tests.txt")
full_count=$(/usr/bin/python3 -I -c '
import sys, xml.etree.ElementTree as ET
r = ET.parse(sys.argv[1]).getroot(); s = r if r.tag == "testsuite" else r.find("testsuite"); print(s.get("tests"))' "$OUT/full.xml" 2>/dev/null || echo 0)
flags=0
[ "$full_count" -ge 2071 ] 2>/dev/null || flags=$((flags+1))
[ "$added_skips" = 0 ] || flags=$((flags+1))
[ "$removed_asserts" = 0 ] || flags=$((flags+1))
[ "$removed_tests" = 0 ] || flags=$((flags+1))
answer="fail (do not answer pass)"
[ $ok = yes ] && answer=pass
[ $ok = yes ] && [ $flags != 0 ] && answer="pass only after every weakening flag below matches the handoff"

{
  echo "Plan L tests at $full ($(date -u +%Y-%m-%dT%H:%M:%SZ)), run by $(id -un). No code was modified."
  echo "gate answer this supports: $answer"
  echo
  echo "focused: exit $focused_rc  $(counts "$OUT/focused.xml")"
  echo "  command: .venv/bin/python -m pytest -q -p no:cacheprovider -rfEs $(echo $FOCUSED)"
  failed "$OUT/focused.xml"
  echo "full:    exit $full_rc  $(counts "$OUT/full.xml")"
  echo "  command: .venv/bin/python -m pytest -q -p no:cacheprovider -rfEs"
  failed "$OUT/full.xml"
  echo
  echo "kit probes for stages 1..$STAGE: exit $probes_rc ($(head -1 "$OUT/PROBES.txt" 2>/dev/null || echo 'no report'))"
  grep -E '^[A-Z0-9]+-[a-z-]+ \(' "$OUT/PROBES.txt" 2>/dev/null | sed 's/^/  /'
  if [ -n "$IDS" ]; then echo "handoff test ids: $required_missing missing or failing (see required-ids.txt)"; else echo "handoff test ids: none given"; fi
  echo
  echo "after the runs: HEAD $head_after, tracked files changed: $tracked_changed (see status-after.txt)"
  echo "credential-pattern lines in outputs: $scan"
  echo
  echo "weakening flags since $BASE (tests/ only; see tests.diff): $flags"
  echo "  full-suite tests: $full_count (Plan R at 36e2cea: 2071)"
  echo "  added skip/xfail markers: $added_skips"
  echo "  removed assertion lines: $removed_asserts (added: $added_asserts)"
  echo "  test ids present at 36e2cea and missing now: $removed_tests (see removed-tests.txt)"
} > "$OUT/SUMMARY.txt"
(cd "$OUT" && shasum -a 256 ENVIRONMENT.txt SUMMARY.txt PROBES.txt packages.txt status-after.txt removed-tests.txt tests.diff *.log *.xml $([ -f required-ids.txt ] && echo required-ids.txt) > SHA256SUMS)
cat "$OUT/SUMMARY.txt"
echo "evidence: $OUT"
[ $ok = yes ] && [ $flags = 0 ]
