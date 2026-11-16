#!/bin/sh
# Issue #5 recovery kit, step 3a: prepare the isolated Plan R workspace. Run as sparring-agent.
# Creates ~/recovery/remote.git (a local bare remote holding only plan/issue5-recovery) and the
# working copy ~/recovery/agent-sparring. Reuses the claude/codex the bootstrap installed.
# Does not touch ~/agent-sparring, ~/remote.git or the original managed worktree. Starts nothing.
set -eu
[ "$(id -un)" = sparring-agent ] || { echo "run as sparring-agent"; exit 1; }
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
KIT=/Users/Shared/sparring-recovery
BUNDLE="$KIT/recovery.bundle"
PLAN_BRANCH=plan/issue5-recovery
PLAN_SHA=$(cat "$KIT/PLAN_SHA")
REMOTE="$HOME/recovery/remote.git"
REPO="$HOME/recovery/agent-sparring"
die(){ echo "SETUP FAILED: $*"; exit 1; }

git bundle verify "$BUNDLE" >/dev/null 2>&1 || die "bundle does not verify"
[ "$(git bundle list-heads "$BUNDLE" "refs/heads/$PLAN_BRANCH" | cut -d' ' -f1)" = "$PLAN_SHA" ] || die "bundle branch is not $PLAN_SHA"
mkdir -p "$HOME/recovery"
[ -e "$REMOTE" ] && die "$REMOTE exists; remove it to rerun"
[ -e "$REPO" ] && die "$REPO exists; remove it to rerun"
git clone -q --bare "$BUNDLE" "$REMOTE" || die "bare clone from bundle"
git --git-dir="$REMOTE" remote remove origin 2>/dev/null || true   # the bare remote points nowhere
git -c core.hooksPath=/dev/null clone -q -b "$PLAN_BRANCH" "$REMOTE" "$REPO" || die "working-copy clone"
[ "$(git -C "$REPO" rev-parse HEAD)" = "$PLAN_SHA" ] || die "working copy is not at $PLAN_SHA"
(cd "$REPO" && uv sync --frozen) || die "uv sync --frozen"
[ -z "$(git -C "$REPO" status --porcelain)" ] || die "working copy is dirty after uv sync"
echo "Setup done. Next: sh $KIT/preview-r.sh"
