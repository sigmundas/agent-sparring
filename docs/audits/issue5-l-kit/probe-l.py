"""Independent black-box probes for Plan L, run by test-l.sh at each human gate.

These are the kit's own checks, written outside the run. They drive the
candidate's `sparring` CLI in disposable repositories with fake providers.
- The provider protocol is the one the reviewed tests use
  (tests/test_plan_activity.py at ea8e70d).
- A recording `git` wrapper first on PATH logs every Git command the engine
  runs.
- Legacy run state is produced by the frozen, reviewed runner (the pre-Plan L
  engine). So the migration is tested against a real legacy pause, not a
  hand-written fixture.

Usage:
  python3 -I probe-l.py --engine <candidate sparring> --legacy-engine <frozen sparring>
                        --accepted-stage N --out DIR
  python3 -I probe-l.py --self-test --legacy-engine <frozen sparring> --out DIR

- `--accepted-stage N` runs every probe for Stages 1..N, and fails if any of
  them fails.
- `--self-test` points every probe at the frozen pre-Plan L engine, which is
  not local-first. The harness controls must pass, and every local-first
  probe must FAIL. That proves the probes tell the difference.

Each probe states its plan reference. Every negative check has a positive
control in the same probe.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REAL_GIT = shutil.which("git")
REMOTE_SUBCOMMANDS = {"push", "fetch", "pull", "ls-remote", "clone", "send-pack", "fetch-pack"}
# Git options that take a separate value before the subcommand.
VALUE_OPTIONS = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}

FAKE_CLAUDE = r'''
import json, os, subprocess, sys
args = sys.argv[1:]
d = os.environ["FAKE_DIR"]; git = os.environ["FAKE_GIT"]
def bump(name):
    p = os.path.join(d, name); n = int(open(p).read()) if os.path.exists(p) else 0
    open(p, "w").write(str(n + 1)); return n
n = bump("claude-calls")
actions = open(os.path.join(d, "claude-actions")).read().split() if os.path.exists(os.path.join(d, "claude-actions")) else []
action = actions[n] if n < len(actions) else "commit"
if action == "trip":
    open(os.path.join(d, "TRIPWIRE"), "w").write("claude was started\n"); sys.exit(3)
def run(*a, cwd=None):
    subprocess.run([git, *a], cwd=cwd, check=True, capture_output=True)
def commit(where):
    p = os.path.join(where, "work.txt")
    open(p, "a").write(f"change {n} {action}\n")
    run("add", "work.txt", cwd=where); run("commit", "-q", "-m", f"fake change {n}", cwd=where)
# commit: commit here. reset-commit: rewrite here (drop the last commit), then commit.
# sibling-commit: commit here and in the sibling. sibling-reset-commit: commit here (a legitimate
# advance), but rewrite the sibling, then commit there.
here, sib = os.getcwd(), os.environ.get("FAKE_SIBLING")
if action == "reset-commit":
    run("reset", "-q", "--hard", "HEAD~1", cwd=here)
commit(here)
if action in ("sibling-commit", "sibling-reset-commit") and sib:
    if action == "sibling-reset-commit":
        run("reset", "-q", "--hard", "HEAD~1", cwd=sib)
    commit(sib)
    if os.environ.get("FAKE_PUSH_SIBLING"):
        # Self-test only: the pre-Plan L engine needs the sibling on its remote. An ordinary push; a
        # rewritten sibling is rejected as non-fast-forward, and that failure is left for the engine to see.
        subprocess.run([git, "push", "-q", "origin", "HEAD:feature/x"], cwd=sib, capture_output=True)
sid = args[args.index("--resume") + 1] if "--resume" in args else f"impl-{n + 1}"
for line in ({"type": "system", "subtype": "init", "session_id": sid, "model": "fake-model"},
             {"type": "result", "subtype": "success", "is_error": False, "result": "implemented",
              "session_id": sid, "num_turns": 1}):
    print(json.dumps(line), flush=True)
'''

FAKE_CODEX = r'''
import json, os, sys
args = sys.argv[1:]
d = os.environ["FAKE_DIR"]
p = os.path.join(d, "codex-calls"); n = int(open(p).read()) if os.path.exists(p) else 0
open(p, "w").write(str(n + 1))
if os.path.exists(os.path.join(d, "codex-trip")):
    open(os.path.join(d, "TRIPWIRE"), "w").write("codex was started\n"); sys.exit(3)
verdicts = open(os.path.join(d, "codex-verdicts")).read().split() if os.path.exists(os.path.join(d, "codex-verdicts")) else []
action = verdicts[n] if n < len(verdicts) else "READY"
tid = args[args.index("resume") + 1] if "resume" in args else f"thread-{n + 1}"
verdict = {"action": action, "summary": f"fake verdict {n}", "needs_you_reason": None,
           "findings": "none", "deferred": None}
for line in ({"type": "thread.started", "thread_id": tid}, {"type": "turn.started"},
             {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": json.dumps(verdict)}},
             {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}):
    print(json.dumps(line), flush=True)
out = args[args.index("-o") + 1]
open(out, "w").write(json.dumps(verdict))
'''

PROJECT_TOML = """project = "probe"

[repo]
root = "."

[agents.stage]
provider = "claude-cli"

[agents.sparring]
provider = "codex-cli"
"""


def plan_text(stages: int) -> str:
    body = "# Probe plan\n\nA disposable plan for the Plan L kit probes.\n"
    for i in range(1, stages + 1):
        body += f"\n## Stage {i} — Probe change {i}\n\n**Outcome:** append a line to work.txt.\n"
    return body


class Env:
    """One disposable world: repositories, fake providers, a recorder, an isolated HOME."""

    def __init__(self, root: Path, engine: str, selftest: bool = False):
        self.root = root
        self.engine = engine
        self.selftest = selftest
        self.bin = root / "bin"; self.rec = root / "recorder"; self.fake = root / "fake"
        self.home = root / "home"
        for d in (self.bin, self.rec, self.fake, self.home):
            d.mkdir(parents=True, exist_ok=True)
        self.log = root / "git-commands.log"
        (self.rec / "git").write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.log}"\nexec "{REAL_GIT}" "$@"\n', encoding="utf-8")
        (self.rec / "git").chmod(0o755)
        self.claude = self._script("claude", FAKE_CLAUDE)
        self.codex = self._script("codex", FAKE_CODEX)
        (self.home / ".gitconfig").write_text("[user]\n\tname = Probe\n\temail = probe@invalid\n[init]\n\tdefaultBranch = main\n")
        self.env = {
            "PATH": f"{self.rec}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
            "TMPDIR": str(root / "tmp"), "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
            "FAKE_DIR": str(self.fake), "FAKE_GIT": REAL_GIT,
        }
        (root / "tmp").mkdir(exist_ok=True)

    def _script(self, name, body):
        script = self.bin / f"{name}.py"; script.write_text(textwrap.dedent(body).lstrip("\n"))
        wrapper = self.bin / name
        wrapper.write_text(f'#!/bin/sh\nexec /usr/bin/python3 -I "{script}" "$@"\n'); wrapper.chmod(0o755)
        return wrapper

    def git(self, cwd, *args, check=True):
        env = dict(self.env, PATH="/usr/bin:/bin")
        r = subprocess.run([REAL_GIT, *args], cwd=cwd, env=env, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr}")
        return r.stdout.strip()

    def repo(self, name, *, stages=1, branch="feature/x"):
        path = self.root / name
        path.mkdir()
        self.git(path, "init", "-q", "-b", "main")
        (path / ".sparring").mkdir()
        (path / ".sparring" / "project.toml").write_text(PROJECT_TOML)
        (path / "docs").mkdir()
        (path / "docs" / "plan.md").write_text(plan_text(stages))
        (path / "work.txt").write_text("start\n")
        (path / ".gitignore").write_text(".sparring/stages/\n.sparring/plans/\n")
        self.git(path, "add", "-A"); self.git(path, "commit", "-q", "-m", "probe base")
        self.git(path, "switch", "-q", "-c", branch)
        if self.selftest:
            bare = self.bare(f"st-{name}", marker=False)
            self.git(path, "remote", "add", "origin", str(bare))
            self.git(path, "push", "-q", "-u", "origin", "main", branch)
        return path

    def set_origin(self, repo, url):
        self.git(repo, "remote", "remove", "origin", check=False)
        self.git(repo, "remote", "add", "origin", str(url))

    def bare(self, name, *, marker=True):
        path = self.root / f"{name}.git"
        self.git(self.root, "init", "-q", "--bare", str(path))
        if marker:
            hook = path / "hooks" / "post-receive"
            hook.write_text(f'#!/bin/sh\ntouch "{self.root}/MARKER-{name}"\n'); hook.chmod(0o755)
        return path

    def marker(self, name):
        return (self.root / f"MARKER-{name}").exists()

    def refs(self, bare):
        return self.git(self.root, "--git-dir", str(bare), "for-each-ref")

    def sparring(self, repo, *args, engine=None, timeout=900):
        cmd = [engine or self.engine, "--sparring-dir", str(repo / ".sparring"), *args]
        r = subprocess.run(cmd, cwd=repo, env=self.env, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr

    def providers(self):
        extra = ["--allow-push-for-run"] if self.selftest else []
        return [*extra, "--claude-executable", str(self.claude), "--codex-executable", str(self.codex)]

    def remote_commands(self):
        if not self.log.exists():
            return []
        found = []
        for line in self.log.read_text().splitlines():
            words, i = line.split(), 0
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] in VALUE_OPTIONS else 1
            sub = words[i] if i < len(words) else ""
            nxt = words[i + 1] if i + 1 < len(words) else ""
            if sub in REMOTE_SUBCOMMANDS or (sub == "remote" and nxt in {"update", "show", "prune"}) \
                    or (sub == "submodule" and nxt == "update"):
                found.append(line)
        return found

    def calls(self, name):
        p = self.fake / f"{name}-calls"
        return int(p.read_text()) if p.exists() else 0


def snapshot(env: Env, repo: Path) -> str:
    """Everything a refusal must leave byte-identical: .sparring, refs, HEAD, worktrees, status."""
    h = hashlib.sha256()
    for p in sorted((repo / ".sparring").rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(repo)).encode()); h.update(p.read_bytes())
    for args in (("for-each-ref",), ("rev-parse", "HEAD"), ("worktree", "list", "--porcelain"),
                 ("status", "--porcelain", "--untracked-files=all")):
        h.update(env.git(repo, *args, check=False).encode())
    return h.hexdigest()


def stage_states(repo: Path) -> dict:
    states = {}
    for p in sorted((repo / ".sparring" / "stages").glob("*/state.json")):
        try:
            states[p.parent.name] = json.loads(p.read_text()).get("status")
        except (OSError, ValueError):
            states[p.parent.name] = "unreadable"
    return states


def run_states(repo: Path) -> list:
    out = []
    for p in sorted((repo / ".sparring" / "plans").glob("*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            out.append({"unreadable": str(p)})
    return out


def awaiting_kinds(repo: Path) -> list:
    return [(s.get("awaiting") or {}).get("kind") for s in run_states(repo)]


def all_text(repo: Path) -> str:
    chunks = []
    for p in (repo / ".sparring").rglob("*"):
        if p.is_file() and p.stat().st_size < 2_000_000:
            chunks.append(p.read_text(errors="replace"))
    return "\n".join(chunks)


class Result:
    def __init__(self, pid, stage, ref):
        self.pid, self.stage, self.ref, self.checks = pid, stage, ref, []
        self.not_self_testable = None

    def check(self, name, ok, detail="", control=False):
        self.checks.append((name, bool(ok), detail, control))
        return ok

    @property
    def passed(self):
        return all(c[1] for c in self.checks) and bool(self.checks)

    @property
    def discriminates(self):
        """Self-test: every control passed and at least one property failed."""
        return all(c[1] for c in self.checks if c[3]) and any(not c[1] for c in self.checks if not c[3])


def accepted(states):
    return [k for k, v in states.items() if str(v).lower() == "accepted"]


# ---------------------------------------------------------------- probes

def p_local_run(env: Env) -> Result:
    r = Result("L2-local-run", 2, "I-L1, I-L2; Stage 2 end-to-end")
    for with_remote in (False, True):
        repo = env.repo(f"local-run-{int(with_remote)}", stages=2)
        bare = None
        if with_remote:
            bare = env.bare(f"local-run-{int(with_remote)}")
            env.set_origin(repo, str(bare))
            env.git(repo, "push", "-q", "-u", "origin", "feature/x")
            before = env.refs(bare)
            (env.root / f"MARKER-local-run-{int(with_remote)}").unlink(missing_ok=True)
        env.log.unlink(missing_ok=True)
        code, out = env.sparring(repo, "run-plan", "docs/plan.md", "--repo-root", str(repo),
                                 "--expected-branch", "feature/x", *env.providers())
        tag = "with bare remote" if with_remote else "no remote"
        r.check(f"run completes ({tag})", code == 0, out[-600:])
        r.check(f"both stages accepted ({tag})", len(accepted(stage_states(repo))) == 2, stage_states(repo))
        r.check(f"no remote Git command ({tag})", not env.remote_commands(), env.remote_commands()[:5])
        if with_remote:
            r.check("remote refs unchanged", env.refs(bare) == before)
            r.check("remote hook never ran", not env.marker(f"local-run-{int(with_remote)}"))
    return r


def p_flags(env: Env, legacy_engine: str) -> Result:
    r = Result("L2-deprecated-flags", 2, "I-L6, L-D3; Stage 2 flags")
    repo = env.repo("flags")
    (env.fake / "codex-trip").write_text("1")
    (env.fake / "claude-actions").write_text("trip\n")
    before = snapshot(env, repo)
    code, out = env.sparring(repo, "run-plan", "docs/plan.md", "--repo-root", str(repo),
                             "--expected-branch", "feature/x", "--allow-push-for-run", *env.providers())
    r.check("--allow-push-for-run refused", code != 0 and "push_not_part_of_runs" in out, out[-400:])
    r.check("nothing created", snapshot(env, repo) == before and not run_states(repo))
    r.check("no provider started", not (env.fake / "TRIPWIRE").exists())
    (env.fake / "codex-trip").unlink(); (env.fake / "claude-actions").unlink()
    # --allow-push-candidate against a real legacy pause made by the frozen runner.
    legacy, bare, sha, key = make_legacy_pause(env, legacy_engine, "flags-legacy")
    if not r.check("legacy pause produced by the frozen runner", sha is not None, key, control=True):
        return r
    (env.fake / "codex-trip").write_text("1"); (env.fake / "claude-actions").write_text("trip\n" * 20)
    claude_before = env.calls("claude")
    before = snapshot(env, legacy)
    code, out = env.sparring(legacy, "resume-plan", "docs/plan.md", "--run-key", key, "--repo-root", str(legacy),
                             "--expected-branch", "feature/x", "--allow-push-candidate", sha, *env.providers())
    r.check("--allow-push-candidate refused", code != 0 and "push_permission_deprecated" in out, out[-400:])
    r.check("run state byte-identical", snapshot(env, legacy) == before)
    r.check("no provider started", not (env.fake / "TRIPWIRE").exists() and env.calls("claude") == claude_before)
    (env.fake / "codex-trip").unlink(); (env.fake / "claude-actions").unlink()
    return r


def p_history(env: Env, sibling: bool) -> Result:
    name = "L2-history-sibling" if sibling else "L2-history-primary"
    r = Result(name, 2, "I-L2b per repository; Stage 2 history")
    for rewrite in (False, True):
        tag = "rewrite" if rewrite else "advance"
        repo = env.repo(f"hist-{int(sibling)}-{tag}", stages=2)
        args = ["run-plan", "docs/plan.md"]
        if sibling:
            sib = env.repo(f"hist-sib-{tag}", stages=1)
            env.env["FAKE_SIBLING"] = str(sib)
            if env.selftest:
                env.env["FAKE_PUSH_SIBLING"] = "1"
            manifest = {"version": 1, "plan_label": "docs/plan.md", "source_digest": "sha256:probe", "stages": [
                {"stage_id": f"probe-s{i}", "label": f"S{i}", "title": f"Probe change {i}",
                 "brief": f"Append a line to work.txt in both repositories (stage {i}).", "mode": "implementation",
                 "repositories": [{"name": "sib", "path": str(sib), "branch": "feature/x", "candidate_sha": None}]}
                for i in (1, 2)]}
            mpath = repo / "probe.manifest.json"; mpath.write_text(json.dumps(manifest, indent=2))
            env.git(repo, "add", "probe.manifest.json"); env.git(repo, "commit", "-q", "-m", "manifest")
            args = ["run-plan", "--manifest", str(mpath)]
            second = "sibling-reset-commit" if rewrite else "sibling-commit"
            (env.fake / "claude-actions").write_text(f"sibling-commit\n{second}\n")
        else:
            env.env.pop("FAKE_SIBLING", None)
            (env.fake / "claude-actions").write_text("commit\n" + ("reset-commit\n" if rewrite else "commit\n"))
        for f in ("claude-calls", "codex-calls"):
            (env.fake / f).unlink(missing_ok=True)
        code, out = env.sparring(repo, *args, "--repo-root", str(repo), "--expected-branch", "feature/x",
                                 *env.providers())
        states = stage_states(repo)
        where = "sibling" if sibling else "primary"
        if rewrite:
            r.check(f"{where} rewrite refused with run_history_rewritten",
                    code != 0 and "run_history_rewritten" in (out + all_text(repo)), out[-500:])
            r.check(f"{where} rewritten stage not accepted", len(accepted(states)) == 1, states)
        else:
            r.check(f"{where} legitimate advance accepted (positive control)", code == 0 and len(accepted(states)) == 2,
                    out[-500:], control=True)
    env.env.pop("FAKE_SIBLING", None); env.env.pop("FAKE_PUSH_SIBLING", None)
    (env.fake / "claude-actions").unlink(missing_ok=True)
    return r


def p_remote_refusal(env: Env, candidate_python: str, candidate_src: str) -> Result:
    r = Result("L3-remote-refusal", 3, "I-L1; Stage 3 refusal")
    repo = env.repo("refusal"); bare = env.bare("refusal")
    env.set_origin(repo, str(bare))
    forms = [["push", str(bare), "HEAD:refs/heads/x"], ["fetch", str(bare)], ["ls-remote", str(bare)],
             ["pull", str(bare), "main"], ["-c", "x.y=1", "push", str(bare), "HEAD:refs/heads/y"],
             ["--git-dir", str(repo / ".git"), "ls-remote", str(bare)], ["-C", str(repo), "fetch", "origin"]]
    env.git(repo, "push", "-q", str(bare), "HEAD:refs/heads/control")
    r.check("positive control: plain git push reaches the remote", env.marker("refusal"), control=True)
    (env.root / "MARKER-refusal").unlink(missing_ok=True)
    control_refs = env.refs(bare)
    code = textwrap.dedent("""
        import json, sys
        sys.path.insert(0, sys.argv[1])
        from pathlib import Path
        from agent_sparring import engine_git
        out = []
        for form in json.loads(sys.argv[3]):
            try:
                res = engine_git.run(Path(sys.argv[2]), *form)
                out.append(["ran", getattr(res, "returncode", None)])
            except Exception as exc:
                out.append(["refused", getattr(exc, "code", None), type(exc).__name__, str(exc)[:200]])
        print(json.dumps(out))
    """)
    p = subprocess.run([candidate_python, "-I", "-c", code, candidate_src, str(repo), json.dumps(forms)],
                       env=env.env, capture_output=True, text=True, timeout=300)
    try:
        results = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        r.not_self_testable = "engine_git is absent in this engine"
        r.check("probe could import engine_git", False, (p.stdout + p.stderr)[-500:]); return r
    for form, res in zip(forms, results):
        r.check(f"refused: git {' '.join(form[:3])}", res[0] == "refused" and (
            res[1] == "remote_operation_not_allowed" or "remote_operation_not_allowed" in res[-1]), res)
    r.check("remote never reached", not env.marker("refusal") and env.refs(bare) == control_refs)
    return r


def p_planted(env: Env) -> Result:
    r = Result("L3-planted-config", 3, "I-L1, I-L4; Stage 3 planted configuration")
    repo = env.repo("planted", stages=1); decoy = env.bare("decoy")
    ssh_marker = env.root / "MARKER-sshcommand"; helper_marker = env.root / "MARKER-helper"
    (env.bin / "evil-ssh").write_text(f'#!/bin/sh\ntouch "{ssh_marker}"\nexit 1\n'); (env.bin / "evil-ssh").chmod(0o755)
    (env.bin / "evil-helper").write_text(f'#!/bin/sh\ntouch "{helper_marker}"\n'); (env.bin / "evil-helper").chmod(0o755)
    env.set_origin(repo, "ssh://127.0.0.1:1/probe.git")
    env.git(repo, "config", "core.sshCommand", str(env.bin / "evil-ssh"))
    env.git(repo, "config", "credential.helper", str(env.bin / "evil-helper"))
    env.git(repo, "config", f"url.{decoy}.insteadOf", "https://example.invalid/")
    env.git(repo, "config", "branch.feature/x.remote", "origin")
    env.git(repo, "config", "branch.feature/x.merge", "refs/heads/feature/x")
    env.log.unlink(missing_ok=True)
    code, out = env.sparring(repo, "run-plan", "docs/plan.md", "--repo-root", str(repo),
                             "--expected-branch", "feature/x", *env.providers())
    r.check("run completes despite planted configuration", code == 0 and len(accepted(stage_states(repo))) == 1,
            out[-500:])
    code2, out2 = env.sparring(repo, "runs", "--repo-root", str(repo), "--json")
    r.check("runs --json works", code2 == 0, out2[-300:])
    r.check("no remote Git command recorded", not env.remote_commands(), env.remote_commands()[:5])
    r.check("no planted program ran", not ssh_marker.exists() and not helper_marker.exists() and not env.marker("decoy"))
    env.git(repo, "ls-remote", "origin", check=False)
    r.check("positive control: plain git ls-remote runs the planted sshCommand", ssh_marker.exists(), control=True)
    return r


def make_legacy_pause(env: Env, legacy_engine: str, name: str):
    """A real push_authorization_required pause, made by the frozen pre-Plan L runner."""
    repo = env.repo(name, stages=1); bare = env.bare(name)
    env.set_origin(repo, str(bare)); env.git(repo, "push", "-q", "-u", "origin", "feature/x")
    for f in ("claude-calls", "codex-calls", "claude-actions"):
        (env.fake / f).unlink(missing_ok=True)
    key = f"legacy-{name}"
    code, out = env.sparring(repo, "run-plan", "docs/plan.md", "--run-key", key, "--repo-root", str(repo),
                             "--expected-branch", "feature/x", "--claude-executable", str(env.claude),
                             "--codex-executable", str(env.codex), engine=legacy_engine)
    states = run_states(repo)
    aw = (states[0].get("awaiting") or {}) if states else {}
    if aw.get("kind") != "push_authorization_required":
        return repo, bare, None, f"frozen runner did not pause for push (exit {code}): {out[-400:]}"
    return repo, bare, aw.get("candidate_sha"), key


def p_legacy(env: Env, legacy_engine: str) -> Result:
    r = Result("L4-legacy-migration", 4, "L-D1 (six conditions), plain resume only; Stage 4")
    # Positive: every condition holds.
    repo, bare, sha, key = make_legacy_pause(env, legacy_engine, "legacy-ok")
    if not r.check("legacy pause produced by the frozen runner", sha is not None, key, control=True):
        return r
    before_refs, claude_before = env.refs(bare), env.calls("claude")
    env.log.unlink(missing_ok=True)
    code, out = env.sparring(repo, "resume-plan", "docs/plan.md", "--run-key", key, "--repo-root", str(repo),
                             "--expected-branch", "feature/x", *env.providers())
    r.check("plain resume converts and accepts locally", code == 0 and len(accepted(stage_states(repo))) == 1, out[-500:])
    r.check("migration recorded (legacy_push_pause_converted)", "legacy_push_pause_converted" in all_text(repo))
    r.check("no implementation turn re-entered", env.calls("claude") == claude_before)
    r.check("nothing pushed, no remote command", env.refs(bare) == before_refs and not env.remote_commands())
    # Negatives: each breaks one condition and must stay paused, state unchanged apart from the report.
    breakers = {
        "HEAD moved": lambda rp: (rp.joinpath("work.txt").open("a").write("x\n"),
                                  env.git(rp, "commit", "-q", "-am", "moved")),
        "dirty tree": lambda rp: rp.joinpath("work.txt").open("a").write("dirty\n"),
        "plan edited": lambda rp: (rp.joinpath("docs/plan.md").open("a").write("\nedited\n"),
                                   env.git(rp, "commit", "-q", "-am", "edit plan")),
    }
    for label, brk in breakers.items():
        repo, bare, sha, key = make_legacy_pause(env, legacy_engine, "legacy-" + label.replace(" ", "-"))
        if not r.check(f"legacy pause produced ({label})", sha is not None, key, control=True):
            continue
        brk(repo)
        refs, claude_before = env.refs(bare), env.calls("claude")
        code, out = env.sparring(repo, "resume-plan", "docs/plan.md", "--run-key", key, "--repo-root", str(repo),
                                 "--expected-branch", "feature/x", *env.providers())
        r.check(f"{label}: not accepted", not accepted(stage_states(repo)), out[-300:])
        r.check(f"{label}: still paused for the legacy reason", "push_authorization_required" in awaiting_kinds(repo),
                awaiting_kinds(repo))
        r.check(f"{label}: no turn, nothing pushed", env.calls("claude") == claude_before and env.refs(bare) == refs)
    return r


def p_local_finish(env: Env) -> Result:
    r = Result("L4-local-finish", 4, "I-L1, I-L2b; Stage 4 local finish")
    repo = env.repo("finish", stages=1); bare = env.bare("finish")
    env.set_origin(repo, str(bare)); env.git(repo, "push", "-q", "-u", "origin", "main")
    env.git(repo, "switch", "-q", "main")
    key = "finish-probe"
    code, out = env.sparring(repo, "run-plan", "docs/plan.md", "--run-key", key, "--managed", "--target-branch", "main",
                             "--repo-root", str(repo), *env.providers())
    if not r.check("managed run completes", code == 0, out[-500:]):
        return r
    refs = env.refs(bare); main_before = env.git(repo, "rev-parse", "main")
    code, out = env.sparring(repo, "finish-run", "--run-key", key, "--repo-root", str(repo), "--push-target", "--json")
    r.check("--push-target refused with remote_finish_removed", code != 0 and "remote_finish_removed" in out, out[-400:])
    r.check("nothing merged by the refused finish", env.git(repo, "rev-parse", "main") == main_before)
    env.log.unlink(missing_ok=True)
    code, out = env.sparring(repo, "finish-run", "--run-key", key, "--repo-root", str(repo), "--json")
    r.check("local finish succeeds", code == 0, out[-400:])
    r.check("target advanced locally", env.git(repo, "rev-parse", "main") != main_before)
    r.check("finish JSON offers no remote step",
            "keep_remote_branch" not in out and "candidate_not_pushed" not in out and "local_first" in out, out[-400:])
    r.check("no remote command, remote unchanged", not env.remote_commands() and env.refs(bare) == refs)
    return r


def p_capability(env: Env) -> Result:
    r = Result("L2-capability", 2, "Stage 2 capability flag")
    repo = env.repo("cap")
    code, out = env.sparring(repo, "capabilities", "--json")
    try:
        caps = json.loads(out).get("capabilities", [])
    except ValueError:
        caps = []
    r.check("capabilities --json lists local_first", code == 0 and "local_first" in caps, out[-300:])
    code, out = env.sparring(repo, "runs", "--repo-root", str(repo), "--json")
    r.check("runs --json keeps schema_version 1 and lists local_first",
            code == 0 and '"schema_version": 1' in out.replace('"schema_version":1', '"schema_version": 1')
            and "local_first" in out, out[-300:])
    return r


def harness_controls(env: Env, legacy_engine: str) -> Result:
    """The harness itself works: fakes drive a full run, and the recorder sees remote commands."""
    r = Result("H-harness", 0, "kit self-check")
    repo = env.repo("harness", stages=1); bare = env.bare("harness")
    env.set_origin(repo, str(bare)); env.git(repo, "push", "-q", "-u", "origin", "feature/x")
    env.log.unlink(missing_ok=True)
    code, out = env.sparring(repo, "run-plan", "docs/plan.md", "--repo-root", str(repo), "--expected-branch",
                             "feature/x", "--allow-push-for-run", *env.providers(), engine=legacy_engine)
    r.check("frozen runner completes a run with the fake providers", code == 0 and len(accepted(stage_states(repo))) == 1,
            out[-500:], control=True)
    r.check("recorder sees the frozen runner's remote commands", bool(env.remote_commands()), control=True)
    r.check("remote hook fired under the frozen runner", env.marker("harness"), control=True)
    return r


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine"); ap.add_argument("--legacy-engine", required=True)
    ap.add_argument("--candidate-python"); ap.add_argument("--candidate-src")
    ap.add_argument("--accepted-stage", type=int, default=5); ap.add_argument("--out", required=True)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if REAL_GIT is None:
        print("git not found"); return 2
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    engine = a.legacy_engine if a.self_test else a.engine
    results = []
    with tempfile.TemporaryDirectory(prefix="probe-l-") as tmp:
        root = Path(tmp).resolve()
        def world(name):
            return Env(root / name, engine, selftest=a.self_test)
        results.append(harness_controls(Env(root / "harness", a.legacy_engine), a.legacy_engine))
        stage = 5 if a.self_test else a.accepted_stage
        if stage >= 2:
            results.append(p_local_run(world("local-run")))
            results.append(p_flags(world("flags"), a.legacy_engine))
            results.append(p_history(world("hist-primary"), sibling=False))
            results.append(p_history(world("hist-sibling"), sibling=True))
            results.append(p_capability(world("cap")))
        if stage >= 3:
            py = a.candidate_python or (str(Path(a.legacy_engine).parent / "python") if a.self_test else None)
            src = a.candidate_src or (str(next(Path(a.legacy_engine).parent.parent.glob("lib/python*/site-packages")))
                                      if a.self_test else None)
            results.append(p_remote_refusal(world("refusal"), py, src))
            results.append(p_planted(world("planted")))
        if stage >= 4:
            results.append(p_legacy(world("legacy"), a.legacy_engine))
            results.append(p_local_finish(world("finish")))
    lines, ok = [], True
    for res in results:
        if a.self_test and res.not_self_testable:
            verdict = f"NOT SELF-TESTABLE ({res.not_self_testable}); its controls: " + (
                "ok" if all(c[1] for c in res.checks if c[3]) else "FAIL")
            ok &= all(c[1] for c in res.checks if c[3])
        elif a.self_test and res.pid != "H-harness":
            # Against the pre-Plan L engine each local-first probe must fail on a property while its
            # controls pass: proof that it discriminates, for the right reason.
            verdict = "DISCRIMINATES (controls pass, a property fails)" if res.discriminates else "DOES NOT DISCRIMINATE"
            ok &= res.discriminates
        else:
            verdict = "PASS" if res.passed else "FAIL"
            ok &= res.passed
        lines.append(f"{res.pid} (stage {res.stage}; {res.ref}): {verdict}")
        for name, good, detail, control in res.checks:
            tag = "control " if control else "property"
            lines.append(f"  {'ok  ' if good else 'FAIL'} {tag} {name}"
                         + (f"  [{str(detail)[-300:]}]" if not good and detail else ""))
    mode = "self-test against the frozen pre-Plan L engine" if a.self_test else f"candidate, accepted stage {a.accepted_stage}"
    report = [f"Plan L kit probes ({mode}): {'PASS' if ok else 'FAIL'}", ""] + lines
    (out / "PROBES.txt").write_text("\n".join(report) + "\n")
    print("\n".join(report))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
