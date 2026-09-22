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
from agent_sparring.agent_config import (
    EffectiveAgents,
    ROLES,
    ROLE_SPARRING,
    ROLE_STAGE,
    ResolvedAgentConfig,
    RoleOverrides,
    resolve_agent_configs,
    resolve_role_config,
)
from agent_sparring.config_edit import RoleEdit, apply_role_edit
from agent_sparring.config import (
    CONFIG_FILENAME,
    ProjectConfig,
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
    resume_plan,
    start_plan,
)
from agent_sparring.deferred_gate import (
    CheckOutcome,
    DeferredAnswer,
    DeferredGateError,
    DeferredVerificationRequired,
)
from agent_sparring.push_gate import PUSH_AUTHORIZATION_REQUIRED
from agent_sparring.providers.claude_cli import (
    DEFAULT_PERMISSION_MODE,
    PROVIDER_ID as CLAUDE_PROVIDER_ID,
    ClaudeCliAdapter,
)
from agent_sparring.providers import ProviderError
from agent_sparring.providers.codex_cli import PROVIDER_ID as CODEX_PROVIDER_ID, CodexCliAdapter
from agent_sparring.recovery import RecoveryError, reopen_for_failed_check, reset_stage
from agent_sparring.routing import RoutingAction, RoutingResult, RoutingResultError
from agent_sparring.sparring_agent import SparringAgentRunError, run_sparring_agent
from agent_sparring.sparring_exchange import record_sparring
from agent_sparring.sparring_prompt import build_sparring_prompt
from agent_sparring.stage import Stage, StageError, StageMode
from agent_sparring.stage_agent import StageAgentRunError, run_stage_agent
from agent_sparring.stage_prompt import build_stage_prompt
from agent_sparring.templates import render_project_config

# Said the same way by run-plan and resume-plan, because it is the same
# permission: one run's own branch, to the remote branch the acceptance gate
# already checks, by ordinary non-force push, and nothing else.
_ALLOW_RUN_HELP = (
    "allow this run to push the verified candidates it produces to their intended remote "
    "branch, so it does not stop to ask for each one. Bound to this run, this worktree, "
    "this branch and that one remote branch; ordinary non-force push only, and no "
    "acceptance check is relaxed by it. Default: no push authorization at all"
)


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
    try:
        # The same resolution a run would perform, with no CLI overrides:
        # an effort a provider cannot honour is reported here rather than
        # discovered when a turn is about to start.
        effective = resolve_agent_configs(config)
    except ProjectConfigError as exc:
        print(f"invalid agent configuration: {exc}", file=sys.stderr)
        return 1
    for resolved in (effective.stage, effective.sparring):
        print(f"{resolved.role} agent: {_describe_agent(resolved)}")
    return _report_workflow_state_ignored(args, sparring_dir)


def _describe_agent(resolved: ResolvedAgentConfig) -> str:
    """One line a person can read, saying where each value came from.

    A value the provider chooses for itself is named as such; the engine
    never prints a model name it did not actually pass.
    """

    parts = [f"{resolved.provider} ({resolved.provider_source})"]
    parts.append(
        f"model {resolved.model!r} ({resolved.model_source})"
        if resolved.model
        else "model: provider default"
    )
    if resolved.effort:
        parts.append(f"effort {resolved.effort!r} ({resolved.effort_source})")
    elif resolved.effort_supported:
        parts.append("effort: provider default")
    else:
        parts.append("effort: not supported by this provider")
    return " | ".join(parts)


def _effective_payload(
    config_path: Path, config: ProjectConfig | None, effective: EffectiveAgents
) -> dict[str, object]:
    """The machine-readable effective configuration, in one shape.

    ``show-config --json`` and ``set-config --json`` report exactly this, so
    a UI that refreshes after a change parses the same object it already
    knows how to read. Configuration only -- no environment, no credentials.
    """

    payload: dict[str, object] = {
        "config_path": str(config_path),
        "config_exists": config_path.is_file(),
        "project": config.project if config is not None else None,
        "error": None,
    }
    payload.update(effective.as_dict())
    return payload


def _cmd_show_config(args: argparse.Namespace) -> int:
    """Report the configuration a provider turn would actually run with.

    This exists so a UI never has to re-derive "claude-cli plus this
    project.toml plus no model means X": the resolution lives in the engine
    (:mod:`agent_sparring.agent_config`) and is reported from there.
    Configuration only -- no environment variables, no credentials.
    """

    sparring_dir = Path(args.sparring_dir)
    # Absolute, because the caller most likely to read this is a UI that
    # wants to open the file and does not share this process's cwd.
    config_path = (sparring_dir / CONFIG_FILENAME).resolve()
    try:
        config = _optional_project_config(sparring_dir)
        effective = resolve_agent_configs(
            config,
            stage=RoleOverrides(
                provider=args.stage_provider, model=args.stage_model, effort=args.stage_effort
            ),
            sparring=RoleOverrides(
                provider=args.sparring_provider,
                model=args.sparring_model,
                effort=args.sparring_effort,
            ),
        )
    except ProjectConfigError as exc:
        if args.json:
            # Still machine-readable: a caller asking for JSON gets JSON
            # even when the answer is "this configuration is invalid".
            json.dump(
                {
                    "config_path": str(config_path),
                    "config_exists": config_path.is_file(),
                    "error": str(exc),
                },
                sys.stdout,
                indent=2,
            )
            print()
        else:
            print(f"invalid project configuration: {exc}", file=sys.stderr)
        return 1

    if args.json:
        json.dump(_effective_payload(config_path, config, effective), sys.stdout, indent=2)
        print()
        return 0

    print(f"project.toml: {config_path}{'' if config_path.is_file() else ' (absent)'}")
    for resolved in (effective.stage, effective.sparring):
        print(f"{resolved.role} agent: {_describe_agent(resolved)}")
    return 0


def _cmd_init_config(args: argparse.Namespace) -> int:
    """Create the engine's minimal project.toml, and never overwrite one.

    The template is engine-owned (:func:`agent_sparring.templates.
    render_project_config`) so nothing else -- including the editor
    extension -- has to keep a second copy of the schema.
    """

    sparring_dir = Path(args.sparring_dir)
    path = sparring_dir / CONFIG_FILENAME
    if path.is_file():
        if not args.exist_ok:
            print(f"{path} already exists", file=sys.stderr)
            return 1
        print(str(path))
        return 0
    project = args.project or Path(sparring_dir).resolve().parent.name
    try:
        sparring_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(render_project_config(project), encoding="utf-8")
    except OSError as exc:
        print(f"could not write {path}: {exc}", file=sys.stderr)
        return 1
    print(str(path))
    print(f"wrote minimal project configuration for {project!r}", file=sys.stderr)
    return 0


def _cmd_set_config(args: argparse.Namespace) -> int:
    """Change one role's provider/model/effort in the project's project.toml.

    The typed counterpart to :func:`_cmd_show_config`: the engine owns
    parsing, validation, mutation and effective resolution, so a UI can
    offer controls without learning to write TOML and without acquiring a
    second opinion about what a value means. The role is a fixed choice and
    the fields are a closed set -- there is no way to spell an arbitrary key,
    an arbitrary value or a command fragment through this command.

    The write itself is :mod:`agent_sparring.config_edit`'s: validated
    before anything reaches the disk, atomic when it does, and a no-op when
    the file already says what was asked for.
    """

    sparring_dir = Path(args.sparring_dir)
    try:
        edit = RoleEdit(
            provider=args.provider,
            model=args.model,
            clear_model=args.model_default,
            effort=args.effort,
            clear_effort=args.effort_default,
        )
        outcome = apply_role_edit(sparring_dir, args.role, edit)
        # Deliberately re-read from disk rather than reporting the in-memory
        # result: what a caller needs is the effective configuration of the
        # file that now exists, resolved the same way a run will resolve it.
        config = _optional_project_config(sparring_dir)
        effective = resolve_agent_configs(config)
    except ProjectConfigError as exc:
        if args.json:
            json.dump(
                {
                    "config_path": str((sparring_dir / CONFIG_FILENAME).resolve()),
                    "config_exists": (sparring_dir / CONFIG_FILENAME).is_file(),
                    "changed": False,
                    "error": str(exc),
                },
                sys.stdout,
                indent=2,
            )
            print()
        else:
            print(f"could not change the {args.role} agent configuration: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = _effective_payload(outcome.path.resolve(), config, effective)
        payload["changed"] = outcome.changed
        payload["created"] = outcome.created
        json.dump(payload, sys.stdout, indent=2)
        print()
        return 0

    if outcome.created:
        print(f"created {outcome.path}", file=sys.stderr)
    elif not outcome.changed:
        print(f"{outcome.path} already said this; nothing written", file=sys.stderr)
    print(f"{outcome.resolved.role} agent: {_describe_agent(outcome.resolved)}")
    return 0


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


def _optional_project_config(sparring_dir: Path) -> ProjectConfig | None:
    """The project's parsed ``project.toml``, or ``None`` if it has none.

    A malformed one still raises :class:`ProjectConfigError`: "absent" and
    "unreadable" are different answers and only the first is benign.
    """

    if (sparring_dir / CONFIG_FILENAME).is_file():
        return load_project_config(sparring_dir)
    return None


def _resolve_role(
    role: str,
    sparring_dir: Path,
    *,
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> ResolvedAgentConfig:
    """One role's effective provider/model/effort for this invocation.

    The single resolution path (see :mod:`agent_sparring.agent_config`):
    every command that starts a provider turn goes through this or
    :func:`_resolve_agents`, so no orchestration path can quietly ignore
    project.toml.
    """

    return resolve_role_config(
        role,
        _optional_project_config(sparring_dir),
        RoleOverrides(provider=provider, model=model, effort=effort),
    )


def _resolve_agents(args: argparse.Namespace, sparring_dir: Path) -> EffectiveAgents:
    """Both roles, from the shared loop flags (see :func:`_add_loop_arguments`)."""

    return resolve_agent_configs(
        _optional_project_config(sparring_dir),
        stage=RoleOverrides(
            provider=args.stage_provider,
            model=args.stage_model,
            effort=args.stage_effort,
        ),
        sparring=RoleOverrides(
            provider=args.sparring_provider,
            model=args.sparring_model,
            effort=args.sparring_effort,
        ),
    )


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

        # Resolved here, at launch, from the project.toml on disk right now:
        # a later edit changes the NEXT turn, never this one (see the
        # live-run semantics note on _build_loop_adapters).
        effective = _resolve_role(
            ROLE_STAGE,
            sparring_dir,
            provider=args.provider,
            model=args.model,
            effort=args.effort,
        )
        adapter = ClaudeCliAdapter(
            repo_root=repo_root,
            executable=args.claude_executable,
            permission_mode=args.permission_mode,
            model=effective.model,
            effort=effective.effort,
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

        effective = _resolve_role(
            ROLE_SPARRING,
            sparring_dir,
            provider=args.provider,
            model=args.model,
            effort=args.effort,
        )
        # No --sandbox override is exposed here: CodexCliAdapter has no
        # sandbox field or extra_args passthrough at all -- read-only is
        # hard-coded (see providers/codex_cli.py), so there is no
        # writable-sandbox escape hatch for this adapter.
        adapter = CodexCliAdapter(
            repo_root=repo_root,
            executable=args.codex_executable,
            model=effective.model,
            effort=effective.effort,
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
    """Refuse any provider/model/effort selection the loop commands cannot
    serve. Separate from :func:`_build_loop_adapters` so run-plan can check
    it once up front, before recording any run state, even though its
    adapters are built later, per planned stage.

    The check is exactly the resolution the adapters will later perform, so
    an unsupported effort or an unimplemented provider is an
    :class:`~agent_sparring.agent_config.AgentConfigError` raised before the
    run starts rather than partway through it.
    """

    _resolve_agents(args, sparring_dir)


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

    Live-run semantics: this is called once per planned stage, immediately
    before that stage's turns, and it re-reads ``project.toml`` every time.
    Editing the file therefore affects the next stage's turns and leaves
    any provider process already running untouched -- the engine never
    reconfigures or restarts a turn in flight. Provider session resume is
    unaffected: the session id is the provider's, recorded in stage state,
    and does not live on an adapter object.
    """

    effective = _resolve_agents(args, sparring_dir)
    stage_adapter = ClaudeCliAdapter(
        repo_root=repo_root,
        executable=args.claude_executable,
        permission_mode=args.permission_mode,
        model=effective.stage.model,
        effort=effective.stage.effort,
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
        model=effective.sparring.model,
        effort=effective.sparring.effort,
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


def _plan_input_args(args: argparse.Namespace, run: str = "") -> str:
    """How this plan run is addressed on the command line, for the copyable
    resume hint: the manifest flag or the plan's own path, and which run of
    it this is.

    The run key is included because a plan document can have been executed
    more than once, and a hint that named only the document would be
    ambiguous exactly when it matters -- after the person has run the same
    plan again. It is omitted only when the caller has no run key to give.
    """

    addressed = f"--manifest {args.manifest}" if args.manifest else str(args.plan_path)
    return f"{addressed} --run-key {run}" if run else addressed


def _report_plan_result(
    result: PlanRunResult, args: argparse.Namespace, sparring_dir: Path
) -> None:
    if result.status is PlanRunStatus.COMPLETE:
        print(f"plan complete: {result.plan}")
        for stage_id, sha in result.accepted:
            print(f"  accepted {stage_id} at {sha}")
        return

    stage = Stage.resolve(sparring_dir, result.stage_id)

    if isinstance(result.awaiting, DeferredVerificationRequired):
        _report_deferred_verification_required(result, args, stage)
        return

    if result.awaiting is not None:
        _report_push_authorization_required(result, args, stage)
        return

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
            f"  sparring resume-plan {_plan_input_args(args, result.run)} "
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
        f"sparring resume-plan {_plan_input_args(args, result.run)} --repo-root {args.repo_root or '.'} "
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


def _report_deferred_verification_required(
    result: PlanRunResult, args: argparse.Namespace, stage: Stage
) -> None:
    """A run stopped for manual verification an earlier reviewer deferred.

    Reported as the outstanding obligation it is, not as a review outcome
    and not as a failure: every stage the plan could run is accepted, the
    reviewers judged each of these safe to carry, and what is left is the
    verification a person owes. One checkpoint, all of it, with the stage
    that raised each check and the reviewer's own reason for deferring it,
    so a person can see why they were not interrupted earlier.
    """

    awaiting = result.awaiting
    assert isinstance(awaiting, DeferredVerificationRequired)  # the caller checked
    resume = (
        f"sparring resume-plan {_plan_input_args(args, result.run)} --repo-root {args.repo_root or '.'} "
        f"--expected-branch {args.expected_branch}"
    )
    print(f"plan paused: {result.plan}")
    print(f"needs: {awaiting.kind}")
    print(f"why now: {awaiting.reason}")
    for stage_id, sha in result.accepted:
        print(f"  accepted {stage_id} at {sha}")
    print()
    print(awaiting.describe + ". Every stage is accepted; none of this is a review failure.")
    answers: list[str] = []
    for obligation in result.deferred:
        print()
        print(f"{obligation.gate.category} — {obligation.gate.title}")
        print(f"  raised by: {obligation.stage_id}")
        print(f"  gate instance: {obligation.instance_id}")
        print(f"  deferred because: {obligation.rationale}")
        for check in obligation.gate.checks:
            recorded = obligation.result_for(check.id)
            mark = f"[{recorded.outcome.word}]" if recorded else "[ ]"
            print(f"  {mark} {check.id}: {check.instruction}")
            print(f"      pass when: {check.pass_criteria}")
            if check.source:
                print(f"      defined in: {check.source}")
            if recorded is not None and recorded.note:
                print(f"      you recorded: {recorded.note}")
            if recorded is None:
                answers.append(f"--deferred-result '{obligation.instance_id}:{check.id}=pass'")
            elif recorded.outcome is not CheckOutcome.PASS:
                # Never a ready-to-paste `=pass` for a check somebody has
                # just reported as failed or untestable: the shortest path
                # out of this pause must not be the one that flips their own
                # result without them noticing.
                answers.append(
                    f"--deferred-result '{obligation.instance_id}:{check.id}=<pass|fail|blocked>'"
                )
    print()
    print("Record your results and let the plan finish:")
    print(f"  {resume} \\\n    " + " \\\n    ".join(answers or ["--deferred-result '<check id>=pass'"]))
    print()
    print(
        "Outcomes are pass, fail or blocked. A note goes after a second '=' . A blocked "
        "check records that it could not be performed, which resolves nothing and leaves "
        "the plan stopped. A failed check keeps the plan stopped too and is written into "
        "the originating stage's notes.md, where that stage's agents read it."
    )
    print(f"stage directory of the current stage: {stage.directory}")


def _report_push_authorization_required(
    result: PlanRunResult, args: argparse.Namespace, stage: Stage
) -> None:
    """A run stopped because a verified candidate needs a person's permission
    to be pushed.

    Reported as the permission question it is, not as a review outcome and
    not as a failure: the reviewer already said READY, nothing is wrong with
    the candidate, and the two commands below are the whole decision.
    """

    awaiting = result.awaiting
    assert awaiting is not None  # the caller checked
    resume = (
        f"sparring resume-plan {_plan_input_args(args, result.run)} --repo-root {args.repo_root or '.'} "
        f"--expected-branch {args.expected_branch}"
    )
    print(f"plan paused: {result.plan}")
    print(f"stage: {result.stage_id} ({stage.directory})")
    print(f"needs: {PUSH_AUTHORIZATION_REQUIRED}")
    for stage_id, sha in result.accepted:
        print(f"  accepted {stage_id} at {sha}")
    print(f"candidate: {awaiting.candidate_sha}")
    print(f"branch: {awaiting.branch}")
    print(f"push to: {awaiting.remote}/{awaiting.remote_branch}")
    if awaiting.detail:
        print(f"why: {awaiting.detail}")
    print()
    print(
        "The review is finished and this exact commit is what would be accepted. It is not "
        "on the remote branch the acceptance gate checks, and nothing has allowed a push."
    )
    print()
    print("Allow this one commit to be pushed, and continue:")
    print(f"  {resume} --allow-push-candidate {awaiting.candidate_sha}")
    print()
    print("Or allow it and stop being asked again for this run:")
    print(f"  {resume} --allow-push-candidate {awaiting.candidate_sha} --allow-push-for-run")
    print()
    print(
        "Either way the push is an ordinary non-force push of "
        f"{awaiting.branch} to {awaiting.remote}/{awaiting.remote_branch}, the engine "
        "re-checks that the commit is really there, and the acceptance gate then runs "
        "unchanged. Push it yourself instead if you prefer; resuming without either "
        "option then accepts it as usual."
    )


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
            run_key=getattr(args, "run_key", None),
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
                deferred_results=tuple(
                    DeferredAnswer.parse(value) for value in (args.deferred_result or [])
                ),
                allow_push_candidate=args.allow_push_candidate,
                allow_push_for_run=args.allow_push_for_run,
                **common,
            )
        else:
            result = start_plan(
                source,
                sparring_dir,
                repo_root,
                make_adapters,
                adopt=args.adopt,
                allow_push_for_run=args.allow_push_for_run,
                **common,
            )
    except (
        PlanError,
        PlanRunError,
        ManifestError,
        StageError,
        ProjectConfigError,
        GitContextError,
        ProviderError,
        DeferredGateError,
    ) as exc:
        print(f"could not {'resume' if resume else 'run'} plan: {exc}", file=sys.stderr)
        return 1

    _report_plan_result(result, args, sparring_dir)
    return 0


def _cmd_run_plan(args: argparse.Namespace) -> int:
    return _run_plan_command(args, resume=False)


def _cmd_resume_plan(args: argparse.Namespace) -> int:
    return _run_plan_command(args, resume=True)


def _cmd_reset_stage(args: argparse.Namespace) -> int:
    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        source = _plan_source(args, repo_root)
        expect_mode = StageMode.from_str(args.mode) if args.mode else None

        def report(message: str) -> None:
            print(message, file=sys.stderr)

        result = reset_stage(
            source,
            sparring_dir,
            repo_root,
            stage_id=args.stage_id,
            expected_branch=args.expected_branch,
            expect_mode=expect_mode,
            report=report,
        )
    except (
        RecoveryError,
        PlanError,
        ManifestError,
        StageError,
        ProjectConfigError,
        GitContextError,
    ) as exc:
        print(f"could not reset stage: {exc}", file=sys.stderr)
        return 1

    print(f"reset stage: {result.stage_id}")
    print(f"was: {result.from_mode.value} ({result.from_mode.describe})")
    print(f"now: {result.to_mode.value} ({result.to_mode.describe})")
    print(f"stage directory: {result.stage_directory}")
    print(f"archived attempt: {result.archive}")
    for role, session in result.discarded_sessions:
        print(f"  no longer authoritative: {role} session {session}")
    print(
        f"verified: {result.reviewed_stage} [{result.reviewed_stage_id}] accepted "
        f"{result.reviewed_sha}, and the repository is at it"
    )
    for touched in result.files:
        print(f"  {touched.disposition}: {touched.path}")
    if not result.files:
        print("  the attempt recorded writing no files")
    if result.previous_digest != result.digest:
        print(f"plan digest re-recorded: {result.previous_digest} -> {result.digest}")
    else:
        print(f"plan digest unchanged: {result.digest}")
    print()
    print("The stage is fresh and the run is paused at it. Continue with:")
    print(
        f"  sparring resume-plan {_plan_input_args(args, result.run)} "
        f"--repo-root {args.repo_root or '.'} --expected-branch {args.expected_branch}"
    )
    return 0


def _cmd_reopen_stage(args: argparse.Namespace) -> int:
    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        source = _plan_source(args, repo_root)

        def report(message: str) -> None:
            print(message, file=sys.stderr)

        result = reopen_for_failed_check(
            source,
            sparring_dir,
            repo_root,
            instance_id=args.gate_instance,
            expected_branch=args.expected_branch,
            report=report,
        )
    except (
        RecoveryError,
        PlanError,
        ManifestError,
        StageError,
        ProjectConfigError,
        GitContextError,
    ) as exc:
        print(f"could not reopen the stage: {exc}", file=sys.stderr)
        return 1

    print(f"reopened stage: {result.stage_id}")
    print(f"run: {result.run}")
    print(f"withdrew asking: {result.instance_id} ({result.title})")
    for check_id in result.failed_checks:
        print(f"  reported failing: {check_id}")
    print(f"candidate kept: {result.candidate_sha}")
    print(f"stage directory: {result.stage_directory}")
    print()
    print(
        "The stage is open again with its candidate, both sessions and its notes "
        "intact, and the failure you reported is in its notes.md as human evidence. "
        "Continue with:"
    )
    print(
        f"  sparring resume-plan {_plan_input_args(args, result.run)} "
        f"--repo-root {args.repo_root or '.'} --expected-branch {args.expected_branch}"
    )
    return 0


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


def _add_single_role_model_arguments(parser: argparse.ArgumentParser) -> None:
    """The model/effort overrides for a command that drives one role
    (run-stage, run-sparring), where the role is already implied."""

    parser.add_argument(
        "--model",
        default=None,
        help=(
            "model override for the provider (default: project.toml's model for "
            "this role, else the provider's own default)"
        ),
    )
    parser.add_argument(
        "--effort",
        default=None,
        help=(
            "reasoning/effort level, validated against the resolved provider "
            "(default: project.toml's effort for this role, else the provider's "
            "own default)"
        ),
    )


def _add_role_model_arguments(parser: argparse.ArgumentParser) -> None:
    """The per-role model/effort overrides, for every command that configures
    both roles at once (the loop commands and show-config).

    Precedence for each of them is the same and lives in one place (see
    :mod:`agent_sparring.agent_config`): this flag, then project.toml's
    ``[agents.<role>]``, then the provider's own default.
    """

    parser.add_argument(
        "--stage-model",
        default=None,
        help=(
            "model override for the stage agent (default: project.toml's "
            "[agents.stage].model, else the provider's own default)"
        ),
    )
    parser.add_argument(
        "--sparring-model",
        default=None,
        help=(
            "model override for the sparring agent (default: project.toml's "
            "[agents.sparring].model, else the provider's own default)"
        ),
    )
    parser.add_argument(
        "--stage-effort",
        default=None,
        help=(
            "reasoning/effort level for the stage agent, validated against the "
            "resolved provider (default: project.toml's [agents.stage].effort, "
            "else the provider's own default)"
        ),
    )
    parser.add_argument(
        "--sparring-effort",
        default=None,
        help=(
            "reasoning/effort level for the sparring agent, validated against the "
            "resolved provider (default: project.toml's [agents.sparring].effort, "
            "else the provider's own default)"
        ),
    )


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
    _add_role_model_arguments(parser)
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

    show_config = subparsers.add_parser(
        "show-config",
        help=(
            "report the provider/model/effort each role would actually run with, "
            "and where each value came from (configuration only; never secrets "
            "or environment contents)"
        ),
    )
    show_config.add_argument(
        "--json",
        action="store_true",
        help="emit the effective configuration as a JSON object on stdout",
    )
    show_config.add_argument(
        "--stage-provider",
        default=None,
        help="stage agent provider override, as run-plan would pass it",
    )
    show_config.add_argument(
        "--sparring-provider",
        default=None,
        help="sparring agent provider override, as run-plan would pass it",
    )
    _add_role_model_arguments(show_config)
    show_config.set_defaults(func=_cmd_show_config)

    init_config = subparsers.add_parser(
        "init-config",
        help=(
            "create a minimal project.toml in --sparring-dir if none exists, and "
            "print its path (never overwrites an existing file)"
        ),
    )
    init_config.add_argument(
        "--project",
        default=None,
        help="project name to record (default: the directory containing .sparring)",
    )
    init_config.add_argument(
        "--exist-ok",
        action="store_true",
        help="succeed and print the path if project.toml already exists",
    )
    init_config.set_defaults(func=_cmd_init_config)

    set_config = subparsers.add_parser(
        "set-config",
        help=(
            "change one role's provider/model/effort in project.toml, validated "
            "the way a run would resolve it (the only engine command that writes "
            "that file)"
        ),
        description=(
            "Set or clear the provider, model and effort of one agent role in "
            "the project's .sparring/project.toml. Only these fields of only "
            "these roles can be written: there is no arbitrary key/value "
            "setting. The edit is validated before anything is written, the "
            "write is atomic, comments and unrelated settings are preserved, "
            "and a request that matches the file already writes nothing. "
            "Clearing an override means the provider's own default applies "
            "again -- the engine then passes no flag and does not guess what "
            "that default is. A change applies to the next provider turn; it "
            "never reconfigures a turn already running."
        ),
    )
    set_config.add_argument(
        "role",
        choices=list(ROLES),
        help="which agent role to configure",
    )
    set_config.add_argument(
        "--provider",
        default=None,
        help=(
            "provider for this role, validated against the providers implemented "
            "for it. A model or effort already configured that the new provider "
            "cannot honour is reported, never silently dropped"
        ),
    )
    model_choice = set_config.add_mutually_exclusive_group()
    model_choice.add_argument(
        "--model", default=None, help="set this role's model (free-form; the provider knows its own names)"
    )
    model_choice.add_argument(
        "--model-default",
        action="store_true",
        help="remove the model override, so the provider's own default applies",
    )
    effort_choice = set_config.add_mutually_exclusive_group()
    effort_choice.add_argument(
        "--effort",
        default=None,
        help="set this role's reasoning/effort level (must be one the resolved provider accepts)",
    )
    effort_choice.add_argument(
        "--effort-default",
        action="store_true",
        help="remove the effort override, so the provider's own default applies",
    )
    set_config.add_argument(
        "--json",
        action="store_true",
        help=(
            "emit the resulting effective configuration as JSON, in the same "
            "shape as show-config --json"
        ),
    )
    set_config.set_defaults(func=_cmd_set_config)

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
    _add_single_role_model_arguments(run_stage)
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
    _add_single_role_model_arguments(run_sparring)
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
        "--run-key",
        default=None,
        metavar="KEY",
        help=(
            "this execution's identity, which the stage ids of its stages are namespaced "
            "by. One is minted per run when this is omitted, which is the ordinary case; "
            "supply it when the caller has to know the run's identity before the run "
            "exists, as a caller that writes the stage ids into a --manifest does. Every "
            "run-plan starts a new run: running the same plan document again is ordinary "
            "work and leaves the earlier run's stages alone"
        ),
    )
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
    run_plan.add_argument("--allow-push-for-run", action="store_true", help=_ALLOW_RUN_HELP)
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
    run_plan.set_defaults(func=_cmd_run_plan, allow_push_candidate=None)

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
    resume_plan_parser.add_argument(
        "--run-key",
        default=None,
        metavar="KEY",
        help=(
            "which run of this plan document to continue, for a document that has been "
            "executed more than once. Omitted, its one open run is resumed; if several are "
            "open the refusal lists them"
        ),
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
        "--deferred-result",
        action="append",
        default=[],
        metavar="CHECK=OUTCOME[=NOTE]",
        help=(
            "answer one deferred manual check at the run's verification checkpoint, e.g. "
            "--deferred-result 'resize-readability=pass=looked fine at 900px'. OUTCOME is "
            "pass, fail or blocked; blocked records that the check could not be performed "
            "and resolves nothing. CHECK is the reviewer's check id, or "
            "'<gate instance>:<check id>' when the same id belongs to more than one "
            "asking. Refused unless the run is in fact stopped on deferred verification "
            "that includes that check. Repeatable"
        ),
    )
    resume_plan_parser.add_argument(
        "--allow-push-candidate",
        default=None,
        metavar="SHA",
        help=(
            "allow this run to push exactly this commit to its intended remote branch, so "
            "the acceptance gate can freeze it. Refused unless the run is in fact stopped "
            "waiting for permission to push that exact commit, which is what stops a stale "
            "surface authorizing a candidate the run has since replaced. Ordinary non-force "
            "push of the run's own branch only; it authorizes nothing else and it does not "
            "relax any acceptance check"
        ),
    )
    resume_plan_parser.add_argument(
        "--allow-push-for-run", action="store_true", help=_ALLOW_RUN_HELP
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

    reset_stage_parser = subparsers.add_parser(
        "reset-stage",
        help=(
            "archive a plan run's current, unaccepted stage and restart it under the "
            "mode the plan input declares: the attempt is preserved as history and "
            "stripped of its authority, earlier stages and their acceptance are left "
            "untouched and verified, the repository is checked to be at the preceding "
            "accepted candidate, and the stage is recreated fresh with no session"
        ),
    )
    reset_stage_parser.add_argument(
        "stage_id",
        help="the stage to reset; must be the run's current stage and must not be accepted",
    )
    reset_stage_parser.add_argument(
        "plan_path",
        nargs="?",
        default=None,
        help="path to the same reviewed plan file the run will continue with "
        "(omit when using --manifest)",
    )
    reset_stage_parser.add_argument(
        "--manifest", default=None, metavar="PATH", help=manifest_help
    )
    reset_stage_parser.add_argument(
        "--mode",
        default=None,
        choices=[member.value for member in StageMode],
        help=(
            "the mode you expect the plan input to declare for this stage; when given it "
            "must match, so a stale plan input is caught instead of quietly obeyed"
        ),
    )
    reset_stage_parser.add_argument("--repo-root", default=None, help=repo_root_help)
    reset_stage_parser.add_argument(
        "--expected-branch",
        required=True,
        help="the branch the plan run was started for; required and verified",
    )
    reset_stage_parser.set_defaults(func=_cmd_reset_stage)

    reopen_parser = subparsers.add_parser(
        "reopen-stage",
        help=(
            "reopen a plan run's current stage so its agents can repair a manual check "
            "its review deferred and you reported failing: the asking is withdrawn, the "
            "stage goes from ACCEPTED back to WORKING keeping its candidate, both "
            "sessions and its notes, and nothing is archived. Only the current stage; "
            "earlier stages' acceptance stays terminal"
        ),
    )
    reopen_parser.add_argument(
        "gate_instance",
        help=(
            "the gate instance of the asking to repair, as the verification checkpoint "
            "reports it; it must be one this run is stopped on and something about it "
            "must have been reported failing"
        ),
    )
    reopen_parser.add_argument(
        "plan_path",
        nargs="?",
        default=None,
        help="path to the same reviewed plan file the run will continue with "
        "(omit when using --manifest)",
    )
    reopen_parser.add_argument(
        "--manifest", default=None, metavar="PATH", help=manifest_help
    )
    reopen_parser.add_argument("--repo-root", default=None, help=repo_root_help)
    reopen_parser.add_argument(
        "--expected-branch",
        required=True,
        help="the branch the plan run was started for; required and verified",
    )
    reopen_parser.set_defaults(func=_cmd_reopen_stage)

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


# What the shell sees when a command is interrupted: the conventional
# 128 + SIGINT. Returned explicitly rather than left to the interpreter's
# own handling of an escaping KeyboardInterrupt, so the code a caller reads
# is the same one on every platform and does not depend on whether Python
# re-raises the signal.
INTERRUPTED_EXIT_CODE = 130


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        # An interruption is an ordinary, supported way to end a command, so
        # it reports as one rather than as a stack trace.
        #
        # Nothing is written, reset or rolled back here, deliberately.
        # Everything the run had got to is already recorded -- a plan run's
        # position, a stage's status, session ids, the accepted candidate --
        # and each of those was written when it became true. Unwinding is
        # therefore the whole of the cleanup: the worktree lock is an
        # ``flock`` the kernel releases with the process, and the provider
        # the turn was waiting on is stopped on the way out
        # (providers/subprocess_runner.py). The interrupted turn itself
        # simply did not happen, and the next `resume-plan` / `run-loop`
        # starts it again from what is recorded.
        print(
            "interrupted; nothing was left half-written and the recorded state "
            "is unchanged. Resume the run to continue from it.",
            file=sys.stderr,
        )
        return INTERRUPTED_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
