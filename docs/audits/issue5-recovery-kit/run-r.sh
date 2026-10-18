#!/bin/sh
# Issue #5 recovery kit, step 3c: START the Plan R run. Run as sparring-agent, and only after a person
# has reviewed the preview from preview-r.sh. The token refuses if anything changed since the preview.
# Pushes go only to the local bare remote ~/recovery/remote.git; this account holds no Git credentials.
set -eu
[ "$(id -un)" = sparring-agent ] || { echo "run as sparring-agent"; exit 1; }
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
KIT=/Users/Shared/sparring-recovery
R=/Users/Shared/sparring-bootstrap/runner/bin/sparring
PLAN=docs/plans/issue5-r-complete-engine-git-authority.md
TOKEN="${1:-}"
[ -n "$TOKEN" ] || { echo "usage: sh run-r.sh <confirm-token from preview-r.sh>"; exit 2; }
sh "$KIT/gate.sh" run >/dev/null || { echo "gate failed: run sh $KIT/gate.sh run to see why"; exit 1; }
cd "$HOME/recovery/agent-sparring"
"$R" start-plan "$PLAN" --managed --target-branch plan/issue5-recovery --allow-push-for-run \
  --max-send-back-cycles 3 \
  --claude-executable "$HOME/.local/bin/claude" --codex-executable "$HOME/.local/bin/codex" \
  --confirm "$TOKEN"
