"""A small Stage-1 CLI for exercising configuration and stage creation.

This is intentionally minimal: config validation and stage skeleton
creation, so the primitives above have an obvious entry point. Building a
full command surface (invocation, sparring, acceptance, loops) is later
work.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent_sparring.activity import ActivityLog
from agent_sparring.acceptance import (
    AcceptanceError,
    accept_candidate,
    freeze_candidate,
)
from agent_sparring.config import (
    CONFIG_FILENAME,
    ProjectConfigError,
    load_project_config,
    load_project_markdown,
)
from agent_sparring.git_context import GitContextError, is_ignored
from agent_sparring.handoff import generate_handoff
from agent_sparring.loop import DEFAULT_MAX_SEND_BACK_CYCLES, LoopError, run_unattended_loop
from agent_sparring.human_gate import HumanGate, HumanGateError
from agent_sparring.manifest import ManifestError
from agent_sparring.plan import (
    PLANS_DIRNAME,
    PlanError,
    PlanRunError,
    PlanRunResult,
    PlanRunStatus,
    load_plan_source,
    plan_label,
    plan_state_not_ignored_message,
    plan_state_path,
    resume_plan,
    start_plan,
)
from agent_sparring.providers.claude_cli import (
    DEFAULT_PERMISSION_MODE,
    PROVIDER_ID as CLAUDE_PROVIDER_ID,
    ClaudeCliAdapter,
)
from agent_sparring.providers import ProviderError
from agent_sparring.providers.codex_cli import PROVIDER_ID as CODEX_PROVIDER_ID, CodexCliAdapter
from agent_sparring.routing import RoutingAction, RoutingResult, RoutingResultError
from agent_sparring.sparring_agent import SparringAgentRunError, run_sparring_agent
from agent_sparring.sparring_exchange import record_sparring
from agent_sparring.sparring_prompt import build_sparring_prompt
from agent_sparring.stage import Stage, StageError
from agent_sparring.stage_agent import StageAgentRunError, run_stage_agent
from agent_sparring.stage_prompt import build_stage_prompt


def _cmd_check_config(args: argparse.Namespace) -> int:
    sparring_dir = Path(args.sparring_dir)
    try:
        config = load_project_config(sparring_dir)
    except ProjectConfigError as exc:
        print(f"invalid project.toml: {exc}", file=sys.stderr)
        return 1
    markdown = load_project_markdown(sparring_dir)
    print(f"project: {config.project}")
    print(f"repo_root: {config.repo_root}")
    print(f"commands: {dict(config.commands)}")
    print(f"stage_agent_provider: {config.stage_agent_provider}")
    print(f"sparring_agent_provider: {config.sparring_agent_provider}")
    print(f"default_sparring_mode: {config.default_sparring_mode}")
    print(f"PROJECT.md present: {markdown is not None}")
    return _report_workflow_state_ignored(args, sparring_dir)


def _report_workflow_state_ignored(args: argparse.Namespace, sparring_dir: Path) -> int:
    """Say, before anyone spends a provider turn, whether git can see the two
    workflow-state directories.

    ``.sparring/stages/`` and ``.sparring/plans/`` are rewritten constantly
    while a run is in progress, so either of them being visible to git turns
    every ``freeze-candidate`` into "the working tree holds changes that
    commit does not represent" -- part-way through a run, after the tokens
    are spent. The plan runner refuses up front for exactly that reason (see
    :func:`agent_sparring.plan.plan_state_not_ignored_message`); reporting it
    here is how a project finds out without having to trip the refusal.
    """

    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
    except ProjectConfigError:
        return 1
    problems: list[str] = []
    for what, probe in (
        ("stage artifacts", sparring_dir / "stages" / "any-stage" / "state.json"),
        ("plan-run state", sparring_dir / PLANS_DIRNAME / "any-plan.json"),
    ):
        try:
            probe.resolve().relative_to(repo_root.resolve())
        except ValueError:
            print(f"{what} git-ignored: not applicable (outside {repo_root})")
            continue
        try:
            ignored = is_ignored(repo_root, probe)
        except GitContextError as exc:
            print(f"{what} git-ignored: unknown ({exc})", file=sys.stderr)
            continue
        print(f"{what} git-ignored: {'yes' if ignored else 'NO'}")
        if not ignored:
            problems.append(what)
    if problems:
        print("", file=sys.stderr)
        print(
            plan_state_not_ignored_message(
                repo_root, sparring_dir / PLANS_DIRNAME / "any-plan.json"
            )
            if "plan-run state" in problems
            else (
                f"stage artifacts under {sparring_dir}/stages/ are not ignored by git; add a "
                f"line '.sparring/stages/' to {repo_root}/.gitignore"
            ),
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_new_stage(args: argparse.Namespace) -> int:
    sparring_dir = Path(args.sparring_dir)
    try:
        # The brief is read before anything is created or resolved on disk:
        # an unreadable file must leave no partially created stage behind.
        brief: str | None = None
        if args.brief_file is not None:
            try:
                # newline="" keeps the file's own line endings; the brief is verbatim.
                with open(args.brief_file, encoding="utf-8", newline="") as handle:
                    brief = handle.read()
            except (OSError, UnicodeDecodeError) as exc:
                raise StageError(f"could not read --brief-file {args.brief_file!r}: {exc}") from exc
        stage = Stage.resolve(sparring_dir, args.stage_id)
        stage.create(exist_ok=args.exist_ok, brief=brief)
    except StageError as exc:
        print(f"could not create stage: {exc}", file=sys.stderr)
        return 1
    print(f"created stage {stage.stage_id!r} at {stage.directory}")
    return 0


def _resolve_repo_root(args: argparse.Namespace, sparring_dir: Path) -> Path:
    """Precedence: explicit --repo-root > configured [repo].root > the
    parent of --sparring-dir. A relative configured root is resolved
    against the project root (the parent of --sparring-dir), never CWD.

    An explicit --repo-root short-circuits before project.toml is even
    looked at, so it works even when project.toml is missing or malformed.
    A missing project.toml is a legitimate "use the default" case; a
    project.toml that exists but fails to parse is not — that is let
    through as :class:`ProjectConfigError` rather than silently falling
    back, since silently picking a different repo root would be worse than
    failing loudly.
    """

    if args.repo_root:
        return Path(args.repo_root)

    project_root = sparring_dir.resolve().parent
    if not (sparring_dir / CONFIG_FILENAME).is_file():
        return project_root

    config = load_project_config(sparring_dir)
    configured = Path(config.repo_root)
    return configured if configured.is_absolute() else (project_root / configured).resolve()


def _cmd_handoff(args: argparse.Namespace) -> int:
    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        stage = Stage.resolve(sparring_dir, args.stage_id)
        if not stage.exists():
            raise StageError(f"stage {args.stage_id!r} does not exist at {stage.directory}")
        content = generate_handoff(
            stage,
            repo_root,
            stage_goal=args.goal or stage.read_brief(),
            claims=args.claims,
            test_evidence=args.test_evidence,
            self_contained=args.self_contained,
            check_pushed=not args.no_check_pushed,
            base_sha=args.base_sha,
            candidate_sha=args.candidate_sha,
        )
    except (StageError, GitContextError, ProjectConfigError) as exc:
        print(f"could not generate handoff: {exc}", file=sys.stderr)
        return 1
    print(content)
    print(f"wrote handoff to {stage.directory / 'handoff.md'}", file=sys.stderr)
    return 0


def _cmd_record_sparring(args: argparse.Namespace) -> int:
    sparring_dir = Path(args.sparring_dir)
    try:
        stage = Stage.resolve(sparring_dir, args.stage_id)
        if not stage.exists():
            raise StageError(f"stage {args.stage_id!r} does not exist at {stage.directory}")
        gate: HumanGate | None = None
        if args.human_gate_file:
            try:
                gate = HumanGate.from_dict(
                    json.loads(Path(args.human_gate_file).read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise StageError(
                    f"could not read --human-gate-file {args.human_gate_file!r}: {exc}"
                ) from exc
        result = RoutingResult(
            action=RoutingAction.from_str(args.action),
            summary=args.summary,
            needs_you_reason=args.needs_you_reason,
            details={"deferred": args.deferred} if args.deferred else {},
            human_gate=gate,
        )
        content = record_sparring(stage, result, findings=args.findings or "")
    except (StageError, RoutingResultError, HumanGateError) as exc:
        print(f"could not record sparring outcome: {exc}", file=sys.stderr)
        return 1
    print(content)
    print(f"wrote sparring outcome to {stage.directory / 'sparring.md'}", file=sys.stderr)
    return 0


def _resolve_stage_provider(explicit_provider: str | None, sparring_dir: Path) -> str:
    """Precedence: explicit override > configured [agents.stage].provider >
    ``claude-cli`` (the initial default, per the project plan)."""

    if explicit_provider:
        return explicit_provider
    if (sparring_dir / CONFIG_FILENAME).is_file():
        config = load_project_config(sparring_dir)
        if config.stage_agent_provider:
            return config.stage_agent_provider
    return "claude-cli"


def _resolve_self_check(sparring_dir: Path) -> bool:
    """Whether the stage prompt should include the optional self-check
    section, per project.toml's ``[stage] self_check`` (default false).

    Config-only by design: there is deliberately no CLI flag for this (see
    the project plan's Stage 5 self-check note) -- a project either wants
    it on for every unattended run or does not, and toggling it per
    invocation would not simplify anything here.
    """

    if (sparring_dir / CONFIG_FILENAME).is_file():
        config = load_project_config(sparring_dir)
        return config.stage_self_check
    return False


def _cmd_run_stage(args: argparse.Namespace) -> int:
    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        stage = Stage.resolve(sparring_dir, args.stage_id)
        if not stage.exists():
            raise StageError(f"stage {args.stage_id!r} does not exist at {stage.directory}")

        self_check = _resolve_self_check(sparring_dir)

        if args.dry_run:
            state = stage.read_state()
            prompt = build_stage_prompt(
                stage,
                sparring_dir,
                resume=state.implementation_session_id is not None,
                expected_branch=args.expected_branch,
                self_check=self_check,
            )
            print(prompt)
            return 0

        provider = _resolve_stage_provider(args.provider, sparring_dir)
        if provider != "claude-cli":
            raise StageError(
                f"unsupported stage agent provider {provider!r}; only 'claude-cli' is "
                "implemented so far"
            )
        adapter = ClaudeCliAdapter(
            repo_root=repo_root,
            executable=args.claude_executable,
            permission_mode=args.permission_mode,
            model=args.model,
            activity=stage.activity_log().bind("stage", provider=CLAUDE_PROVIDER_ID),
        )
        run_result = run_stage_agent(
            stage,
            sparring_dir,
            repo_root,
            adapter,
            expected_branch=args.expected_branch,
            self_check=self_check,
        )
    except (StageError, StageAgentRunError, ProjectConfigError, GitContextError) as exc:
        print(f"could not run stage agent: {exc}", file=sys.stderr)
        return 1

    print(run_result.result.text)
    print(
        f"session_id={run_result.result.session_id} resumed={run_result.resumed} "
        f"branch={run_result.branch}",
        file=sys.stderr,
    )
    return 1 if run_result.result.is_error else 0


def _resolve_sparring_provider(explicit_provider: str | None, sparring_dir: Path) -> str:
    """Precedence: explicit override > configured [agents.sparring].provider
    > ``codex-cli`` (the initial default, per the Stage 4 capability probe)."""

    if explicit_provider:
        return explicit_provider
    if (sparring_dir / CONFIG_FILENAME).is_file():
        config = load_project_config(sparring_dir)
        if config.sparring_agent_provider:
            return config.sparring_agent_provider
    return "codex-cli"


def _cmd_run_sparring(args: argparse.Namespace) -> int:
    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        stage = Stage.resolve(sparring_dir, args.stage_id)
        if not stage.exists():
            raise StageError(f"stage {args.stage_id!r} does not exist at {stage.directory}")

        if args.dry_run:
            state = stage.read_state()
            prompt = build_sparring_prompt(
                stage,
                sparring_dir,
                resume=state.sparring_session_id is not None,
                expected_branch=args.expected_branch,
            )
            print(prompt)
            return 0

        provider = _resolve_sparring_provider(args.provider, sparring_dir)
        if provider != "codex-cli":
            raise StageError(
                f"unsupported sparring agent provider {provider!r}; only 'codex-cli' is "
                "implemented so far"
            )
        # No --sandbox override is exposed here: CodexCliAdapter has no
        # sandbox field or extra_args passthrough at all -- read-only is
        # hard-coded (see providers/codex_cli.py), so there is no
        # writable-sandbox escape hatch for this adapter.
        adapter = CodexCliAdapter(
            repo_root=repo_root,
            executable=args.codex_executable,
            model=args.model,
            activity=stage.activity_log().bind("sparrer", provider=CODEX_PROVIDER_ID),
        )
        run_result = run_sparring_agent(
            stage, sparring_dir, repo_root, adapter, expected_branch=args.expected_branch
        )
    except (
        StageError,
        SparringAgentRunError,
        ProjectConfigError,
        GitContextError,
        ProviderError,
    ) as exc:
        print(f"could not run sparring agent: {exc}", file=sys.stderr)
        return 1

    print(run_result.sparring)
    print(
        f"session_id={run_result.result.session_id} resumed={run_result.resumed} "
        f"action={run_result.routing.action.value}",
        file=sys.stderr,
    )
    return 0


def _require_loop_providers(args: argparse.Namespace, sparring_dir: Path) -> None:
    """Refuse (:class:`StageError`) any provider selection the loop commands
    cannot serve yet. Separate from :func:`_build_loop_adapters` so run-plan
    can check it once up front, before recording any run state, even though
    its adapters are built later, per planned stage."""

    stage_provider = _resolve_stage_provider(args.stage_provider, sparring_dir)
    if stage_provider != "claude-cli":
        raise StageError(
            f"unsupported stage agent provider {stage_provider!r}; only "
            "'claude-cli' is implemented so far"
        )
    sparring_provider = _resolve_sparring_provider(args.sparring_provider, sparring_dir)
    if sparring_provider != "codex-cli":
        raise StageError(
            f"unsupported sparring agent provider {sparring_provider!r}; only "
            "'codex-cli' is implemented so far"
        )


def _build_loop_adapters(
    args: argparse.Namespace,
    sparring_dir: Path,
    repo_root: Path,
    *,
    activity_log: ActivityLog | None = None,
) -> tuple[ClaudeCliAdapter, CodexCliAdapter]:
    """The stage and sparring adapters for run-loop / run-plan / resume-plan,
    from the shared provider flags (see :func:`_add_loop_arguments`).

    ``activity_log`` is the stage's observational ``activity.jsonl`` (see
    :mod:`agent_sparring.activity`); both adapters append their provider
    stream to it. Observational only, never read back. ``None`` produces
    adapters that emit no provider telemetry.
    """

    _require_loop_providers(args, sparring_dir)
    stage_adapter = ClaudeCliAdapter(
        repo_root=repo_root,
        executable=args.claude_executable,
        permission_mode=args.permission_mode,
        model=args.stage_model,
        activity=(
            activity_log.bind("stage", provider=CLAUDE_PROVIDER_ID)
            if activity_log is not None
            else None
        ),
    )

    # No --sandbox override is exposed here either, for the same reason
    # as run-sparring: CodexCliAdapter has no sandbox field at all.
    sparring_adapter = CodexCliAdapter(
        repo_root=repo_root,
        executable=args.codex_executable,
        model=args.sparring_model,
        activity=(
            activity_log.bind("sparrer", provider=CODEX_PROVIDER_ID)
            if activity_log is not None
            else None
        ),
    )
    return stage_adapter, sparring_adapter


def _cmd_run_loop(args: argparse.Namespace) -> int:
    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        stage = Stage.resolve(sparring_dir, args.stage_id)
        if not stage.exists():
            raise StageError(f"stage {args.stage_id!r} does not exist at {stage.directory}")

        self_check = _resolve_self_check(sparring_dir)
        # Both adapters append to this stage's activity.jsonl (see
        # agent_sparring.activity): observational only, never read back.
        stage_adapter, sparring_adapter = _build_loop_adapters(
            args, sparring_dir, repo_root, activity_log=stage.activity_log()
        )

        loop_result = run_unattended_loop(
            stage,
            sparring_dir,
            repo_root,
            stage_adapter,
            sparring_adapter,
            expected_branch=args.expected_branch,
            max_send_back_cycles=args.max_send_back_cycles,
            self_check=self_check,
        )
    except (StageError, LoopError, ProjectConfigError, GitContextError, ProviderError) as exc:
        print(f"could not run unattended loop: {exc}", file=sys.stderr)
        return 1

    print(loop_result.routing.summary)
    print(
        f"outcome={loop_result.outcome.value} cycles={len(loop_result.cycles)} "
        f"send_back_count={loop_result.send_back_count}",
        file=sys.stderr,
    )
    return 0


def _plan_input_args(args: argparse.Namespace) -> str:
    """How this plan run is addressed on the command line, for the copyable
    resume hint: the manifest flag or the plan's own path."""

    return f"--manifest {args.manifest}" if args.manifest else str(args.plan_path)


def _report_plan_result(
    result: PlanRunResult, args: argparse.Namespace, sparring_dir: Path
) -> None:
    if result.status is PlanRunStatus.COMPLETE:
        print(f"plan complete: {result.plan}")
        for stage_id, sha in result.accepted:
            print(f"  accepted {stage_id} at {sha}")
        return

    stage = Stage.resolve(sparring_dir, result.stage_id)

    # A PAUSED result usually carries either the verdict this run produced
    # or, when the stage was already stopped for a human before the run
    # reached it, the one that was already on disk. They print the same way.
    #
    # One pause carries neither, and it is not a verdict at all: a run
    # bounded by --stop-after-stage accepted everything it was asked to and
    # then stopped before entering the next stage, which was never run and
    # has nothing to report. Printing a verdict's shape for it (or asserting
    # one exists) would be wrong twice over -- there is no review to show,
    # and nothing here failed.
    routing = result.routing or result.recorded
    if routing is None:
        print(f"plan paused: {result.plan}")
        for stage_id, sha in result.accepted:
            print(f"  accepted {stage_id} at {sha}")
        print(f"next stage: {result.stage_id} ({stage.directory}); not started")
        print()
        print("Continue when you are ready:")
        print(
            f"  sparring resume-plan {_plan_input_args(args)} "
            f"--repo-root {args.repo_root or '.'} --expected-branch {args.expected_branch}"
        )
        return

    print(f"plan paused: {result.plan}")
    print(f"stage: {result.stage_id} ({stage.directory})")
    print(f"action: {routing.action.value}")
    if result.recorded is not None:
        print("this stage was already waiting for you; nothing was run and the recorded")
        print("review is unchanged.")
    if routing.needs_you_reason:
        print(f"reason: {routing.needs_you_reason}")
    print(f"summary: {routing.summary}")
    if routing.human_gate is not None:
        gate = routing.human_gate
        print()
        print(f"required before READY ({gate.category}): {gate.title}")
        for position, check in enumerate(gate.checks, start=1):
            print(f"  {position}. [{check.id}] {check.instruction}")
            print(f"     pass when: {check.pass_criteria}")
            if check.source:
                print(f"     defined in: {check.source}")
    print()
    print(stage.read_sparring().rstrip())
    print()
    resume = (
        f"sparring resume-plan {_plan_input_args(args)} --repo-root {args.repo_root or '.'} "
        f"--expected-branch {args.expected_branch}"
    )
    if routing.action is RoutingAction.ESCALATE:
        print("To spar this stage elsewhere, build a packet from the current handoff:")
        print(f"  handoff: {stage.directory / 'handoff.md'}")
        print(
            f"  sparring handoff {result.stage_id} --repo-root {args.repo_root or '.'} "
            "--claims '<claims>' --self-contained"
        )
        print(
            f"  sparring record-sparring {result.stage_id} --action <ACTION> "
            "--summary '...' --findings '...'"
        )
        print("Then either accept by hand (freeze-candidate, accept-candidate) and resume,")
        print("or resume with the external verdict as evidence:")
    else:
        print("When the check/answer above is done, resume the same stage with it:")
    print(f"  {resume} --evidence '<what you found or decided>'")


def _plan_source(args: argparse.Namespace, repo_root: Path):
    """The plan input for run-plan / resume-plan: exactly one of the
    positional Markdown plan or ``--manifest``."""

    if bool(args.plan_path) == bool(args.manifest):
        raise PlanError(
            "give exactly one plan input: the reviewed Markdown plan as a positional "
            "argument, or --manifest with an execution manifest"
        )
    path = Path(args.manifest or args.plan_path)
    return load_plan_source(path, repo_root, manifest=bool(args.manifest))


def _run_plan_command(args: argparse.Namespace, *, resume: bool) -> int:
    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        source = _plan_source(args, repo_root)
        self_check = _resolve_self_check(sparring_dir)
        # Provider selection is checked once, before any run state exists;
        # the adapters themselves are built per planned stage (below) so
        # each stage's provider telemetry lands in that stage's own
        # activity.jsonl -- observational only, never read back.
        _require_loop_providers(args, sparring_dir)

        def make_adapters(stage: Stage) -> tuple[ClaudeCliAdapter, CodexCliAdapter]:
            return _build_loop_adapters(
                args, sparring_dir, repo_root, activity_log=stage.activity_log()
            )

        def report(message: str) -> None:
            print(message, file=sys.stderr)

        common = dict(
            expected_branch=args.expected_branch,
            max_send_back_cycles=args.max_send_back_cycles,
            self_check=self_check,
            stop_after_stage=args.stop_after_stage,
            report=report,
        )
        if resume:
            result = resume_plan(
                source,
                sparring_dir,
                repo_root,
                make_adapters,
                evidence=args.evidence,
                **common,
            )
        else:
            result = start_plan(
                source, sparring_dir, repo_root, make_adapters, adopt=args.adopt, **common
            )
    except (
        PlanError,
        PlanRunError,
        ManifestError,
        StageError,
        ProjectConfigError,
        GitContextError,
        ProviderError,
    ) as exc:
        print(f"could not {'resume' if resume else 'run'} plan: {exc}", file=sys.stderr)
        return 1

    _report_plan_result(result, args, sparring_dir)
    return 0


def _cmd_run_plan(args: argparse.Namespace) -> int:
    return _run_plan_command(args, resume=False)


def _cmd_resume_plan(args: argparse.Namespace) -> int:
    return _run_plan_command(args, resume=True)


def _resolved_stage(args: argparse.Namespace, sparring_dir: Path) -> Stage:
    stage = Stage.resolve(sparring_dir, args.stage_id)
    if not stage.exists():
        raise StageError(f"stage {args.stage_id!r} does not exist at {stage.directory}")
    return stage


def _cmd_freeze_candidate(args: argparse.Namespace) -> int:
    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        stage = _resolved_stage(args, sparring_dir)
        result = freeze_candidate(
            stage, sparring_dir, repo_root, expected_branch=args.expected_branch
        )
    except (StageError, AcceptanceError, ProjectConfigError, GitContextError) as exc:
        print(f"could not freeze candidate: {exc}", file=sys.stderr)
        return 1

    print(f"frozen candidate: {result.candidate_sha}")
    print(f"branch: {result.branch}")
    print(f"pushed: {result.push_detail}")
    if result.ignored_dirty_paths:
        print(
            "ignored workflow-artifact changes under "
            f"{sparring_dir}: {', '.join(result.ignored_dirty_paths)}"
        )
    return 0


def _cmd_accept_candidate(args: argparse.Namespace) -> int:
    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        stage = _resolved_stage(args, sparring_dir)
        result = accept_candidate(stage, repo_root, expected_branch=args.expected_branch)
    except (StageError, AcceptanceError, ProjectConfigError, GitContextError) as exc:
        print(f"could not accept candidate: {exc}", file=sys.stderr)
        return 1

    print(f"accepted candidate: {result.candidate_sha}")
    print(f"branch: {result.branch}")
    return 0


def _add_loop_arguments(
    parser: argparse.ArgumentParser, repo_root_help: str, *, branch_help: str
) -> None:
    """The flags run-loop, run-plan and resume-plan share: repo root,
    expected branch, provider selection and the SEND_BACK runaway limit."""

    parser.add_argument("--repo-root", default=None, help=repo_root_help)
    parser.add_argument("--expected-branch", required=True, help=branch_help)
    parser.add_argument(
        "--stage-provider",
        default=None,
        help="stage agent provider override (default: project.toml's [agents.stage].provider, else claude-cli)",
    )
    parser.add_argument(
        "--sparring-provider",
        default=None,
        help="sparring agent provider override (default: project.toml's [agents.sparring].provider, else codex-cli)",
    )
    parser.add_argument(
        "--claude-executable",
        default="claude",
        help="claude CLI executable to invoke (default: claude)",
    )
    parser.add_argument(
        "--codex-executable",
        default="codex",
        help="codex CLI executable to invoke (default: codex)",
    )
    parser.add_argument("--stage-model", default=None, help="model override for the stage agent")
    parser.add_argument(
        "--sparring-model", default=None, help="model override for the sparring agent"
    )
    parser.add_argument(
        "--permission-mode",
        default=DEFAULT_PERMISSION_MODE,
        help=f"claude CLI --permission-mode value (default: {DEFAULT_PERMISSION_MODE})",
    )
    parser.add_argument(
        "--max-send-back-cycles",
        type=int,
        default=DEFAULT_MAX_SEND_BACK_CYCLES,
        help=(
            "runaway limit: stop with an error after this many SEND_BACK "
            f"cycles without reaching READY/NEEDS_YOU/ESCALATE (default: "
            f"{DEFAULT_MAX_SEND_BACK_CYCLES})"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sparring")
    parser.add_argument(
        "--sparring-dir",
        default=".sparring",
        help="path to the project's .sparring directory (default: .sparring)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_config = subparsers.add_parser(
        "check-config",
        help=(
            "load and print project.toml / PROJECT.md status, and whether git can see the "
            "workflow-state directories (which would break every freeze mid-run)"
        ),
    )
    check_config.add_argument(
        "--repo-root",
        default=None,
        help=(
            "repository root; overrides project.toml's [repo].root if set "
            "(default: [repo].root from project.toml, resolved against the "
            "project root, else the parent of --sparring-dir)"
        ),
    )
    check_config.set_defaults(func=_cmd_check_config)

    new_stage = subparsers.add_parser("new-stage", help="create a new stage skeleton")
    new_stage.add_argument("stage_id")
    new_stage.add_argument(
        "--exist-ok",
        action="store_true",
        help="do not fail if the stage directory already exists",
    )
    new_stage.add_argument(
        "--brief-file",
        default=None,
        metavar="PATH",
        help=(
            "UTF-8 Markdown to use verbatim as the initial brief.md instead of the "
            "template (never overwrites an existing brief.md)"
        ),
    )
    new_stage.set_defaults(func=_cmd_new_stage)

    handoff = subparsers.add_parser(
        "handoff", help="generate and write handoff.md for a stage"
    )
    handoff.add_argument("stage_id")
    handoff.add_argument("--claims", required=True, help="implementation-agent claims")
    handoff.add_argument(
        "--goal", default=None, help="stage goal text (default: this stage's brief.md)"
    )
    handoff.add_argument("--test-evidence", default=None, help="test/build evidence text")
    handoff.add_argument(
        "--base-sha",
        default=None,
        help="base commit (default: this stage's recorded state.json base_sha)",
    )
    handoff.add_argument(
        "--candidate-sha",
        default=None,
        help="candidate commit (default: recorded state.json candidate_sha, else HEAD)",
    )
    handoff.add_argument(
        "--repo-root",
        default=None,
        help=(
            "repository root; overrides project.toml's [repo].root if set "
            "(default: [repo].root from project.toml, resolved against the "
            "project root, else the parent of --sparring-dir)"
        ),
    )
    handoff.add_argument(
        "--self-contained",
        action="store_true",
        help="embed a diff for a sparrer without repository access (default: thin)",
    )
    handoff.add_argument(
        "--no-check-pushed",
        action="store_true",
        help="skip verifying the candidate commit is pushed to its remote",
    )
    handoff.set_defaults(func=_cmd_handoff)

    record_sparring_parser = subparsers.add_parser(
        "record-sparring", help="render and write sparring.md for a routing outcome"
    )
    record_sparring_parser.add_argument("stage_id")
    record_sparring_parser.add_argument(
        "--action",
        required=True,
        choices=[member.value for member in RoutingAction],
    )
    record_sparring_parser.add_argument("--summary", required=True)
    record_sparring_parser.add_argument("--needs-you-reason", default=None)
    record_sparring_parser.add_argument("--findings", default=None)
    record_sparring_parser.add_argument("--deferred", default=None)
    record_sparring_parser.add_argument(
        "--human-gate-file",
        default=None,
        metavar="PATH",
        help=(
            "JSON file holding the structured human gate (category/title/checks) for a "
            "NEEDS_YOU outcome; required for NEEDS_YOU and refused for every other action"
        ),
    )
    record_sparring_parser.set_defaults(func=_cmd_record_sparring)

    run_stage = subparsers.add_parser(
        "run-stage", help="start or resume the stage-agent adapter for a stage"
    )
    run_stage.add_argument("stage_id")
    run_stage.add_argument(
        "--repo-root",
        default=None,
        help=(
            "repository root; overrides project.toml's [repo].root if set "
            "(default: [repo].root from project.toml, resolved against the "
            "project root, else the parent of --sparring-dir)"
        ),
    )
    run_stage.add_argument(
        "--expected-branch",
        required=True,
        help=(
            "the branch this unattended run is meant to modify; required, and "
            "included in the stage prompt so the agent is told not to switch "
            "branches"
        ),
    )
    run_stage.add_argument(
        "--provider",
        default=None,
        help=(
            "stage agent provider (default: project.toml's "
            "[agents.stage].provider, else claude-cli)"
        ),
    )
    run_stage.add_argument(
        "--claude-executable",
        default="claude",
        help="claude CLI executable to invoke (default: claude)",
    )
    run_stage.add_argument("--model", default=None, help="model override for the provider")
    run_stage.add_argument(
        "--permission-mode",
        default=DEFAULT_PERMISSION_MODE,
        help=f"claude CLI --permission-mode value (default: {DEFAULT_PERMISSION_MODE})",
    )
    run_stage.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "print the stage prompt the NEXT turn would be given, without invoking any "
            "provider. Not a record of a turn that already ran: it re-reads files that "
            "change between turns, and a stage whose first turn is still in flight "
            "already has a session id, so it renders as a resume. Every turn's exact "
            "prompt is captured under the stage's prompts/ directory"
        ),
    )
    run_stage.set_defaults(func=_cmd_run_stage)

    run_sparring = subparsers.add_parser(
        "run-sparring", help="start or resume the sparring-agent adapter for a stage"
    )
    run_sparring.add_argument("stage_id")
    run_sparring.add_argument(
        "--repo-root",
        default=None,
        help=(
            "repository root; overrides project.toml's [repo].root if set "
            "(default: [repo].root from project.toml, resolved against the "
            "project root, else the parent of --sparring-dir)"
        ),
    )
    run_sparring.add_argument(
        "--expected-branch",
        required=True,
        help=(
            "the branch this sparring run is meant to review; required, and "
            "verified against the actual worktree before and after the "
            "provider turn (matches --expected-branch on run-stage)"
        ),
    )
    run_sparring.add_argument(
        "--provider",
        default=None,
        help=(
            "sparring agent provider (default: project.toml's "
            "[agents.sparring].provider, else codex-cli)"
        ),
    )
    run_sparring.add_argument(
        "--codex-executable",
        default="codex",
        help="codex CLI executable to invoke (default: codex)",
    )
    run_sparring.add_argument("--model", default=None, help="model override for the provider")
    # No --sandbox flag: CodexCliAdapter has no sandbox field at all --
    # read-only is hard-coded (see providers/codex_cli.py).
    run_sparring.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "print the sparring prompt the NEXT turn would be given, without invoking "
            "any provider. Not a record of a turn that already ran; see run-stage "
            "--dry-run and the stage's prompts/ directory"
        ),
    )
    run_sparring.set_defaults(func=_cmd_run_sparring)

    repo_root_help = (
        "repository root; overrides project.toml's [repo].root if set "
        "(default: [repo].root from project.toml, resolved against the "
        "project root, else the parent of --sparring-dir)"
    )

    run_loop = subparsers.add_parser(
        "run-loop",
        help="run the unattended stage<->sparring loop for a stage until READY/NEEDS_YOU/ESCALATE",
    )
    run_loop.add_argument("stage_id")
    _add_loop_arguments(
        run_loop,
        repo_root_help,
        branch_help="the branch this unattended loop is meant to modify and review; required",
    )
    run_loop.set_defaults(func=_cmd_run_loop)

    manifest_help = (
        "path to an execution manifest (JSON) instead of a Markdown plan: explicit stage "
        "ids, labels such as 'Stage 3C', exact briefs and the execution order, produced by "
        "a caller that already interpreted the plan document"
    )

    run_plan = subparsers.add_parser(
        "run-plan",
        help=(
            "run a multi-stage plan unattended, from a reviewed Markdown plan (each "
            "'## Stage <n> — <title>' section becomes a stage) or from --manifest; READY "
            "freezes and accepts the exact pushed SHA and the next stage starts; "
            "NEEDS_YOU/ESCALATE/failure pauses"
        ),
    )
    run_plan.add_argument(
        "plan_path",
        nargs="?",
        default=None,
        help="path to the reviewed plan Markdown file (omit when using --manifest)",
    )
    run_plan.add_argument("--manifest", default=None, metavar="PATH", help=manifest_help)
    run_plan.add_argument(
        "--adopt",
        action="store_true",
        help=(
            "continue a sequence whose stages already exist on disk instead of refusing "
            "them: each is checked (ACCEPTED, or an identical brief) and reported, never "
            "silently inherited"
        ),
    )
    _add_loop_arguments(
        run_plan,
        repo_root_help,
        branch_help="the feature branch every stage of this plan must modify; required",
    )
    run_plan.add_argument(
        "--stop-after-stage",
        default=None,
        metavar="STAGE_ID",
        help=(
            "stop once this stage is accepted, instead of continuing into the next one: "
            "the run pauses with its position already advanced, and nothing is run, "
            "created or briefed for the next stage. For taking a managed plan one stage "
            "at a time without giving up its position, digest and acceptance handling"
        ),
    )
    run_plan.set_defaults(func=_cmd_run_plan)

    resume_plan_parser = subparsers.add_parser(
        "resume-plan",
        help=(
            "continue a paused plan run at its current stage, optionally recording the "
            "human's answer/check result first"
        ),
    )
    resume_plan_parser.add_argument(
        "plan_path",
        nargs="?",
        default=None,
        help="path to the same reviewed plan file (omit when using --manifest)",
    )
    resume_plan_parser.add_argument(
        "--manifest", default=None, metavar="PATH", help=manifest_help
    )
    _add_loop_arguments(
        resume_plan_parser,
        repo_root_help,
        branch_help="the feature branch the plan run was started for; required",
    )
    resume_plan_parser.add_argument(
        "--evidence",
        default=None,
        help=(
            "the human's answer, manual/device check result, external-condition result "
            "or scope approval; appended to the current stage's notes.md under "
            "'## Human evidence', after which the SPARRER resumes against the unchanged "
            "candidate (the stage agent is not started to deliver an answer)"
        ),
    )
    resume_plan_parser.add_argument(
        "--stop-after-stage",
        default=None,
        metavar="STAGE_ID",
        help=(
            "stop once this stage is accepted, instead of continuing into the next one: "
            "the run pauses with its position already advanced, and nothing is run, "
            "created or briefed for the next stage. For taking a managed plan one stage "
            "at a time without giving up its position, digest and acceptance handling"
        ),
    )
    resume_plan_parser.set_defaults(func=_cmd_resume_plan, adopt=False)

    freeze = subparsers.add_parser(
        "freeze-candidate",
        help=(
            "freeze the repository's exact current HEAD as this stage's "
            "acceptance candidate (requires a clean, pushed worktree)"
        ),
    )
    freeze.add_argument("stage_id")
    freeze.add_argument("--repo-root", default=None, help=repo_root_help)
    freeze.add_argument(
        "--expected-branch",
        required=True,
        help=(
            "the branch this candidate belongs to; required, verified "
            "against the worktree and used for the pushed/reachable check"
        ),
    )
    freeze.set_defaults(func=_cmd_freeze_candidate)

    accept = subparsers.add_parser(
        "accept-candidate",
        help=(
            "accept exactly this stage's frozen candidate; refuses as stale "
            "if HEAD has moved since the freeze"
        ),
    )
    accept.add_argument("stage_id")
    accept.add_argument("--repo-root", default=None, help=repo_root_help)
    accept.add_argument(
        "--expected-branch",
        required=True,
        help="the branch the frozen candidate belongs to; required",
    )
    accept.set_defaults(func=_cmd_accept_candidate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
