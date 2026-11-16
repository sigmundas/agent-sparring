#!/bin/sh
# Issue #5 recovery kit, step 1: record the Stage 1 baseline at d1d48d0. Run as sparring-agent.
# Creates a fresh clone in ~/recovery/baseline-d1d48d0 from the kit bundle and runs the focused Stage 1
# tests and the full suite exactly as committed. It never edits code and never retries a failure.
# It does not touch ~/agent-sparring, ~/remote.git or the original managed worktree.
# Evidence goes to /Users/Shared/sparring-outbox/baseline-d1d48d0-<UTC time>/.
set -u
[ "$(id -un)" = sparring-agent ] || { echo "run as sparring-agent"; exit 1; }
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
KIT=/Users/Shared/sparring-recovery
BUNDLE="$KIT/recovery.bundle"
BASE=d1d48d01def183fdee4fc019cbf7fa1101d1c8bd # Stage 1 endpoint; must resolve exactly
WORK="$HOME/recovery/baseline-d1d48d0"
OUT="/Users/Shared/sparring-outbox/baseline-d1d48d0-$(date -u +%Y%m%dT%H%M%SZ)"
FOCUSED="tests/test_engine_git_authority.py tests/test_push_authorization.py tests/test_managed_finish.py
tests/test_acceptance.py tests/test_accept_advanced_head.py tests/test_sparring_acceptance_concurrency.py
tests/test_finalization.py tests/test_intake_branch.py"
die(){ echo "BASELINE NOT RECORDED: $*"; exit 1; }

sh "$KIT/gate.sh" baseline || die "gate failed; run sh $KIT/gate.sh baseline to see why"
[ -e "$WORK" ] && die "$WORK already exists; remove it to rerun (a rerun is a new, separately recorded baseline)"
git bundle verify "$BUNDLE" >/dev/null 2>&1 || die "bundle does not verify"
mkdir -p "$HOME/recovery" "$OUT" || die "cannot create directories"

git clone -q --no-checkout "$BUNDLE" "$WORK" || die "clone from bundle"
full=$(git -C "$WORK" rev-parse --verify "$BASE^{commit}" 2>/dev/null) || die "$BASE not in bundle"
[ "$full" = "$BASE" ] || die "resolved $full, expected $BASE"
git -C "$WORK" -c core.hooksPath=/dev/null checkout -q --detach "$full" || die "checkout $full"
[ "$(git -C "$WORK" rev-parse HEAD)" = "$full" ] || die "HEAD is not $full"
[ -z "$(git -C "$WORK" status --porcelain)" ] || die "checkout is not clean"

cd "$WORK" || die "cd $WORK"
{
  echo "baseline_commit: $full"
  echo "date_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "user: $(id -un) uid=$(id -u)"
  echo "macos: $(sw_vers -productVersion) build $(sw_vers -buildVersion)"
  echo "kernel: $(uname -mrs)"
  echo "git: $(git --version)"
  echo "uv: $(uv --version 2>&1)"
  echo "claude: $(claude --version 2>&1)"
  echo "codex: $(codex --version 2>&1)"
  echo "env_var_names: $(env | cut -d= -f1 | LC_ALL=C sort | tr '\n' ' ')"
} > "$OUT/ENVIRONMENT.txt"

uv sync --frozen > "$OUT/uv-sync.log" 2>&1; sync_rc=$?
echo "uv_sync_exit: $sync_rc" >> "$OUT/ENVIRONMENT.txt"
[ $sync_rc = 0 ] || die "uv sync --frozen failed (exit $sync_rc); see $OUT/uv-sync.log. No tests were run."
echo "python: $(.venv/bin/python --version 2>&1)" >> "$OUT/ENVIRONMENT.txt"
echo "pytest: $(.venv/bin/python -m pytest --version 2>&1)" >> "$OUT/ENVIRONMENT.txt"

# shellcheck disable=SC2086
.venv/bin/python -m pytest -q -p no:cacheprovider -rfEs --junitxml="$OUT/focused.xml" $FOCUSED > "$OUT/focused.log" 2>&1
focused_rc=$?
.venv/bin/python -m pytest -q -p no:cacheprovider -rfEs --junitxml="$OUT/full.xml" > "$OUT/full.log" 2>&1
full_rc=$?

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

# Credential-pattern scan of everything produced: counts only, never the matched text.
scan=$(cat "$OUT"/*.log "$OUT"/*.xml 2>/dev/null | grep -c -E 'ghp_[A-Za-z0-9]{20}|github_pat_|gho_[A-Za-z0-9]{20}|glpat-|-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}')

{
  echo "Stage 1 baseline at $full ($(date -u +%Y-%m-%dT%H:%M:%SZ)), run by $(id -un). No code was modified."
  echo
  echo "focused: exit $focused_rc  $(counts "$OUT/focused.xml")"
  echo "  command: .venv/bin/python -m pytest -q -p no:cacheprovider -rfEs $(echo $FOCUSED)"
  failed "$OUT/focused.xml"
  echo "full:    exit $full_rc  $(counts "$OUT/full.xml")"
  echo "  command: .venv/bin/python -m pytest -q -p no:cacheprovider -rfEs"
  failed "$OUT/full.xml"
  echo
  echo "credential-pattern lines in outputs: $scan"
} > "$OUT/SUMMARY.txt"
(cd "$OUT" && shasum -a 256 ENVIRONMENT.txt SUMMARY.txt *.log *.xml > SHA256SUMS)
cat "$OUT/SUMMARY.txt"
echo "evidence: $OUT"
