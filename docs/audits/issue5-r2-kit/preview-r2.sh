#!/bin/sh
# Issue #5 Plan R2 kit, step 3: the execution preview. Run as sparring-agent after a PASS preflight.
# Runs the gate, check-plan on the Markdown plan and on the manifest, lists the gates the frozen runner
# will enforce, and prints the manifest sha256 that run-r2.sh requires. Starts nothing. run-plan has
# no dry run, so the manifest sha256 is the confirmation token: it binds exactly what was previewed.
set -eu
[ "$(id -un)" = sparring-agent ] || { echo "run as sparring-agent"; exit 1; }
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
KIT=/Users/Shared/sparring-recovery-r2
R=/Users/Shared/sparring-bootstrap/runner/bin/sparring
RPY=/Users/Shared/sparring-bootstrap/runner/bin/python
PLAN=docs/plans/issue5-r2-complete-and-verify-engine-git-authority.md
MANIFEST=docs/plans/issue5-r2.manifest.json
OUT="/Users/Shared/sparring-outbox/plan-r2-preview-$(date -u +%Y%m%dT%H%M%SZ).txt"
sh "$KIT/gate.sh" run >/dev/null || { echo "gate failed: run sh $KIT/gate.sh run to see why"; exit 1; }
unset SSH_AUTH_SOCK   # the gate checked the account's ssh-agent above; nothing started below may use it
ls /Users/Shared/sparring-outbox/preflight-r2-*/PREFLIGHT.txt >/dev/null 2>&1 || { echo "no preflight recorded"; exit 1; }
cd "$HOME/recovery-r2/agent-sparring"
{
  echo "== gate =="; sh "$KIT/gate.sh" run
  echo; echo "== latest preflight =="; head -1 "$(ls -d /Users/Shared/sparring-outbox/preflight-r2-*/PREFLIGHT.txt | tail -1)"
  echo; echo "== check-plan (Markdown) =="; "$R" check-plan "$PLAN" --repo-root .
  echo; echo "== check-plan (manifest) =="; "$R" check-plan --manifest "$MANIFEST" --repo-root .
  echo; echo "== gates the runner will enforce =="
  "$RPY" -I -c '
import sys
from agent_sparring.manifest import parse_manifest
m = parse_manifest(open(sys.argv[1]).read())
for s in m.stages:
    for g in s.gates_before: print(f"  before {s.stage_id}: {g.id} ({g.kind})")
for g in m.completion_gates: print(f"  before completion: {g.id} ({g.kind})")' "$MANIFEST"
  echo; echo "== manifest =="
  echo "sha256: $(shasum -a 256 "$MANIFEST" | cut -d' ' -f1)"
  echo "kit pin: $(cat "$KIT/MANIFEST_SHA256")"
  echo; echo "== command (not run) =="
  echo "sh $KIT/run-r2.sh $(shasum -a 256 "$MANIFEST" | cut -d' ' -f1)"
} 2>&1 | tee "$OUT"
echo; echo "Saved: $OUT"
echo "Do not run until a person has reviewed this preview."
