#!/bin/sh
# Issue #5 recovery kit, step 3b: show the Plan R start summary. Run as sparring-agent.
# start-plan without --confirm is a dry run: it parses the Markdown plan, makes no provider turn,
# creates no branch, worktree or run, and prints a confirm token bound to exactly what it showed.
# The preview is also saved to the outbox for review. Nothing starts here.
set -eu
[ "$(id -un)" = sparring-agent ] || { echo "run as sparring-agent"; exit 1; }
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
KIT=/Users/Shared/sparring-recovery
R=/Users/Shared/sparring-bootstrap/runner/bin/sparring
PLAN=docs/plans/issue5-r-complete-engine-git-authority.md
OUT="/Users/Shared/sparring-outbox/plan-r-preview-$(date -u +%Y%m%dT%H%M%SZ).txt"
sh "$KIT/gate.sh" run >/dev/null || { echo "gate failed: run sh $KIT/gate.sh run to see why"; exit 1; }
cd "$HOME/recovery/agent-sparring"
{
  echo "== gate =="; sh "$KIT/gate.sh" run
  echo; echo "== check-plan =="; "$R" check-plan "$PLAN" --repo-root .
  echo; echo "== start-plan preview (not started) =="
  "$R" start-plan "$PLAN" --managed --target-branch plan/issue5-recovery --allow-push-for-run \
    --max-send-back-cycles 3 \
    --claude-executable "$HOME/.local/bin/claude" --codex-executable "$HOME/.local/bin/codex"
} 2>&1 | tee "$OUT"
echo; echo "Saved: $OUT"
echo "Do not confirm until the preview has been reviewed. Then: sh $KIT/run-r.sh <confirm-token>"
