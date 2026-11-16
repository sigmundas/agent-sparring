#!/bin/sh
# Issue #5 Plan R2 kit, step 1: prepare the isolated Plan R2 workspace. Run as sparring-agent.
# Creates ~/recovery-r2/remote.git (a local bare remote holding only plan/issue5-r2) and the working copy
# ~/recovery-r2/agent-sparring, warms the uv cache from the locked dependencies, and lets the stage agent
# run the test commands without an approval prompt or the default Bash timeout. Does not touch ~/recovery, ~/agent-sparring or any
# earlier run. Starts nothing.
set -eu
[ "$(id -un)" = sparring-agent ] || { echo "run as sparring-agent"; exit 1; }
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
KIT=/Users/Shared/sparring-recovery-r2
BUNDLE="$KIT/r2.bundle"
PLAN_BRANCH=plan/issue5-r2
PLAN_SHA=$(cat "$KIT/PLAN_SHA")
BUNDLE_SHA256=$(cat "$KIT/BUNDLE_SHA256")
REMOTE="$HOME/recovery-r2/remote.git"
REPO="$HOME/recovery-r2/agent-sparring"
SETTINGS="$HOME/.claude/settings.json"
die(){ echo "SETUP FAILED: $*"; exit 1; }

sh "$KIT/gate.sh" baseline >/dev/null || die "gate failed; run sh $KIT/gate.sh baseline to see why"
unset SSH_AUTH_SOCK   # the gate checked the account's ssh-agent above; nothing started below may use it
[ "$(shasum -a 256 "$BUNDLE" | cut -d' ' -f1)" = "$BUNDLE_SHA256" ] || die "bundle sha256 differs from $KIT/BUNDLE_SHA256"
git bundle verify "$BUNDLE" >/dev/null 2>&1 || die "bundle does not verify"
[ "$(git bundle list-heads "$BUNDLE")" = "$PLAN_SHA refs/heads/$PLAN_BRANCH" ] || die "bundle heads are not exactly $PLAN_SHA $PLAN_BRANCH"
mkdir -p "$HOME/recovery-r2"
[ -e "$REMOTE" ] && die "$REMOTE exists; remove it to rerun"
[ -e "$REPO" ] && die "$REPO exists; remove it to rerun"
git -c transfer.fsckObjects=true clone -q --bare "$BUNDLE" "$REMOTE" || die "bare clone from bundle"
git --git-dir="$REMOTE" remote remove origin 2>/dev/null || true   # the bare remote points nowhere
git -c core.hooksPath=/dev/null clone -q -b "$PLAN_BRANCH" "$REMOTE" "$REPO" || die "working-copy clone"
[ "$(git -C "$REPO" rev-parse HEAD)" = "$PLAN_SHA" ] || die "working copy is not at $PLAN_SHA"
(cd "$REPO" && uv sync --frozen) || die "uv sync --frozen"
[ -z "$(git -C "$REPO" status --porcelain)" ] || die "working copy is dirty after uv sync"

# The last run's stage agent could not run pytest: Claude refused the Bash call for approval under
# --permission-mode acceptEdits. Allow exactly the local test and Git commands the plan requires, deny
# git push (the engine pushes, the agent never does). This account holds no credentials, and these
# rules are workflow settings, not a security boundary. A previous file is kept beside it.
mkdir -p "$HOME/.claude"
[ -e "$SETTINGS" ] && cp -p "$SETTINGS" "$SETTINGS.pre-r2-$(date -u +%Y%m%dT%H%M%SZ)"
/usr/bin/python3 -I - "$SETTINGS" <<'EOF' || die "could not update $SETTINGS"
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
perms = data.setdefault("permissions", {})
allow = [
    "Bash(uv sync --frozen)", "Bash(uv sync --frozen:*)",
    "Bash(.venv/bin/python -m pytest:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    "Bash(git add:*)", "Bash(git commit:*)", "Bash(git rev-parse:*)", "Bash(git ls-files:*)",
    "Bash(git grep:*)",
]
deny = ["Bash(git push:*)"]
perms["allow"] = sorted(set(perms.get("allow", [])) | set(allow))
perms["deny"] = sorted(set(perms.get("deny", [])) | set(deny))
# The focused set takes about 12 minutes and the full suite about 35: above Claude's default Bash
# timeout. preflight-r2.sh proves the raised default takes effect.
env = data.setdefault("env", {})
env["BASH_DEFAULT_TIMEOUT_MS"] = "2700000"
env["BASH_MAX_TIMEOUT_MS"] = "3600000"
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print("claude permissions:", path)
EOF
echo "Setup done. Next: sh $KIT/preflight-r2.sh"
