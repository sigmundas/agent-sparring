#!/bin/sh
# Issue #5 Plan L kit: the credential-free gate, derived from issue5-recovery-kit/gate.sh. Run as sparring-agent.
#   sh gate.sh baseline  -- before an isolated test or the preflight (credentials and isolation only)
#   sh gate.sh run       -- before the Plan L preview or run (also checks the repository layout)
# Every line must say "ok". Presence only: no command here prints, requests or keeps a secret.
# Unlike the bootstrap gate, anything indeterminate (an error, an unknown helper) is FAIL, never "absent".
set -u
MODE="${1:-}"
case "$MODE" in baseline|run) ;; *) echo "usage: sh gate.sh baseline|run"; exit 2;; esac
[ "$(id -un)" = sparring-agent ] || { echo "run as sparring-agent"; exit 1; }
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
KIT=/Users/Shared/sparring-recovery-l
RUNNER=/Users/Shared/sparring-bootstrap/runner
RUNNER_PKG="$RUNNER/lib/python3.13/site-packages"
PLAN_BRANCH=plan/issue5-local-first
PLAN_SHA=$(cat "$KIT/PLAN_SHA")
RUNNER_SHA256=$(cat "$KIT/RUNNER_SHA256")
REPO="$HOME/recovery-l/agent-sparring"
REMOTE="$HOME/recovery-l/remote.git"
HOSTS="github.com gitlab.com gitlab.sintef.no bitbucket.org"
fail=0
ok(){ echo "ok    $1"; }
bad(){ echo "FAIL  $1"; fail=1; }
req(){ if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }

# A keychain lookup without -w/-g prints no secret. Exit 44 is "not found"; 0 is "present"; anything else is indeterminate.
kc(){ # kc <label> <security args...>
  label=$1; shift
  /usr/bin/security "$@" >/dev/null 2>&1; rc=$?
  case $rc in 44) ok "$label: absent";; 0) bad "$label: PRESENT";; *) bad "$label: indeterminate (security exit $rc)";; esac
}
# A locked keychain could make a lookup say "not found" for an item that exists, so a locked login
# keychain is indeterminate (FAIL): unlock it in the account's own session first. No keychain at all is fine.
LOGIN_KC="$HOME/Library/Keychains/login.keychain-db"
if [ -e "$LOGIN_KC" ]; then
  /usr/bin/security show-keychain-info "$LOGIN_KC" >/dev/null 2>&1; rc=$?
  [ $rc = 0 ] && ok "login keychain is unlocked, so lookups are meaningful" \
              || bad "login keychain locked or unreadable (exit $rc): lookups would be indeterminate"
else
  ok "no login keychain"
fi
for h in $HOSTS; do
  kc "keychain internet password for $h" find-internet-password -s "$h"
  kc "keychain generic item gh:$h"       find-generic-password -s "gh:$h"
  kc "keychain generic item git:https://$h" find-generic-password -s "git:https://$h"
done

# Credential helpers from user and system scope. osxkeychain is covered by the keychain checks above.
# Any other helper cannot be classified without running it, so it fails the gate instead of being run.
helpers=$(cd / && git config --get-regexp '^credential\..*helper$|^credential\.helper$' 2>/dev/null | cut -d' ' -f2-)
if [ -z "$helpers" ]; then ok "no credential helper configured"
else
  echo "$helpers" | while IFS= read -r hv; do
    case "$hv" in osxkeychain|"") ;; *) echo "UNKNOWN";; esac
  done | grep -q UNKNOWN && bad "a credential helper other than osxkeychain is configured (not run)" \
                         || ok "only osxkeychain helpers configured (covered by keychain checks)"
fi

# Credential files and gh: existence only, contents never read.
for f in "$HOME/.git-credentials" "$HOME/.config/git/credentials" "$HOME/.netrc" "$HOME/.config/gh/hosts.yml"; do
  if [ -e "$f" ]; then bad "credential file exists: ${f#$HOME/}"; else ok "no ${f#$HOME/}"; fi
done

# SSH: ssh-add -l exits 1 for no identities and 2 for no agent; 0 means identities; anything else is indeterminate.
ssh-add -l >/dev/null 2>&1; rc=$?
case $rc in 1|2) ok "ssh-agent holds no identities (exit $rc)";; 0) bad "ssh-agent holds identities";; *) bad "ssh-add indeterminate (exit $rc)";; esac
keys=0
if [ -d "$HOME/.ssh" ]; then
  for f in "$HOME"/.ssh/* ; do
    [ -f "$f" ] && grep -q -- '-----BEGIN .*PRIVATE KEY-----' "$f" 2>/dev/null && keys=$((keys+1))
  done
  idf=$(awk 'tolower($1)=="identityfile"{print $2}' "$HOME/.ssh/config" 2>/dev/null | sed "s|^~|$HOME|")
  for f in $idf; do [ -e "$f" ] && keys=$((keys+1)); done
fi
[ "$keys" = 0 ] && ok "no SSH private keys" || bad "SSH private keys present: $keys"

# Credential-bearing environment variables: names only. SSH_AUTH_SOCK is not listed: launchd sets it in
# every macOS login session, and the ssh-add check above already proves the agent holds no identities.
for v in GITHUB_TOKEN GH_TOKEN GITLAB_TOKEN CI_JOB_TOKEN HOMEBREW_GITHUB_API_TOKEN GIT_ASKPASS SSH_ASKPASS \
         GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT GIT_SSH GIT_SSH_COMMAND; do
  eval "isset=\${$v+x}"; [ -z "$isset" ] && ok "$v unset" || bad "$v is set"
done

# Isolation from the owner's account and the trusted kit.
if ls /Users/sigmundas >/dev/null 2>&1; then bad "can list /Users/sigmundas"; else ok "cannot list /Users/sigmundas"; fi
for d in "$KIT" /Users/Shared/sparring-recovery-r2 /Users/Shared/sparring-recovery-r2/evidence /Users/Shared/sparring-recovery "$RUNNER/bin" "$RUNNER_PKG/agent_sparring"; do
  if touch "$d/.w" 2>/dev/null; then rm -f "$d/.w"; bad "can write $d"; else ok "cannot write $d"; fi
done
got=$(cd "$RUNNER_PKG" && find agent_sparring -type f -name '*.py' | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256 | cut -d' ' -f1)
[ "$got" = "$RUNNER_SHA256" ] && ok "frozen runner source matches c61eaf8 fingerprint" || bad "frozen runner source changed"

# Pinned provider CLIs (the same versions the bootstrap installed).
req "claude is ~/.local/bin/claude 2.1.302" "[ \"\$(command -v claude)\" = \"$HOME/.local/bin/claude\" ] && claude --version | grep -q '^2.1.302 '"
req "codex is ~/.local/bin/codex 0.160.0"   "[ \"\$(command -v codex)\" = \"$HOME/.local/bin/codex\" ] && codex --version | grep -q ' 0.160.0\$'"

if [ "$MODE" = run ]; then
  req "checkout is $PLAN_BRANCH"            "[ \"\$(git -C '$REPO' symbolic-ref --short HEAD)\" = '$PLAN_BRANCH' ]"
  req "checkout HEAD is $PLAN_SHA"          "[ \"\$(git -C '$REPO' rev-parse HEAD)\" = '$PLAN_SHA' ]"
  req "checkout is clean"                   "[ -z \"\$(git -C '$REPO' status --porcelain)\" ]"
  req "local remote branch is $PLAN_SHA"    "[ \"\$(git --git-dir='$REMOTE' rev-parse refs/heads/$PLAN_BRANCH)\" = '$PLAN_SHA' ]"
  req "local remote has only $PLAN_BRANCH"  "[ \"\$(git --git-dir='$REMOTE' for-each-ref --format='%(refname)')\" = 'refs/heads/$PLAN_BRANCH' ]"
  req "origin is the only remote"           "[ \"\$(git -C '$REPO' remote)\" = origin ]"
  req "origin fetch URL is exactly $REMOTE" "[ \"\$(git -C '$REPO' remote get-url --all origin)\" = '$REMOTE' ]"
  req "origin push URL is exactly $REMOTE"  "[ \"\$(git -C '$REPO' remote get-url --push --all origin)\" = '$REMOTE' ]"
  req "no pushurl/insteadOf/sshCommand/hooksPath in repo config" \
      "! git -C '$REPO' config --local --get-regexp '(pushurl|insteadof|pushinsteadof|sshcommand|hookspath|credential)' "
  req "no repository hooks"                 "[ -z \"\$(ls '$REPO/.git/hooks' | grep -v '\\.sample\$')\" ]"
  req "Plan R isolated test evidence preserved" "ls /Users/Shared/sparring-recovery-r2/evidence/test-r-36e2cea-*/SUMMARY.txt"
fi

[ $fail = 0 ] && echo "GATE PASSED ($MODE)" || echo "GATE FAILED ($MODE): do not continue"
exit $fail
