#!/bin/sh
# Issue #5 recovery kit, step 3d: after the run stops, export everything on the local remote for review.
set -eu
[ "$(id -un)" = sparring-agent ] || { echo "run as sparring-agent"; exit 1; }
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
REMOTE="$HOME/recovery/remote.git"
OUT="/Users/Shared/sparring-outbox/plan-r-$(date -u +%Y%m%dT%H%M%SZ).bundle"
git --git-dir="$REMOTE" for-each-ref --format='%(objectname) %(refname)'
git --git-dir="$REMOTE" bundle create "$OUT" --all
git bundle verify "$OUT" >/dev/null && echo "exported: $OUT"
