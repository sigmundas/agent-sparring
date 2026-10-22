#!/bin/sh
# Issue #5 recovery kit, step 4: test the Plan R result 36e2cea. Run as sparring-agent.
# Creates a fresh, disposable clone in ~/recovery/test-r-36e2cea-<UTC time> from the kit's own copy of the
# Plan R bundle, then runs the eight focused Stage 1 baseline test files and the full suite exactly as
# committed. It never edits code and never retries a failure. It does not touch ~/recovery/agent-sparring,
# ~/recovery/remote.git, ~/agent-sparring or ~/remote.git.
# Evidence goes to /Users/Shared/sparring-outbox/test-r-36e2cea-<UTC time>/.
set -u
[ "$(id -un)" = sparring-agent ] || { echo "run as sparring-agent"; exit 1; }
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
KIT=/Users/Shared/sparring-recovery
# The kit's owner-owned copy, not the outbox copy that sparring-agent wrote and could still change.
BUNDLE="$KIT/plan-r-36e2cea.bundle"
BUNDLE_SHA256=8df1e75b89723cd566e2f29e7edd7c9fc379f18d2231d8fc32117b66290b5e38
BUNDLE_REF=refs/heads/plan/issue5-recovery
CAND=36e2cea12c0e301b94ce2e7528030977530d40de # Plan R end; must resolve exactly
BASE=2ba8d59b9daa6fcd4db11bfd560224f8ddd117cc # Plan R start; must be an ancestor
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
WORK="$HOME/recovery/test-r-36e2cea-$STAMP"
OUT="/Users/Shared/sparring-outbox/test-r-36e2cea-$STAMP"
# The same eight files, in the same order, as the Stage 1 baseline (baseline.sh).
FOCUSED="tests/test_engine_git_authority.py tests/test_push_authorization.py tests/test_managed_finish.py
tests/test_acceptance.py tests/test_accept_advanced_head.py tests/test_sparring_acceptance_concurrency.py
tests/test_finalization.py tests/test_intake_branch.py"
# Added by Plan R. They run in the full suite; their counts are reported separately.
PLAN_R_NEW="tests/test_no_live_remote_config.py tests/test_fail_closed_pins.py"
die(){ echo "RESULTS NOT RECORDED: $*"; exit 1; }

# The gate runs before any candidate code exists on disk, unchanged, in the same mode as the baseline.
sh "$KIT/gate.sh" baseline || die "gate failed; run sh $KIT/gate.sh baseline to see why"
[ -e "$WORK" ] && die "$WORK already exists"
[ -e "$OUT" ] && die "$OUT already exists"
[ -f "$BUNDLE" ] || die "$BUNDLE missing"
if touch "$BUNDLE" 2>/dev/null; then die "$BUNDLE is writable by $(id -un)"; fi
got=$(shasum -a 256 "$BUNDLE" | cut -d' ' -f1)
[ "$got" = "$BUNDLE_SHA256" ] || die "bundle sha256 is $got, expected $BUNDLE_SHA256"
git bundle verify "$BUNDLE" >/dev/null 2>&1 || die "bundle does not verify"
heads=$(git bundle list-heads "$BUNDLE")
[ "$heads" = "$CAND $BUNDLE_REF" ] || die "bundle heads are not exactly '$CAND $BUNDLE_REF'"
mkdir -p "$HOME/recovery" "$OUT" || die "cannot create directories"

git -c transfer.fsckObjects=true clone -q --no-checkout "$BUNDLE" "$WORK" || die "clone from bundle"
full=$(git -C "$WORK" rev-parse --verify "$CAND^{commit}" 2>/dev/null) || die "$CAND not in bundle"
[ "$full" = "$CAND" ] || die "resolved $full, expected $CAND"
git -C "$WORK" merge-base --is-ancestor "$BASE" "$full" || die "$BASE is not an ancestor of $full"
git -C "$WORK" remote remove origin || die "remove origin"   # the disposable clone has no remote at all
git -C "$WORK" -c core.hooksPath=/dev/null checkout -q --detach "$full" || die "checkout $full"
[ "$(git -C "$WORK" rev-parse HEAD)" = "$full" ] || die "HEAD is not $full"
[ -z "$(git -C "$WORK" status --porcelain)" ] || die "checkout is not clean"
[ -z "$(git -C "$WORK" remote)" ] || die "the clone still has a remote"

cd "$WORK" || die "cd $WORK"
{
  echo "candidate_commit: $full"
  echo "base_commit: $BASE"
  echo "bundle: $BUNDLE sha256=$got"
  echo "checkout: $WORK"
  echo "date_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "user: $(id -un) uid=$(id -u)"
  echo "macos: $(sw_vers -productVersion) build $(sw_vers -buildVersion)"
  echo "kernel: $(uname -mrs)"
  echo "git: $(git --version)"
  echo "uv: $(uv --version 2>&1)"
  echo "claude: $(claude --version 2>&1)"
  echo "codex: $(codex --version 2>&1)"
  echo "uv_lock_sha256: $(shasum -a 256 uv.lock | cut -d' ' -f1)"
  echo "pyproject_sha256: $(shasum -a 256 pyproject.toml | cut -d' ' -f1)"
  echo "env_var_names: $(env | cut -d= -f1 | LC_ALL=C sort | tr '\n' ' ')"
} > "$OUT/ENVIRONMENT.txt"

uv sync --frozen > "$OUT/uv-sync.log" 2>&1; sync_rc=$?
echo "uv_sync_exit: $sync_rc" >> "$OUT/ENVIRONMENT.txt"
[ $sync_rc = 0 ] || die "uv sync --frozen failed (exit $sync_rc); see $OUT/uv-sync.log. No tests were run."
[ -z "$(git status --porcelain)" ] || die "checkout is dirty after uv sync. No tests were run."
echo "python: $(.venv/bin/python --version 2>&1)" >> "$OUT/ENVIRONMENT.txt"
echo "pytest: $(.venv/bin/python -m pytest --version 2>&1)" >> "$OUT/ENVIRONMENT.txt"
uv pip list --python .venv/bin/python > "$OUT/packages.txt" 2>&1 || echo "uv pip list failed" >> "$OUT/packages.txt"
for f in $FOCUSED $PLAN_R_NEW; do [ -f "$f" ] || die "test file $f missing at $full. No tests were run."; done

# One run each. A failure is recorded, never retried.
# shellcheck disable=SC2086
.venv/bin/python -m pytest -q -p no:cacheprovider -rfEs --junitxml="$OUT/focused.xml" $FOCUSED > "$OUT/focused.log" 2>&1
focused_rc=$?
.venv/bin/python -m pytest -q -p no:cacheprovider -rfEs --junitxml="$OUT/full.xml" > "$OUT/full.log" 2>&1
full_rc=$?

# The tests must not have changed tracked files or moved HEAD. Untracked output is listed, not judged.
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
# Counts for one module within full.xml, matched on the junit classname prefix.
module(){ /usr/bin/python3 -I -c '
import sys, xml.etree.ElementTree as ET
mod = sys.argv[2][:-3].replace("/", ".")
n = f = e = s = 0
for c in ET.parse(sys.argv[1]).iter("testcase"):
    cn = c.get("classname", "")
    if cn == mod or cn.startswith(mod + "."):
        n += 1
        f += c.find("failure") is not None
        e += c.find("error") is not None
        s += c.find("skipped") is not None
print(f"tests={n} failures={f} errors={e} skipped={s}")
' "$1" "$2" 2>/dev/null || echo "unparseable junit xml"; }

# Credential-pattern scan of everything produced: counts only, never the matched text.
scan=$(cat "$OUT"/*.log "$OUT"/*.xml "$OUT"/*.txt 2>/dev/null | grep -c -E 'ghp_[A-Za-z0-9]{20}|github_pat_|gho_[A-Za-z0-9]{20}|glpat-|-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}')
baseline=$(ls -d /Users/Shared/sparring-outbox/baseline-d1d48d0-*/SUMMARY.txt 2>/dev/null | tail -1)

{
  echo "Plan R tests at $full ($(date -u +%Y-%m-%dT%H:%M:%SZ)), run by $(id -un). No code was modified."
  echo
  echo "focused: exit $focused_rc  $(counts "$OUT/focused.xml")"
  echo "  command: .venv/bin/python -m pytest -q -p no:cacheprovider -rfEs $(echo $FOCUSED)"
  failed "$OUT/focused.xml"
  echo "full:    exit $full_rc  $(counts "$OUT/full.xml")"
  echo "  command: .venv/bin/python -m pytest -q -p no:cacheprovider -rfEs"
  failed "$OUT/full.xml"
  echo
  echo "Plan R's new test files, within the full run:"
  for f in $PLAN_R_NEW; do echo "  $f: $(module "$OUT/full.xml" "$f")"; done
  echo
  echo "after the runs: HEAD $head_after, tracked files changed: $tracked_changed (see status-after.txt)"
  echo "credential-pattern lines in outputs: $scan"
  echo "Stage 1 baseline for comparison: ${baseline:-none found}"
} > "$OUT/SUMMARY.txt"
(cd "$OUT" && shasum -a 256 ENVIRONMENT.txt SUMMARY.txt packages.txt status-after.txt *.log *.xml > SHA256SUMS)
cat "$OUT/SUMMARY.txt"
echo "evidence: $OUT"
[ $focused_rc = 0 ] && [ $full_rc = 0 ] && [ "$head_after" = "$full" ] && [ "$tracked_changed" = 0 ]
