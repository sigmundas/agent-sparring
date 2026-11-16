#!/bin/sh
# Issue #5 Plan R2 kit: continue a stopped Plan R2 run. Run as sparring-agent.
# Usage: sh resume-r2.sh --run-key <key> [--deferred-result <instance>:<gate id>=pass|fail|blocked] [--evidence "..."]
# Answer an isolated-test gate "pass" only after test-r2.sh passed at the exact accepted SHA and its
# evidence was copied to the owner's account. The credential gate runs first; the run-mode layout
# checks do not apply once a run has started.
set -eu
[ "$(id -un)" = sparring-agent ] || { echo "run as sparring-agent"; exit 1; }
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
KIT=/Users/Shared/sparring-recovery-r2
R=/Users/Shared/sparring-bootstrap/runner/bin/sparring
MANIFEST=docs/plans/issue5-r2.manifest.json
[ $# -gt 0 ] || { echo "usage: sh resume-r2.sh --run-key <key> [--deferred-result ...] [--evidence ...]"; exit 2; }
sh "$KIT/gate.sh" baseline >/dev/null || { echo "gate failed: run sh $KIT/gate.sh baseline to see why"; exit 1; }
unset SSH_AUTH_SOCK   # the gate checked the account's ssh-agent above; nothing started below may use it
cd "$HOME/recovery-r2/agent-sparring"
[ "$(shasum -a 256 "$MANIFEST" | cut -d' ' -f1)" = "$(cat "$KIT/MANIFEST_SHA256")" ] || { echo "manifest changed"; exit 1; }
"$R" resume-plan --manifest "$MANIFEST" --max-send-back-cycles 3 \
  --claude-executable "$HOME/.local/bin/claude" --codex-executable "$HOME/.local/bin/codex" "$@"
