#!/bin/sh
# Issue #5 Plan R2 kit, step 4: START Plan R2. Only after a person has reviewed the preview.
# Usage: sh run-r2.sh <manifest sha256 printed by preview-r2.sh>
# The run stops on its own before Stage R2.2, before Stage R2.3 and before completion, at the
# manifest's human gates (an isolated test, then a go-ahead). Continue with resume-r2.sh.
set -eu
[ "$(id -un)" = sparring-agent ] || { echo "run as sparring-agent"; exit 1; }
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
KIT=/Users/Shared/sparring-recovery-r2
R=/Users/Shared/sparring-bootstrap/runner/bin/sparring
MANIFEST=docs/plans/issue5-r2.manifest.json
WANT="${1:-}"
[ -n "$WANT" ] || { echo "usage: sh run-r2.sh <manifest sha256 from preview-r2.sh>"; exit 2; }
sh "$KIT/gate.sh" run >/dev/null || { echo "gate failed: run sh $KIT/gate.sh run to see why"; exit 1; }
unset SSH_AUTH_SOCK   # the gate checked the account's ssh-agent above; nothing started below may use it
cd "$HOME/recovery-r2/agent-sparring"
got=$(shasum -a 256 "$MANIFEST" | cut -d' ' -f1)
[ "$got" = "$WANT" ] && [ "$got" = "$(cat "$KIT/MANIFEST_SHA256")" ] || { echo "manifest sha256 $got does not match the argument and the kit pin"; exit 1; }
grep -q ': PASS$' "$(ls -d /Users/Shared/sparring-outbox/preflight-r2-*/PREFLIGHT.txt | tail -1)" || { echo "the latest preflight did not PASS"; exit 1; }
"$R" run-plan --manifest "$MANIFEST" --managed --target-branch plan/issue5-r2 --allow-push-for-run \
  --max-send-back-cycles 3 \
  --claude-executable "$HOME/.local/bin/claude" --codex-executable "$HOME/.local/bin/codex"
