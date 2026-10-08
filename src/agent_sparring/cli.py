"""A small Stage-1 CLI for exercising configuration and stage creation.

This is intentionally minimal: config validation and stage skeleton
creation, so the primitives above have an obvious entry point. Building a
full command surface (invocation, sparring, acceptance, loops) is later
work.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from dataclasses import replace
from pathlib import Path

from agent_sparring.activity import ActivityLog
from agent_sparring.acceptance import (
    AcceptanceError,
    accept_candidate,
    freeze_candidate,
)
from agent_sparring.agent_config import (
    AgentConfigError,
    EffectiveAgents,
    ROLES,
    ROLE_SPARRING,
    ROLE_STAGE,
    ResolvedAgentConfig,
    RoleOverrides,
    SOURCE_CLI,
    SOURCE_ENV,
    resolve_agent_configs,
    resolve_role_config,
    validate_preference,
)
from agent_sparring.config_edit import RoleEdit, apply_role_edit
from agent_sparring.setup_check import fix_setup, setup_problems
from agent_sparring.user_config import (
    load_user_preferences,
    update_user_preferences,
    user_config_path,
)
from agent_sparring.model_choices import model_choices
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
from agent_sparring.intake import (
    DISPOSITIONS as INTAKE_DISPOSITIONS,
    INTAKE_DIRNAME,
    MODES as INTAKE_MODES,
    IntakeError,
    approve_plan,
    intake_not_ignored_message,
    prepare_plan,
)
from agent_sparring.manifest import ManifestError
from agent_sparring import managed_finish, managed_run
from agent_sparring.managed_run import ManagedRunError
from agent_sparring.migration_history import MigrationHistoryError, record_history_snapshot
from agent_sparring.migration_status import REPORT_VERSION as MIGRATION_REPORT_VERSION
from agent_sparring.migration_status import MigrationStatusError, classify
from agent_sparring.migration_status import render_report as render_migration_report
from agent_sparring.plan import (
    PLANS_DIRNAME,
    PlanError,
    PlanRunError,
    PlanRunResult,
    PlanRunStatus,
    PAUSE_SESSION_UNRESUMABLE,
    ProviderPause,
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
from agent_sparring.plan_model import PlanSource
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
from agent_sparring.next_turn import (
    FinalizationDrift,
    NextTurnError,
    UntrackedRefusal,
    check_untracked_before_review,
    standalone_start_with,
)
from agent_sparring.sessions import (
    SessionError,
    current_session_id,
    generations,
    is_fresh,
    record_pin,
    start_fresh_sessions,
)
from agent_sparring.providers import (
    ProviderSessionUnresumable,
    recoverable_provider_failure,
)
from agent_sparring.sparring_agent import SparringAgentRunError, run_sparring_agent
from agent_sparring.sparring_exchange import record_sparring
from agent_sparring.sparring_prompt import build_sparring_prompt
from agent_sparring.dialogue import DialogueError, resolve_check, run_dialogue_turn
from agent_sparring.dialogue_prompt import assemble_dialogue_prompt
from agent_sparring.stage import PinnedAgent, Stage, StageError, StageMode
from agent_sparring.usage import collect_stage_usage, render_report
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
    print(f"user preferences: {user_config_path()}")
    for resolved in (effective.stage, effective.sparring):
        print(f"{resolved.role} agent: {_describe_agent(resolved)}")
    obsolete = _warn_obsolete_settings(config)
    ignored = _report_workflow_state_ignored(args, sparring_dir)
    return 1 if obsolete else ignored


def _warn_obsolete_settings(config: ProjectConfig | None) -> bool:
    """Say, on stderr, that project.toml still sets model/effort the engine no
    longer reads. True when it does. Commands that start a provider turn
    refuse outright instead (:func:`_refuse_obsolete_settings`)."""

    if config is None or not config.obsolete_agent_settings:
        return False
    for setting in config.obsolete_agent_settings:
        print(
            f"obsolete: project.toml [agents.{setting.role}] {setting.field} = {setting.value!r} "
            f"is ignored; model and effort are your own preferences now",
            file=sys.stderr,
        )
    print("remove them with: sparring fix-config", file=sys.stderr)
    return True


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
    config_path: Path,
    config: ProjectConfig | None,
    effective: EffectiveAgents,
    sparring_dir: Path | None = None,
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
    # Where model and effort preferences live: one file for this person,
    # outside every repository (see agent_sparring.user_config). A UI writes
    # it through set-config, never directly.
    user_path = user_config_path()
    payload["user_config_path"] = str(user_path)
    payload["user_config_exists"] = user_path.is_file()
    payload.update(effective.as_dict())
    return payload


def _cmd_show_config(args: argparse.Namespace) -> int:
    """Report the configuration a provider turn would actually run with.

    This exists so a UI never has to re-derive "claude-cli plus this
    project.toml plus no model means X": the resolution lives in the engine
    (:mod:`agent_sparring.agent_config`) and is reported from there. That
    includes the environment layer, so a value set by ``SPARRING_*`` appears
    here with source ``env`` rather than silently disagreeing with the file
    this command prints the path of. Only the resolved values are shown:
    this never dumps the environment and never prints a credential.
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
        payload = _effective_payload(config_path, config, effective, sparring_dir)
        payload.update(_setup_payload(args, sparring_dir))
        json.dump(payload, sys.stdout, indent=2)
        print()
        return 0

    print(f"project.toml: {config_path}{'' if config_path.is_file() else ' (absent)'}")
    for resolved in (effective.stage, effective.sparring):
        print(f"{resolved.role} agent: {_describe_agent(resolved)}")
    return 0


def _cmd_model_choices(args: argparse.Namespace) -> int:
    """The models a UI may offer for each role's provider here, and where the
    list came from (see :mod:`agent_sparring.model_choices`). A suggestion
    list, never a validation boundary: ``custom_allowed`` is always true.

    Separate from ``show-config`` because the Codex catalog is a provider
    subprocess, which a configuration refresh must not pay for every time.
    """

    sparring_dir = Path(args.sparring_dir)
    try:
        effective = resolve_agent_configs(_optional_project_config(sparring_dir))
        roles = [effective.stage, effective.sparring]
        if args.role:
            roles = [resolved for resolved in roles if resolved.role == args.role]
        report = [
            model_choices(
                resolved.role,
                args.provider or resolved.provider,
                codex_executable=args.codex_executable,
            ).as_dict()
            for resolved in roles
        ]
    except ProjectConfigError as exc:
        if args.json:
            json.dump({"roles": [], "error": str(exc)}, sys.stdout, indent=2)
            print()
        else:
            print(f"invalid configuration: {exc}", file=sys.stderr)
        return 1
    if args.json:
        json.dump({"roles": report, "error": None}, sys.stdout, indent=2)
        print()
        return 0
    for entry in report:
        print(f"{entry['role']} agent ({entry['provider']}), from {entry['source']}:")
        for choice in entry["choices"]:
            print(f"  {choice['model']}")
        if entry["error"]:
            print(f"  (no list: {entry['error']})")
        print("  any other exact model id is accepted too")
    return 0


def _setup_payload(args: argparse.Namespace, sparring_dir: Path) -> dict[str, object]:
    """``setup_problems``: the fixable setup problems, for a UI (see
    :mod:`agent_sparring.setup_check`). ``setup_error`` instead when they
    could not be determined -- never an empty list standing in for "unknown".
    """

    try:
        repo_root = _resolve_repo_root(argparse.Namespace(repo_root=getattr(args, "repo_root", None)), sparring_dir)
        return {"setup_problems": [problem.as_dict() for problem in setup_problems(repo_root, sparring_dir)]}
    except (ProjectConfigError, GitContextError) as exc:
        return {"setup_error": str(exc)}


def _cmd_fix_config(args: argparse.Namespace) -> int:
    """Repair the fixable setup problems ``show-config`` reports.

    Two kinds (see :mod:`agent_sparring.setup_check`): obsolete model/effort
    keys are removed from ``project.toml``, and the missing ``.gitignore``
    lines for workflow-state directories are appended. Nothing else changes,
    no preference is chosen for the person, and committing is left to them.
    """

    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        fix = fix_setup(repo_root, sparring_dir)
    except (ProjectConfigError, GitContextError, OSError) as exc:
        if args.json:
            json.dump({"fixed": False, "added": [], "removed": [], "error": str(exc)}, sys.stdout, indent=2)
            print()
        else:
            print(f"could not fix the setup: {exc}", file=sys.stderr)
        return 1
    gitignore = repo_root / ".gitignore"
    removed = [{"role": r.role, "field": r.field, "value": r.value} for r in fix.removed]
    if args.json:
        json.dump(
            {
                "fixed": True,
                "added": fix.added,
                "gitignore": str(gitignore),
                "removed": removed,
                "config_path": str(fix.config_path),
                "error": None,
            },
            sys.stdout,
            indent=2,
        )
        print()
        return 0
    if fix.removed:
        names = ", ".join(f"[agents.{r.role}] {r.field}" for r in fix.removed)
        print(
            f"removed obsolete {names} from {fix.config_path}; commit it to keep it. Your model/effort "
            f"preferences were not changed: choose them with 'sparring set-config'"
        )
    if fix.added:
        print(f"added {', '.join(fix.added)} to {gitignore}; commit .gitignore to keep it")
    if not fix.removed and not fix.added:
        print("nothing to fix: no obsolete settings, and every workflow-state directory is already ignored by git")
    return 0


# check-migrations' distinct non-zero exit for "the report has findings",
# separate from the ordinary error exit code (1) every other command in this
# file uses for a misconfiguration -- a caller (a CI step, say) can then tell
# "this ran fine but something needs attention" apart from "this could not
# even determine the answer".
CHECK_MIGRATIONS_EXIT_FINDINGS = 2


class MigrationsNotConfiguredError(ProjectConfigError):
    """The project has not opted into migration-order detection: there is no
    ``project.toml``, or it has no ``[migrations]`` table."""


def _load_migrations_config(sparring_dir: Path) -> ProjectConfig:
    """Load ``project.toml`` and require it to carry a ``[migrations]``
    table.

    Raised as :class:`ProjectConfigError` (the "not configured" case as its
    :class:`MigrationsNotConfiguredError` subclass), the same type every
    other configuration problem in this file raises, so callers that
    already catch it need no new handler: a project that has not opted into
    migration-order detection is reported plainly, exit 1, never a crash --
    and nothing is written for it.
    """

    config_path = sparring_dir / CONFIG_FILENAME
    if not config_path.is_file():
        raise MigrationsNotConfiguredError(
            f"migration-order detection is not configured: {config_path} does not exist "
            "(see docs/migrations.md)"
        )
    config = load_project_config(sparring_dir)
    if config.migrations is None:
        raise MigrationsNotConfiguredError(
            f"migration-order detection is not configured: {config_path} has no "
            "[migrations] table; add one (see docs/migrations.md) to use migration commands"
        )
    return config


def _cmd_record_migration_history(args: argparse.Namespace) -> int:
    """Parse a captured migration-history listing and record it as a new,
    versioned snapshot (see :mod:`agent_sparring.migration_history`).

    Record, don't interpret: this never compares the result against anything
    else and never fails because of what the history says, only because it
    could not be read at all.
    """

    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        migrations = _load_migrations_config(sparring_dir).migrations
        try:
            raw_text = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            raise MigrationHistoryError(f"could not read --file {args.file}: {exc}") from exc
        snapshot, path = record_history_snapshot(
            repo_root,
            adapter_id=migrations.adapter,
            raw_text=raw_text,
            target_ref=migrations.target_ref,
            observed_at=args.observed_at,
        )
    except (ProjectConfigError, GitContextError, MigrationHistoryError) as exc:
        if args.json:
            json.dump({"recorded": False, "path": None, "error": str(exc)}, sys.stdout, indent=2)
            print()
        else:
            print(f"could not record migration history: {exc}", file=sys.stderr)
        return 1

    if args.json:
        json.dump(
            {"recorded": True, "path": str(path), "error": None, "snapshot": snapshot.as_dict()},
            sys.stdout,
            indent=2,
        )
        print()
        return 0
    print(f"recorded {len(snapshot.applied)} applied version(s) observed at {snapshot.observed_at}")
    print(f"head: {snapshot.head or '(none)'}")
    print(f"written to {path}")
    return 0


def _cmd_check_migrations(args: argparse.Namespace) -> int:
    """Report migration-order drift for the configured target: a read-only
    classification against the last recorded snapshot, never a live probe
    (see :mod:`agent_sparring.migration_status`).

    Exit codes: ``0`` clean, :data:`CHECK_MIGRATIONS_EXIT_FINDINGS` when the
    report has anything to say (stale/tampered/remote-only/modified/unknown/
    stale snapshot), ``1`` when the answer could not be determined at all
    (not configured, bad configuration, an unreadable ``main_ref``, ...).
    With ``--json`` an error is ``{"version", "configured", "error"}``, where
    ``configured`` is false only when there is no ``[migrations]`` table.
    """

    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        migrations = _load_migrations_config(sparring_dir).migrations
        report = classify(repo_root, migrations, branch_ref=args.branch_ref)
    except (ProjectConfigError, GitContextError, MigrationStatusError) as exc:
        if args.json:
            json.dump(
                {
                    "version": MIGRATION_REPORT_VERSION,
                    "configured": not isinstance(exc, MigrationsNotConfiguredError),
                    "error": str(exc),
                },
                sys.stdout,
                indent=2,
            )
            print()
        else:
            print(f"could not check migrations: {exc}", file=sys.stderr)
        return 1

    if args.json:
        json.dump(report.as_dict(), sys.stdout, indent=2)
        print()
    else:
        print(render_migration_report(report))
    return CHECK_MIGRATIONS_EXIT_FINDINGS if report.has_findings() else 0


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
    """Change one role's provider (project) or model/effort (user preference).

    The typed counterpart to :func:`_cmd_show_config`: the engine owns
    parsing, validation, mutation and effective resolution, so a UI can offer
    controls without learning to write TOML and without acquiring a second
    opinion about what a value means. The role is a fixed choice and the
    fields are a closed set.

    Two destinations, because they are two different decisions:

    - ``--provider`` is the project's, and is written to ``project.toml``
      (:mod:`agent_sparring.config_edit`).
    - ``--model``/``--effort`` (and their ``-default`` clears) are the
      person's, shared by every project, and are written to the user
      preference file (:mod:`agent_sparring.user_config`) under this role and
      the provider in effect for it -- or ``--for-provider``. They never touch
      a repository.

    Everything is validated before anything is written; a request that
    matches what is stored already writes nothing.
    """

    sparring_dir = Path(args.sparring_dir)
    config_path = (sparring_dir / CONFIG_FILENAME).resolve()
    pref_edits: list[tuple[str, str | None]] = []
    if args.model is not None or args.model_default:
        pref_edits.append(("model", None if args.model_default else args.model))
    if args.effort is not None or args.effort_default:
        pref_edits.append(("effort", None if args.effort_default else args.effort))
    outcome = None
    preference_changed = False
    try:
        if args.provider is None and not pref_edits:
            raise ProjectConfigError(
                "nothing to change: give --provider (project), or --model/--model-default "
                "or --effort/--effort-default (your preference)"
            )
        if args.provider is not None and args.for_provider is not None:
            raise ProjectConfigError("--for-provider names the preference scope; do not combine it with --provider")
        # Validate the preference edit before anything is written, including a
        # provider change in the same invocation: the preference then belongs to
        # the new provider.
        provider = args.for_provider or args.provider
        if pref_edits:
            provider = provider or _resolve_role(args.role, sparring_dir).provider
            for field, value in pref_edits:
                if value is not None:
                    validate_preference(args.role, provider, field, value.strip())
        if args.provider is not None:
            outcome = apply_role_edit(sparring_dir, args.role, RoleEdit(provider=args.provider))
        if pref_edits:

            def edit(current):
                for field, value in pref_edits:
                    current = current.with_edit(args.role, provider, field, value)
                return current

            _, preference_changed = update_user_preferences(user_config_path(), edit)
        # Re-read what is now stored, resolved the way a run will resolve it.
        config = _optional_project_config(sparring_dir)
        effective = resolve_agent_configs(config)
    except ProjectConfigError as exc:
        if args.json:
            json.dump(
                {
                    "config_path": str(config_path),
                    "config_exists": config_path.is_file(),
                    "user_config_path": str(user_config_path()),
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

    changed = preference_changed or (outcome is not None and outcome.changed)
    if args.json:
        payload = _effective_payload(config_path, config, effective, sparring_dir)
        payload.update(_setup_payload(args, sparring_dir))
        payload["changed"] = changed
        payload["created"] = outcome.created if outcome is not None else False
        json.dump(payload, sys.stdout, indent=2)
        print()
        return 0

    if outcome is not None and outcome.created:
        print(f"created {outcome.path}", file=sys.stderr)
    if not changed:
        print("already set; nothing written", file=sys.stderr)
    resolved = effective.stage if args.role == ROLE_STAGE else effective.sparring
    print(f"{resolved.role} agent: {_describe_agent(resolved)}")
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
        # Written only by prepare-plan/approve-plan, never mid-run, but into
        # the same worktree: visible intake files make the next freeze
        # refuse the tree as dirty just the same.
        ("plan intake", sparring_dir / INTAKE_DIRNAME / "any-intake" / "intake.json"),
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
        if "plan-run state" in problems:
            print(
                plan_state_not_ignored_message(
                    repo_root, sparring_dir / PLANS_DIRNAME / "any-plan.json"
                ),
                file=sys.stderr,
            )
        elif "stage artifacts" in problems:
            print(
                f"stage artifacts under {sparring_dir}/stages/ are not ignored by git; add a "
                f"line '.sparring/stages/' to {repo_root}/.gitignore",
                file=sys.stderr,
            )
        if "plan intake" in problems:
            print(intake_not_ignored_message(repo_root), file=sys.stderr)
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


def _cmd_usage(args: argparse.Namespace) -> int:
    """Report what a stage's provider turns used: agents, timing, tokens.

    Reads the stage's observational ``activity.jsonl`` (see
    :mod:`agent_sparring.usage`) and decides nothing. A stage with no log
    reports that it has none and still exits 0: an absent telemetry file is
    not a run failure.
    """

    sparring_dir = Path(args.sparring_dir)
    try:
        if args.stage_id:
            stages = [Stage.resolve(sparring_dir, stage_id) for stage_id in args.stage_id]
            for stage in stages:
                if not stage.exists():
                    raise StageError(
                        f"stage {stage.stage_id!r} does not exist at {stage.directory}"
                    )
        else:
            stages_root = sparring_dir / "stages"
            stages = [
                Stage.resolve(sparring_dir, path.name)
                for path in sorted(stages_root.iterdir())
                if path.is_dir()
            ] if stages_root.is_dir() else []
            if not stages:
                print(f"no stages under {stages_root}", file=sys.stderr)
                return 1
    except (StageError, OSError) as exc:
        print(f"could not read stage usage: {exc}", file=sys.stderr)
        return 1

    usages = [collect_stage_usage(stage) for stage in stages]
    if args.json:
        json.dump([usage.as_dict() for usage in usages], sys.stdout, indent=2)
        print()
    else:
        print(render_report(usages))
    return 0


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


#: Which activity actor speaks for which agent role.
_ROLE_ACTORS: dict[str, str] = {ROLE_STAGE: "stage", ROLE_SPARRING: "sparrer"}


def _emit_resolved(
    activity_log: ActivityLog | None, resolved: ResolvedAgentConfig
) -> None:
    """Record what this role was configured to run with, before it runs.

    Written once per role per adapter construction -- which is once per
    planned stage in a managed run -- so the stage's ``activity.jsonl``
    answers "what did this actually use, and where was it set" without
    anyone having to reconstruct it from a shell history, an untracked
    environment variable and the current contents of ``project.toml``. That
    reconstruction is not possible after the fact, which is the whole
    reason the event exists: an environment override leaves no other trace.

    Observational only, like everything in ``activity.jsonl``: nothing reads
    it back, and :meth:`ActivityLog.emit` cannot fail a turn.
    """

    if activity_log is None:
        return
    activity_log.emit(
        _ROLE_ACTORS[resolved.role],
        "agents.resolved",
        role=resolved.role,
        provider=resolved.provider,
        provider_source=resolved.provider_source,
        requested_model=resolved.model,
        model_source=resolved.model_source,
        requested_effort=resolved.effort,
        effort_source=resolved.effort_source,
    )


def _resolve_agents(args: argparse.Namespace, sparring_dir: Path) -> EffectiveAgents:
    """Both roles, from the shared loop flags (see :func:`_add_loop_arguments`).

    Every caller starts provider turns, so obsolete project settings refuse
    here (:func:`_refuse_obsolete_settings`).
    """

    _refuse_obsolete_settings(sparring_dir)
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


def _refuse_obsolete_settings(sparring_dir: Path) -> None:
    """Refuse to start a provider turn while project.toml still sets model/effort.

    Those keys no longer take effect (model and effort are the person's own
    preferences). Running anyway would use a different model than the file
    appears to choose, so every command that starts a provider turn stops
    here, before any run state is written or any provider starts, and names
    the engine-owned fix.
    """

    config = _optional_project_config(sparring_dir)
    if config is None or not config.obsolete_agent_settings:
        return
    names = ", ".join(
        f"[agents.{setting.role}] {setting.field} = {setting.value!r}"
        for setting in config.obsolete_agent_settings
    )
    raise AgentConfigError(
        f"project.toml still sets {names}, which no longer takes effect: model and effort are your "
        f"own preferences now, shared by every project. The engine will not start a provider turn "
        f"while the file appears to choose a model it would not use. Run 'sparring fix-config' to "
        f"remove the obsolete keys, then choose your preferences with 'sparring set-config' (the "
        f"removed values are not carried over)"
    )


def _pin_of(resolved: ResolvedAgentConfig) -> PinnedAgent:
    return PinnedAgent(
        provider=resolved.provider,
        model=resolved.model,
        model_source=resolved.model_source,
        effort=resolved.effort,
        effort_source=resolved.effort_source,
    )


def _apply_pin(resolved: ResolvedAgentConfig, pin: PinnedAgent) -> ResolvedAgentConfig:
    """The current session's recorded configuration for one role, or a
    refusal.

    The lock is per session generation (see :mod:`agent_sparring.sessions`):
    a preference changed since the session started is simply not used here;
    it applies from the next stage or from a fresh session. An explicit
    command-line or environment override that asks for something else
    mid-session is refused rather than ignored, because it is a request the
    person made for this invocation.
    """

    if resolved.provider != pin.provider:
        raise AgentConfigError(
            f"this {resolved.role} session started with {pin.provider} and keeps it for the whole "
            f"session; the provider now resolves to {resolved.provider}. A provider change applies "
            f"from the next stage, or from a fresh {resolved.role} session for this one"
        )
    for field, pinned in (("model", pin.model), ("effort", pin.effort)):
        value = getattr(resolved, field)
        source = getattr(resolved, f"{field}_source")
        if source in (SOURCE_CLI, SOURCE_ENV) and value != pinned:
            raise AgentConfigError(
                f"this {resolved.role} session runs with {field} "
                f"{pinned if pinned is not None else 'provider default'!s} from its first turn to its "
                f"last; the {source} override asks for {value!r}. A {field} change applies from the "
                f"next stage, or from a fresh {resolved.role} session: drop the override to continue "
                f"this one"
            )
    return replace(
        resolved,
        model=pin.model,
        model_source=pin.model_source,
        effort=pin.effort,
        effort_source=pin.effort_source,
    )


def _stage_agents(
    stage: Stage, fresh: EffectiveAgents, *, record: "tuple[str, ...]" = (ROLE_STAGE, ROLE_SPARRING)
) -> EffectiveAgents:
    """The configuration ``stage`` runs with: pinned at each session
    generation's first provider turn and reused by every later turn of that
    session (see :class:`agent_sparring.stage.PinnedAgent`).

    ``record`` names the roles about to run a turn; only those are pinned,
    so running one role never locks the other's configuration before that
    role has a conversation."""

    state = stage.read_state()
    pins = dict(state.agents or {})
    missing = [
        resolved
        for resolved in (fresh.stage, fresh.sparring)
        if resolved.role not in pins and resolved.role in record
    ]
    if missing:
        for resolved in missing:
            pins[resolved.role] = _pin_of(resolved)
            # Generation 1, or a pending fresh generation, gets its
            # configuration here, at its first turn.
            record_pin(state, resolved.role, pins[resolved.role])
        state.agents = pins
        stage.write_state(state)
    return EffectiveAgents(
        stage=_apply_pin(fresh.stage, pins[ROLE_STAGE]) if ROLE_STAGE in pins else fresh.stage,
        sparring=_apply_pin(fresh.sparring, pins[ROLE_SPARRING]) if ROLE_SPARRING in pins else fresh.sparring,
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
        _refuse_obsolete_settings(sparring_dir)
        effective = _stage_agents(
            stage,
            resolve_agent_configs(
                _optional_project_config(sparring_dir),
                stage=RoleOverrides(provider=args.provider, model=args.model, effort=args.effort),
            ),
            record=(ROLE_STAGE,),
        ).stage
        _emit_resolved(stage.activity_log(), effective)
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


def _cmd_ask(args: argparse.Namespace) -> int:
    """Ask the stage's reviewer a question and print its answer.

    Read-only and stateless with respect to the run: no verdict, no
    lifecycle transition, no plan advance. See
    :mod:`agent_sparring.dialogue` for what that costs and buys.
    """

    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        stage = Stage.resolve(sparring_dir, args.stage_id)
        if not stage.exists():
            raise StageError(f"stage {args.stage_id!r} does not exist at {stage.directory}")

        if args.dry_run:
            check = resolve_check(stage, args.check_id)
            print(
                assemble_dialogue_prompt(
                    stage,
                    message=args.message,
                    check=check,
                    expected_branch=args.expected_branch,
                ).text
            )
            return 0

        _refuse_obsolete_settings(sparring_dir)
        effective = _stage_agents(
            stage,
            resolve_agent_configs(
                _optional_project_config(sparring_dir),
                sparring=RoleOverrides(provider=args.provider, model=args.model, effort=args.effort),
            ),
            record=(),
        ).sparring
        _emit_resolved(stage.activity_log(), effective)
        adapter = CodexCliAdapter(
            repo_root=repo_root,
            executable=args.codex_executable,
            model=effective.model,
            effort=effective.effort,
            activity=stage.activity_log().bind("sparrer", provider=CODEX_PROVIDER_ID),
        )
        turn = run_dialogue_turn(
            stage,
            repo_root,
            adapter,
            message=args.message,
            check_id=args.check_id,
            gate_instance=args.gate_instance,
            expected_branch=args.expected_branch,
            activity=stage.activity_log().bind("sparrer", provider=CODEX_PROVIDER_ID),
        )
    except (StageError, DialogueError, ProjectConfigError, GitContextError) as exc:
        print(f"could not ask the reviewer: {exc}", file=sys.stderr)
        return 1

    if args.json:
        json.dump(
            {
                "stage_id": stage.stage_id,
                "session_id": turn.session_id,
                "check_id": turn.check_id,
                "gate_instance": turn.gate_instance,
                "question": turn.question,
                "answer": turn.answer,
                "duration_ms": turn.duration_ms,
            },
            sys.stdout,
            indent=2,
        )
        print()
    else:
        print(turn.answer)
        print(
            f"session_id={turn.session_id} duration_ms={turn.duration_ms} "
            f"recorded={stage.dialogue_path()}",
            file=sys.stderr,
        )
    return 0


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

        _refuse_obsolete_settings(sparring_dir)
        # Before anything is recorded (agent pins included).
        check_untracked_before_review(repo_root, stage)
        effective = _stage_agents(
            stage,
            resolve_agent_configs(
                _optional_project_config(sparring_dir),
                sparring=RoleOverrides(provider=args.provider, model=args.model, effort=args.effort),
            ),
            record=(ROLE_SPARRING,),
        ).sparring
        # No --sandbox override is exposed here: CodexCliAdapter has no
        # sandbox field or extra_args passthrough at all -- read-only is
        # hard-coded (see providers/codex_cli.py), so there is no
        # writable-sandbox escape hatch for this adapter.
        _emit_resolved(stage.activity_log(), effective)
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
        NextTurnError,
    ) as exc:
        print(f"could not run sparring agent: {exc}", file=sys.stderr)
        if _refusal_cause(exc) is not None:
            _print_next_commands(
                exc,
                shlex.join(
                    [
                        "sparring", "--sparring-dir", str(Path(sparring_dir).resolve()),
                        "run-sparring", args.stage_id,
                        "--repo-root", str(Path(repo_root).resolve()),
                        "--expected-branch", args.expected_branch,
                        # Refused before anything was pinned: every reviewer
                        # option given still has to be repeated.
                        *_given_options(
                            args, ("provider", "model", "effort"), codex_executable="codex"
                        ),
                    ]
                ),
            )
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
    stage: Stage | None = None,
) -> tuple[ClaudeCliAdapter, CodexCliAdapter]:
    """The stage and sparring adapters for run-loop / run-plan / resume-plan,
    from the shared provider flags (see :func:`_add_loop_arguments`).

    ``activity_log`` is the stage's observational ``activity.jsonl`` (see
    :mod:`agent_sparring.activity`); both adapters append their provider
    stream to it. Observational only, never read back. ``None`` produces
    adapters that emit no provider telemetry.

    Live-run semantics: this is called once per planned stage, immediately
    before that stage's turns, and it re-reads ``project.toml`` and the user
    preferences every time. Changing either therefore affects the next
    stage's turns and leaves any provider process already running untouched -- the engine never
    reconfigures or restarts a turn in flight. Provider session resume is
    unaffected: the session id is the provider's, recorded in stage state,
    and does not live on an adapter object.
    """

    effective = _resolve_agents(args, sparring_dir)
    deferred: tuple[str, ...] = ()
    if stage is not None:
        # One configuration per provider session: pinned before its first
        # turn, reused by every later turn and every later process. A
        # pending fresh generation is the exception: it may not run in this
        # loop at all (an implementation turn may be owed first and fail),
        # so its pin is written at its own first turn (see
        # agent_sparring.sessions.pin_pending_generation), never here.
        state = stage.read_state()
        deferred = tuple(role for role in (ROLE_STAGE, ROLE_SPARRING) if is_fresh(state, role))
        effective = _stage_agents(
            stage,
            effective,
            record=tuple(role for role in (ROLE_STAGE, ROLE_SPARRING) if role not in deferred),
        )
    _emit_resolved(activity_log, effective.stage)
    _emit_resolved(activity_log, effective.sparring)
    if stage is not None:
        _print_resolved_agents(stage, effective, args)
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
    for role, adapter, resolved in (
        (ROLE_STAGE, stage_adapter, effective.stage),
        (ROLE_SPARRING, sparring_adapter, effective.sparring),
    ):
        if role in deferred:
            adapter.pending_pin = _pin_of(resolved)  # type: ignore[attr-defined]
    return stage_adapter, sparring_adapter


_FRESH_FLAGS = {ROLE_STAGE: "--fresh-stage-agent", ROLE_SPARRING: "--fresh-sparrer"}


def _print_resolved_agents(stage: Stage, effective: EffectiveAgents, args: argparse.Namespace) -> None:
    """Say, before any provider starts, exactly what each role will run as.

    Adapter, the configured model and effort with where each came from, which
    conversation the role continues (or that a new / fresh one starts), the
    model the provider itself last reported for that conversation, and the
    backend/account -- which no adapter can read safely today, so it says
    so. The reported model comes from activity.jsonl and is display only.
    """

    state = stage.read_state()
    usage = collect_stage_usage(stage)
    executables = {
        CLAUDE_PROVIDER_ID: getattr(args, "claude_executable", "claude"),
        CODEX_PROVIDER_ID: getattr(args, "codex_executable", "codex"),
    }
    print(f"agents for stage {stage.stage_id}:", file=sys.stderr)
    for label, resolved in (("stage agent", effective.stage), ("sparrer", effective.sparring)):
        role = resolved.role
        history = generations(state, role)
        session = current_session_id(state, role)
        if is_fresh(state, role):
            conversation = (
                f"fresh conversation, generation {history[-1].generation} "
                f"({history[-1].start_reason})"
            )
        elif session is not None:
            generation = history[-1].generation if history else 1
            conversation = f"resuming generation {generation}, session {session}"
        else:
            conversation = "new conversation at its first turn"
        reported = None
        if session is not None:
            role_usage = usage.roles.get(role)
            reported = role_usage.reported_model if role_usage is not None else None
        model = resolved.model if resolved.model is not None else "provider default"
        effort = resolved.effort if resolved.effort is not None else "provider default"
        print(
            f"  {label}: adapter {resolved.provider} ({executables.get(resolved.provider, '?')}); "
            f"configured model {model} [{resolved.model_source}]; effort {effort} "
            f"[{resolved.effort_source}]",
            file=sys.stderr,
        )
        print(
            f"    {conversation}; provider-reported model {reported or 'not reported'}; "
            "backend/account not reported",
            file=sys.stderr,
        )


def _fresh_roles(args: argparse.Namespace) -> tuple[str, ...]:
    """The roles ``--fresh-stage-agent`` / ``--fresh-sparrer`` name, refusing
    a ``--fresh-reason`` that accompanies neither."""

    roles = tuple(
        role
        for role, flag in ((ROLE_STAGE, "fresh_stage_agent"), (ROLE_SPARRING, "fresh_sparrer"))
        if getattr(args, flag, False)
    )
    if getattr(args, "fresh_reason", None) is not None and not roles:
        raise AgentConfigError(
            "--fresh-reason explains a fresh session; give --fresh-sparrer and/or "
            "--fresh-stage-agent with it"
        )
    return roles


def _retry_command(
    args: argparse.Namespace, sparring_dir: Path, repo_root: Path, command: list[str]
) -> str:
    """The shell command that repeats this invocation's run after a provider
    pause: the same state directory and repository (absolute, so it works
    from anywhere), the same branch, and every provider, executable and
    limit option that was given -- shell-quoted. One-time requests (fresh
    sessions, --next-turn, --evidence, push grants) are not repeated: each
    was already applied and recorded by this invocation."""

    managed = _managed_record(args)
    if managed is not None:
        # A managed run is addressed by its key alone: the record supplies
        # the worktree, branch, state directory and plan input.
        parts = ["sparring", *command]
    else:
        parts = [
            "sparring",
            "--sparring-dir",
            str(Path(sparring_dir).resolve()),
            *command,
            "--repo-root",
            str(Path(repo_root).resolve()),
            "--expected-branch",
            args.expected_branch,
        ]
    for option in (
        "stage_provider",
        "sparring_provider",
        "stage_model",
        "sparring_model",
        "stage_effort",
        "sparring_effort",
        *(() if managed is not None else ("stop_after_stage",)),
    ):
        value = getattr(args, option, None)
        if value is not None:
            parts += [f"--{option.replace('_', '-')}", str(value)]
    for option, default in (
        ("claude_executable", "claude"),
        ("codex_executable", "codex"),
        ("permission_mode", DEFAULT_PERMISSION_MODE),
        ("max_send_back_cycles", DEFAULT_MAX_SEND_BACK_CYCLES),
    ):
        value = getattr(args, option, default)
        if value != default:
            parts += [f"--{option.replace('_', '-')}", str(value)]
    return shlex.join(parts)


def _retry_lines(command: str, *, role: str, kind: str, has_session: bool) -> list[str]:
    """The exact command(s) a person can run to continue after a provider
    pause. Never run by the engine."""

    flag = _FRESH_FLAGS[role]
    prefix = "--stage" if role == ROLE_STAGE else "--sparring"
    if kind == PAUSE_SESSION_UNRESUMABLE:
        return [
            "Continue the same stage in a fresh conversation for that role:",
            f"  {command} {flag} --fresh-reason session-unresumable",
        ]
    lines = ["Retry the same conversation once the provider is available again:", f"  {command}"]
    if has_session:
        lines += [
            f"or continue in a fresh conversation (optionally choosing {prefix}-provider / "
            f"{prefix}-model / {prefix}-effort anew):",
            f"  {command} {flag} --fresh-reason provider-unavailable",
        ]
    return lines


def _report_provider_pause(
    *,
    stage_id: str,
    role: str,
    kind: str,
    has_session: bool,
    detail: str,
    command: str,
) -> None:
    print(f"paused: stage {stage_id}: {role} provider turn {kind}", file=sys.stderr)
    print(detail, file=sys.stderr)
    print("The engine did not retry, discard a session or touch the candidate.", file=sys.stderr)
    for line in _retry_lines(command, role=role, kind=kind, has_session=has_session):
        print(line, file=sys.stderr)


def _cmd_run_loop(args: argparse.Namespace) -> int:
    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        stage = Stage.resolve(sparring_dir, args.stage_id)
        if not stage.exists():
            raise StageError(f"stage {args.stage_id!r} does not exist at {stage.directory}")

        self_check = _resolve_self_check(sparring_dir)
        # An ordinary resume obeys the engine's record of whose turn it is
        # (derived once for state written before it existed), decided before
        # anything is pinned or any provider starts.
        fresh_roles = _fresh_roles(args)
        start_with = standalone_start_with(repo_root, stage, choice=args.next_turn)
        if fresh_roles:
            # After every refusal above, before any configuration is
            # resolved for the roles it replaces.
            for role, pending in start_fresh_sessions(
                stage, fresh_roles, args.fresh_reason or "manual", repo_root=repo_root
            ).items():
                print(
                    f"starting a fresh {role} session (generation {pending.generation}, "
                    f"{pending.start_reason}); same stage, candidate and artifacts",
                    file=sys.stderr,
                )
        # Both adapters append to this stage's activity.jsonl (see
        # agent_sparring.activity): observational only, never read back.
        stage_adapter, sparring_adapter = _build_loop_adapters(
            args, sparring_dir, repo_root, activity_log=stage.activity_log(), stage=stage
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
            start_with=start_with,
            sparring_first_reason="next_turn",
        )
    except LoopError as exc:
        failure = recoverable_provider_failure(exc)
        if failure is None:
            print(f"could not run unattended loop: {exc}", file=sys.stderr)
            if _refusal_cause(exc) is not None:
                _print_next_commands(
                    exc,
                    _retry_command(
                        args,
                        sparring_dir,
                        repo_root,
                        ["run-loop", args.stage_id, *_unapplied_next_turn(exc)],
                    ),
                )
            return 1
        role = _loop_failed_role(exc)
        state = stage.read_state()
        _report_provider_pause(
            stage_id=stage.stage_id,
            role=role,
            kind=(
                PAUSE_SESSION_UNRESUMABLE
                if isinstance(failure, ProviderSessionUnresumable)
                else "provider-unavailable"
            ),
            has_session=current_session_id(state, role) is not None,
            detail=str(exc),
            command=_retry_command(
                args, sparring_dir, repo_root, ["run-loop", args.stage_id]
            ),
        )
        return 1
    except (
        StageError,
        SessionError,
        NextTurnError,
        ProjectConfigError,
        GitContextError,
        ProviderError,
    ) as exc:
        print(f"could not run unattended loop: {exc}", file=sys.stderr)
        if _refusal_cause(exc) is not None:
            _print_next_commands(
                exc,
                    _retry_command(
                        args,
                        sparring_dir,
                        repo_root,
                        ["run-loop", args.stage_id, *_unapplied_next_turn(exc)],
                    ),
            )
        return 1

    print(loop_result.routing.summary)
    print(
        f"outcome={loop_result.outcome.value} cycles={len(loop_result.cycles)} "
        f"send_back_count={loop_result.send_back_count}",
        file=sys.stderr,
    )
    return 0


def _loop_failed_role(exc: BaseException) -> str:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, SparringAgentRunError):
            return ROLE_SPARRING
        if isinstance(current, StageAgentRunError):
            return ROLE_STAGE
        current = current.__cause__
    return ROLE_STAGE


def _refusal_cause(exc: BaseException) -> NextTurnError | None:
    """The untracked-file or finalization-drift refusal behind ``exc``, if
    any, following the exception chain the loop and plan layers wrap it in."""

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, (UntrackedRefusal, FinalizationDrift)):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def _print_next_commands(exc: BaseException, rerun: str, reset: str | None = None) -> None:
    """For the refusals whose next step is a command, print it exactly."""

    cause = _refusal_cause(exc)
    if cause is None:
        return
    if isinstance(cause, UntrackedRefusal):
        print(
            "Remove, commit or ignore: " + ", ".join(cause.paths) + "; then rerun:",
            file=sys.stderr,
        )
        print(f"  {rerun}", file=sys.stderr)
        return
    print("After restoring the reviewed candidate, rerun:", file=sys.stderr)
    print(f"  {rerun}", file=sys.stderr)
    if reset is not None:
        print("Or deliberately discard this attempt:", file=sys.stderr)
        print(f"  {reset}", file=sys.stderr)


def _given_options(
    args: argparse.Namespace, options: tuple[str, ...], **defaults: str
) -> list[str]:
    """``--option value`` for each option this invocation was given."""

    parts: list[str] = []
    for option in options:
        value = getattr(args, option, None)
        if value is not None:
            parts += [f"--{option.replace('_', '-')}", str(value)]
    for option, default in defaults.items():
        value = getattr(args, option, default)
        if value != default:
            parts += [f"--{option.replace('_', '-')}", str(value)]
    return parts


def _unapplied_next_turn(exc: BaseException) -> list[str]:
    """``--next-turn <choice>`` when the refusal came before that explicit
    choice was recorded, so the retry must make it again."""

    cause = _refusal_cause(exc)
    choice = getattr(cause, "unapplied_next_turn", None)
    return ["--next-turn", choice] if choice else []


def _plan_command_parts(args: argparse.Namespace) -> list[str]:
    if args.manifest:
        return ["--manifest", str(Path(args.manifest).resolve())]
    return [str(Path(args.plan_path).resolve())]


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


def _managed_record(args: argparse.Namespace):
    return getattr(args, "managed_record", None)


def _note_managed_identity(args: argparse.Namespace, repo_root: Path, run: str) -> None:
    """For a command that is not itself managed-aware (reset-stage,
    reopen-stage): when ``run`` is a recorded managed run on this branch,
    remember its record so the printed resume hint is addressed by key.
    Reporting only; a record that cannot be read leaves the long form."""

    if not run or _managed_record(args) is not None:
        return
    try:
        record = managed_run.read_record(repo_root, run)
    except (ManagedRunError, OSError, ValueError):
        return
    if record is not None and record.branch == args.expected_branch:
        args.managed_record = record


def _resume_command(args: argparse.Namespace, run: str = "") -> str:
    """The copyable ``resume-plan`` command for this run, without the
    situation-specific flags a caller appends.

    A managed run is resumed by its key alone, from any checkout of the
    repository: its record supplies the worktree, branch and plan input.
    """

    record = _managed_record(args)
    if record is not None:
        return f"sparring resume-plan --run-key {record.run_key}"
    return (
        f"sparring resume-plan {_plan_input_args(args, run)} --repo-root {args.repo_root or '.'} "
        f"--expected-branch {args.expected_branch}"
    )


def _resume_retry_parts(args: argparse.Namespace, run: str | None) -> list[str]:
    """``resume-plan`` and its run addressing, for :func:`_retry_command`."""

    record = _managed_record(args)
    if record is not None:
        return ["resume-plan", "--run-key", record.run_key]
    return ["resume-plan", *_plan_command_parts(args), *(["--run-key", run] if run else [])]


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
            f"  {_resume_command(args, result.run)}"
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
    resume = _resume_command(args, result.run)
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
    resume = _resume_command(args, result.run)
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
    resume = _resume_command(args, result.run)
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


def _run_plan_command(args: argparse.Namespace, *, resume: bool, source: PlanSource | None = None) -> int:
    """``source``, when given, is the plan already read and checked (by
    start-plan's confirmation), run as is rather than read again."""

    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        if source is None:
            source = _plan_source(args, repo_root)
        self_check = _resolve_self_check(sparring_dir)
        # Provider selection is checked once, before any run state exists;
        # the adapters themselves are built per planned stage (below) so
        # each stage's provider telemetry lands in that stage's own
        # activity.jsonl -- observational only, never read back.
        _require_loop_providers(args, sparring_dir)

        def make_adapters(stage: Stage) -> tuple[ClaudeCliAdapter, CodexCliAdapter]:
            return _build_loop_adapters(
                args, sparring_dir, repo_root, activity_log=stage.activity_log(), stage=stage
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
            fresh_roles = _fresh_roles(args)
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
                next_turn=args.next_turn,
                fresh_roles=fresh_roles,
                fresh_reason=args.fresh_reason,
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
                managed=bool(getattr(args, "managed_record", None)),
                **common,
            )
    except ProviderPause as exc:
        _report_provider_pause(
            stage_id=exc.stage_id,
            role=exc.role,
            kind=exc.kind,
            has_session=exc.has_session,
            detail=str(exc),
            command=_retry_command(
                args,
                sparring_dir,
                repo_root,
_resume_retry_parts(args, exc.run),
            ),
        )
        return 1
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
        cause = _refusal_cause(exc)
        run_key = getattr(args, "run_key", None)
        if cause is not None:
            _print_next_commands(
                exc,
                _retry_command(
                    args,
                    sparring_dir,
                    repo_root,
[*_resume_retry_parts(args, run_key), *_unapplied_next_turn(exc)],
                ),
                reset=(
                    shlex.join(
                        [
                            "sparring", "--sparring-dir", str(Path(sparring_dir).resolve()),
                            "reset-stage", cause.stage_id, *_plan_command_parts(args),
                            "--repo-root", str(Path(repo_root).resolve()),
                            "--expected-branch", args.expected_branch,
                        ]
                    )
                    if isinstance(cause, FinalizationDrift)
                    else None
                ),
            )
        return 1

    _report_plan_result(result, args, sparring_dir)
    return 0


def _cmd_check_plan(args: argparse.Namespace) -> int:
    """Validate a plan input exactly as ``run-plan`` would read it, and stop.

    Nothing is recorded, approved or run: no run state, no stage directory,
    no provider. Stage ids are left out on purpose -- a run assigns them
    from its own key when it starts.
    """

    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        source = load_plan_source(Path(args.plan_path), repo_root, manifest=args.manifest)
        stages = source.stages()
    except (PlanError, ProjectConfigError, OSError) as exc:
        if args.json:
            json.dump({"valid": False, "error": str(exc)}, sys.stdout, indent=2)
            print()
        else:
            print(f"plan is not runnable: {exc}", file=sys.stderr)
        return 1
    if args.json:
        payload = {
            "valid": True,
            "kind": source.kind,
            "label": source.label,
            "stages": [
                {
                    "position": stage.position,
                    "label": stage.label,
                    "title": stage.title,
                    "mode": stage.mode.value,
                    "repositories": [repository.name for repository in stage.repositories],
                }
                for stage in stages
            ],
            "error": None,
        }
        json.dump(payload, sys.stdout, indent=2)
        print()
        return 0
    print(f"{source.label}: {len(stages)} stage(s), read as {source.kind}")
    for stage in stages:
        suffix = " (review only)" if stage.review_only else ""
        print(f"  {stage.display}{suffix}")
    print("nothing was run or approved")
    return 0


def _cmd_run_plan(args: argparse.Namespace) -> int:
    if args.managed:
        return _run_managed_plan(args)
    if args.target_branch:
        print("could not run plan: --target-branch is for --managed runs", file=sys.stderr)
        return 1
    if not args.expected_branch:
        print("could not run plan: --expected-branch is required (or use --managed)", file=sys.stderr)
        return 1
    return _run_plan_command(args, resume=False)


def _cmd_resume_plan(args: argparse.Namespace) -> int:
    if args.run_key:
        try:
            repo_root = _resolve_repo_root(args, Path(args.sparring_dir))
            record = managed_run.read_record(repo_root, args.run_key)
        except (ManagedRunError, ProjectConfigError) as exc:
            if not (isinstance(exc, ManagedRunError) and exc.code == "not_a_repository"):
                print(f"could not resume plan: {exc}", file=sys.stderr)
                return 1
            record = None
        if record is not None:
            return _resume_managed_plan(args, repo_root, record)
    if not args.expected_branch:
        print(
            "could not resume plan: --expected-branch is required (a managed run is resumed "
            "by --run-key alone)",
            file=sys.stderr,
        )
        return 1
    return _run_plan_command(args, resume=True)


def _managed_input(args: argparse.Namespace) -> tuple[str, Path]:
    if bool(args.plan_path) == bool(args.manifest):
        raise PlanError(
            "give exactly one plan input: the reviewed Markdown plan as a positional "
            "argument, or --manifest with an execution manifest"
        )
    return ("manifest", Path(args.manifest)) if args.manifest else ("markdown", Path(args.plan_path))


def _check_managed_source(source: PlanSource) -> None:
    """Single repository first; intake manifests are follow-up work."""

    if source.kind not in managed_run.INPUT_KINDS:
        raise ManagedRunError(
            "input_kind_unsupported",
            f"a {source.kind} input cannot run --managed yet; use a Markdown plan or a plain manifest",
        )
    if any(stage.repositories for stage in source.stages()):
        raise ManagedRunError(
            "sibling_repositories",
            "this plan input declares sibling repositories; a managed run is single-repository for now",
        )


def _run_managed_plan(
    args: argparse.Namespace, *, base_sha: str | None = None, expected_digest: str | None = None
) -> int:
    """run-plan --managed: preflight, create the record + branch + worktree,
    then run there exactly as run-plan does."""

    from agent_sparring.plan import new_run_key

    sparring_dir = Path(args.sparring_dir)
    try:
        if args.expected_branch:
            raise ManagedRunError(
                "expected_branch_with_managed",
                "--expected-branch cannot be given with --managed: the engine chooses the managed branch",
            )
        repo_root = _resolve_repo_root(args, sparring_dir)
        kind, input_path = _managed_input(args)

        def source_label(read_path: Path, mapped: Path) -> str:
            source = load_plan_source(read_path, repo_root, manifest=kind == "manifest")
            _check_managed_source(source)
            if expected_digest is not None and source.digest() != expected_digest:
                raise ManagedRunError("plan_changed", "the plan changed since it was confirmed; run start-plan again")
            if kind == "manifest":
                return source.label
            # Relative: read inside the worktree, so labelled as there.
            return mapped.as_posix()

        prepared = managed_run.plan_managed_run(
            repo_root,
            sparring_dir,
            source_label=source_label,
            input_kind=kind,
            input_path=input_path,
            target_branch=args.target_branch,
            run_key=args.run_key,
            new_run_key=new_run_key,
            base_sha=base_sha,
        )
        record = managed_run.create_managed_worktree(repo_root, prepared)
    except (ManagedRunError, PlanError, ProjectConfigError, GitContextError) as exc:
        print(f"could not run plan: {exc}", file=sys.stderr)
        return 1
    print(
        f"managed run {record.run_key}: branch {record.branch} from {record.target_branch} "
        f"({record.base_sha[:12]}) in {record.worktree_path}",
        file=sys.stderr,
    )
    if prepared.uncommitted:
        print(
            f"uncommitted changes in your checkout are not part of this run: {', '.join(prepared.uncommitted)}",
            file=sys.stderr,
        )
    return _run_in_record(args, record, resume=False)


def _run_in_record(args: argparse.Namespace, record: managed_run.ManagedRunRecord, *, resume: bool) -> int:
    run_args = argparse.Namespace(**vars(args))
    try:
        # Containment is checked here, before anything runs or is written.
        run_args.sparring_dir = str(record.sparring_dir)
    except ManagedRunError as exc:
        print(f"could not {'resume' if resume else 'run'} plan: {exc}", file=sys.stderr)
        return 1
    run_args.repo_root = record.worktree_path
    run_args.expected_branch = record.branch
    run_args.run_key = record.run_key
    run_args.plan_path = record.input_path if record.input_kind == "markdown" else None
    run_args.manifest = record.input_path if record.input_kind == "manifest" else None
    run_args.managed_record = record
    return _run_plan_command(run_args, resume=resume)


def _resume_managed_plan(args: argparse.Namespace, repo_root: Path, record: managed_run.ManagedRunRecord) -> int:
    """resume-plan --run-key K for a managed run, from any worktree."""

    try:
        if args.expected_branch and args.expected_branch != record.branch:
            raise ManagedRunError(
                "branch_mismatch",
                f"run {record.run_key} runs on {record.branch}, not {args.expected_branch}",
            )
        if args.repo_root and not managed_run.same_repository(Path(args.repo_root), repo_root):
            raise ManagedRunError("repository_mismatch", f"{args.repo_root} is not a worktree of this repository")
        if args.plan_path or args.manifest:
            kind, given = _managed_input(args)
            managed_run.check_input_agrees(
                record, kind=kind, path=given, invoking_top=managed_run.worktree_top(repo_root)
            )
        # A record without ``created`` owns nothing until proven otherwise.
        record = managed_run.confirm_creation(repo_root, record)
        managed_run.verify_worktree(repo_root, record)
    except (ManagedRunError, PlanError) as exc:
        print(f"could not resume plan: {exc}", file=sys.stderr)
        return 1
    try:
        started = (record.sparring_dir / "plans" / f"{record.run_key}.json").is_file()
    except ManagedRunError as exc:
        print(f"could not resume plan: {exc}", file=sys.stderr)
        return 1
    # An interrupted first start: the worktree exists, the run does not yet.
    return _run_in_record(args, record, resume=started)


def _cmd_runs(args: argparse.Namespace) -> int:
    try:
        repo_root = Path(args.repo_root) if args.repo_root else Path(".")
        payload = managed_finish.runs_report(repo_root)
    except ManagedRunError as exc:
        print(f"could not list runs: {exc}", file=sys.stderr)
        return 1
    if args.json:
        json.dump(payload, sys.stdout, indent=2)
        print()
        return 0
    if not payload["runs"]:
        print("no managed runs")
    for run in payload["runs"]:
        where = run["worktree_path"] + ("" if run["worktree_exists"] else " (missing)")
        print(
            f"{run['run_key']}  {run['plan_label']}  {run['lifecycle']}/{run['run_status']}  "
            f"{run['branch']} -> {run['target_branch']}  {where}"
        )
        print(f"  {run['finish']['summary']}")
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    try:
        repo_root = Path(args.repo_root) if args.repo_root else Path(".")
        payload = managed_finish.prune_report(repo_root)
    except ManagedRunError as exc:
        print(f"could not report prunable state: {exc}", file=sys.stderr)
        return 1
    if args.json:
        json.dump(payload, sys.stdout, indent=2)
        print()
        return 0
    if not payload["items"]:
        print("nothing believed unused")
    for item in payload["items"]:
        print(f"{item['kind']} {item['run_key']} ({item['reason']}): {item['path']}")
        print(f"  {item['detail']}")
    return 0


def _cmd_finish_run(args: argparse.Namespace) -> int:
    if not args.dry_run:
        return _execute_finish_run(args)
    try:
        repo_root = Path(args.repo_root) if args.repo_root else Path(".")
        status = managed_finish.finish_status(
            repo_root, args.run_key, allow_merge_commit=args.allow_merge_commit
        )
    except ManagedRunError as exc:
        print(f"could not check run {args.run_key}: {exc}", file=sys.stderr)
        return 1
    finish = status["finish"]
    if args.json:
        json.dump(finish, sys.stdout, indent=2)
        print()
    else:
        print(finish["summary"])
        for check in finish["checks"]:
            print(f"  [{'ok' if check['ok'] else 'NO'}] {check['code']}: {check['detail']}")
        for action in finish["actions"]:
            print(f"  would: {action}")
        for path in finish["deleted_ignored_paths"]:
            print(f"  would delete ignored: {path}")
        for kept in finish["kept"]:
            print(f"  kept ({kept['code']}): {kept['detail']}")
    return 0 if finish["eligible"]["merge"] else 3


def _execute_finish_run(args: argparse.Namespace) -> int:
    try:
        repo_root = Path(args.repo_root) if args.repo_root else Path(".")
        report, refused = managed_finish.finish_run(
            repo_root,
            args.run_key,
            merge_only=args.merge_only,
            allow_merge_commit=args.allow_merge_commit,
            push_target=args.push_target,
        )
    except ManagedRunError as exc:
        print(f"could not finish run {args.run_key}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        for step in report["completed_steps"]:
            print(f"  done: {step}")
        if report["stopped_at"] is not None:
            print(f"stopped at {report['stopped_at']}: {report['reason']}")
            for check in (refused or {}).get("checks", []):
                if not check["ok"]:
                    print(f"  [NO] {check['code']}: {check['detail']}")
            if report["remaining"]:
                print("  remaining: " + ", ".join(report["remaining"]))
        else:
            print(report["reason"] or f"run {args.run_key} is finished")
        for path in report["deleted_ignored_paths"]:
            print(f"  deleted ignored: {path}")  # only paths a completed removal deleted
        for kept in report["kept"]:
            print(f"  kept ({kept['code']}): {kept['detail']}")
    if report["stopped_at"] is None:
        return 0
    return 3 if report["stopped_at"] == "checks" else 1


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
    _note_managed_identity(args, repo_root, result.run)
    print("The stage is fresh and the run is paused at it. Continue with:")
    print(
        f"  {_resume_command(args, result.run)}"
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
    _note_managed_identity(args, repo_root, result.run)
    print(
        "The stage is open again with its candidate, both sessions and its notes "
        "intact, and the failure you reported is in its notes.md as human evidence. "
        "Continue with:"
    )
    print(
        f"  {_resume_command(args, result.run)}"
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
            "model override for the provider (default: your saved preference for "
            "this role and the resolved provider, else the provider's own default)"
        ),
    )
    parser.add_argument(
        "--effort",
        default=None,
        help=(
            "reasoning/effort level, validated against the resolved provider "
            "(default: your saved preference for it, else the provider's own "
            "default)"
        ),
    )


def _pairs(values: list[str] | None, flag: str) -> dict[str, str]:
    """``NAME=VALUE`` flags as a mapping; a repeated or malformed name is refused."""

    pairs: dict[str, str] = {}
    for value in values or []:
        name, sep, rest = value.partition("=")
        name, rest = name.strip(), rest.strip()
        if not sep or not name or not rest:
            raise IntakeError(f"{flag} expects NAME=VALUE, got {value!r}")
        if name in pairs:
            raise IntakeError(f"{flag} names {name!r} twice")
        pairs[name] = rest
    return pairs


def _project_repository_name(args: argparse.Namespace, sparring_dir: Path, repo_root: Path) -> str:
    """This project's repository name: --repository-name, else project.toml's
    ``project``, else the repository directory's name. It is what intake's
    run slices name as their primary repository."""

    if getattr(args, "repository_name", None):
        return str(args.repository_name).strip()
    config = _optional_project_config(sparring_dir)
    if config is not None and config.project.strip():
        return config.project.strip()
    return repo_root.resolve().name


def _intake_adapter(args: argparse.Namespace, sparring_dir: Path, repo_root: Path) -> tuple[CodexCliAdapter, dict]:
    """The read-only intake turn's adapter, and the provider record intake
    stores, from the sparring role's resolution."""

    effective = _resolve_agents(args, sparring_dir).sparring
    if effective.provider != CODEX_PROVIDER_ID:
        # Intake is a read-only turn, and codex-cli is the provider whose
        # read-only sandbox is enforced by the OS rather than by a prompt.
        raise IntakeError(
            f"plan intake runs on {CODEX_PROVIDER_ID} (an OS-enforced read-only sandbox); "
            f"the sparring role resolves to {effective.provider!r}"
        )
    adapter = CodexCliAdapter(
        repo_root=repo_root,
        executable=args.codex_executable,
        model=effective.model,
        effort=effective.effort,
    )
    return adapter, {"provider": effective.provider, "model": effective.model, "effort": effective.effort}


def _project_context(sparring_dir: Path) -> str | None:
    project_md = sparring_dir / "PROJECT.md"
    return project_md.read_text(encoding="utf-8") if project_md.is_file() else None


def _cmd_prepare_plan(args: argparse.Namespace) -> int:
    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        adapter, provider = _intake_adapter(args, sparring_dir, repo_root)
        project_context = _project_context(sparring_dir)
        context_repositories = {
            name: Path(path) for name, path in _pairs(args.context_repository, "--context-repository").items()
        }
        result = prepare_plan(
            Path(args.plan_path),
            repo_root,
            sparring_dir,
            adapter,
            mode=args.mode,
            primary_repository=_project_repository_name(args, sparring_dir, repo_root),
            context_repositories=context_repositories,
            project_context=project_context,
            provider=provider,
        )
    except (IntakeError, ProjectConfigError, AgentConfigError, OSError) as exc:
        print(f"could not prepare plan: {exc}", file=sys.stderr)
        return 1

    interpretation = result.interpretation
    print(f"intake written to {result.directory}", file=sys.stderr)
    print(f"verdict: {interpretation.verdict}")
    for run in interpretation.runs:
        labels = ", ".join(stage.label for stage in run.stages)
        print(f"run slice {run.id} ({run.primary_repository}): {labels}")
    blocking = result.blocking
    print(f"findings: {len(result.findings)} ({len(blocking)} blocking)")
    dispositions = [f.disposition for f in result.findings if f.disposition]
    if dispositions:
        counts = ", ".join(f"{d}={dispositions.count(d)}" for d in INTAKE_DISPOSITIONS if d in dispositions)
        print(f"dispositions: {counts}")
    print(f"review: {result.directory / 'report.md'}")
    # Nothing is executable yet either way; a blocking finding only means
    # approval will refuse, so this is reported as its own exit status.
    return 2 if blocking else 0


def _cmd_slice_branch(args: argparse.Namespace) -> int:
    """Whether an intake slice needs a feature branch; ``--create`` / ``--move`` fix it.

    ``--json`` reports ``{"ok", "status", "created", "moved", "error"}``, where
    ``status`` is :meth:`~agent_sparring.intake_branch.SliceBranch.as_dict`
    as it is *after* any action.
    """

    from agent_sparring.intake_branch import create_slice_branch, move_slice_approval, slice_branch_status

    sparring_dir = Path(args.sparring_dir)
    created: str | None = None
    moved: dict[str, object] | None = None
    error: str | None = None
    status = None
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        where = {"run_id": args.run, "repo_root": repo_root, "sparring_dir": sparring_dir}
        move = None
        if args.create:
            created, move = create_slice_branch(Path(args.intake_dir), branch=args.create, **where)
        elif args.move:
            move = move_slice_approval(Path(args.intake_dir), **where)
        if move is not None:
            moved = {
                "record": str(move.path),
                "from_branch": move.from_branch,
                "to_branch": move.to_branch,
                "created": move.created,
                "run_state": str(move.run_state) if move.run_state else None,
            }
        status = slice_branch_status(Path(args.intake_dir), **where)
    except (IntakeError, ProjectConfigError, GitContextError, OSError) as exc:
        error = str(exc)
    if args.json:
        json.dump(
            {"ok": error is None, "status": status.as_dict() if status else None, "created": created, "moved": moved, "error": error},
            sys.stdout,
            indent=2,
        )
        print()
        return 0 if error is None else 1
    if error is not None:
        print(f"could not {'change' if args.create or args.move else 'check'} the slice's branch: {error}", file=sys.stderr)
        return 1
    if created:
        print(f"checked out new branch {created}", file=sys.stderr)
    if moved:
        print(f"moved the approval from {moved['from_branch']} to {moved['to_branch']}: {moved['record']}", file=sys.stderr)
    assert status is not None
    if status.blocked:
        print(f"{status.problem}\nThe engine cannot fix this: {status.blocked}")
    elif status.action:
        flag = f"--create {status.suggested_branch}" if status.suggested_branch else "--move"
        print(f"{status.problem}\nsparring slice-branch {args.intake_dir} --run {args.run} {flag}")
    else:
        print(f"{status.stages} runs on {status.approved_branch or status.current_branch}; no branch change is needed")
    return 0


_START_OVERRIDES = (
    ("--stage-provider", "stage_provider"),
    ("--sparring-provider", "sparring_provider"),
    ("--stage-model", "stage_model"),
    ("--sparring-model", "sparring_model"),
    ("--stage-effort", "stage_effort"),
    ("--sparring-effort", "sparring_effort"),
    ("--repository-name", "repository_name"),
)


def _start_plan_command(args: argparse.Namespace, repo_root: Path, *, answers: dict, token: str | None) -> list[str]:
    """The copyable start-plan command for a next step: these same inputs,
    plus ``answers`` and, when given, ``--confirm token``."""

    parts = [
        "sparring", "--sparring-dir", str(Path(args.sparring_dir).resolve()), "start-plan",
        str(Path(args.plan_path).resolve()), "--repo-root", str(Path(repo_root).resolve()),
    ]
    if getattr(args, "managed", False):
        parts.append("--managed")
        if args.target_branch:
            parts += ["--target-branch", args.target_branch]
    else:
        parts += ["--expected-branch", args.expected_branch]
    for value in args.context_repository or []:
        parts += ["--context-repository", value]
    for value in args.repository_branch or []:
        parts += ["--repository-branch", value]
    for flag, attr in _START_OVERRIDES:
        if getattr(args, attr, None):
            parts += [flag, str(getattr(args, attr))]
    # Execution inputs the run would use: always carried, so following the
    # printed command runs the same executables, mode and limit.
    parts += [
        "--claude-executable", str(args.claude_executable),
        "--codex-executable", str(args.codex_executable),
        "--permission-mode", str(args.permission_mode),
        "--max-send-back-cycles", str(args.max_send_back_cycles),
    ]
    for decision_id, option_id in answers.items():
        parts += ["--answer", f"{decision_id}={option_id}"]
    if args.allow_push_for_run:
        parts.append("--allow-push-for-run")
    if token:
        parts += ["--confirm", token]
    return parts


def _start_execution(args: argparse.Namespace) -> dict:
    """The start-plan options, besides models, branch, repositories and
    the push choice, that change what runs -- as given."""

    return {
        # Where project configuration, self-check and agent instructions come from.
        "sparring_dir": str(Path(args.sparring_dir).resolve()),
        "permission_mode": str(args.permission_mode),
        "claude_executable": str(args.claude_executable),
        "codex_executable": str(args.codex_executable),
        "max_send_back_cycles": args.max_send_back_cycles,
    }


def _committed_agents(args: argparse.Namespace, repo_root: Path, sparring_dir: Path) -> tuple[EffectiveAgents, str]:
    """The agents and primary repository name a managed run will actually
    use: resolved from the project.toml committed at the target's tip --
    what the managed worktree is checked out at -- never from the invoking
    checkout's copy (--repository-name still overrides the name)."""

    import tempfile

    target, base_sha = managed_run.resolve_target(repo_root, args.target_branch)
    _, config = managed_run.committed_project(repo_root, sparring_dir, target, base_sha)
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / CONFIG_FILENAME).write_bytes(config)
        return _resolve_agents(args, Path(tmp)), _project_repository_name(args, Path(tmp), repo_root)


def _cmd_start_plan(args: argparse.Namespace) -> int:
    """One confirmation from a human plan to a managed run (see
    :mod:`agent_sparring.plan_start`). Without ``--confirm``: report
    ``refused | needs_decision | ready`` and a confirm token, writing
    nothing beyond a prepare's own intake. With it: recompute, refuse any
    difference, seal (intake route) and run exactly as run-plan does."""

    from agent_sparring.intake import seal_approval
    from agent_sparring.plan_start import (
        ROUTE_DIRECT,
        STATUS_NEEDS_DECISION,
        STATUS_READY,
        StartRequest,
        evaluate,
    )

    sparring_dir = Path(args.sparring_dir)
    repo_root = Path(args.repo_root or ".")
    status = None
    if args.managed and args.expected_branch:
        print("start-plan refused: --expected-branch cannot be given with --managed: the engine chooses the managed branch", file=sys.stderr)
        return 1
    if not args.managed and (args.target_branch or not args.expected_branch):
        print(
            "start-plan refused: "
            + ("--target-branch is for --managed runs" if args.target_branch else "--expected-branch is required (or use --managed)"),
            file=sys.stderr,
        )
        return 1
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        context_repositories = {
            name: Path(path) for name, path in _pairs(args.context_repository, "--context-repository").items()
        }
        answers = _pairs(args.answer, "--answer")
        if args.managed:
            effective, primary = _committed_agents(args, repo_root, sparring_dir)
        else:
            effective = _resolve_agents(args, sparring_dir)
            primary = _project_repository_name(args, sparring_dir, repo_root)
        models = {
            resolved.role: {"provider": resolved.provider, "model": resolved.model, "effort": resolved.effort}
            for resolved in (effective.stage, effective.sparring)
        }
        request = StartRequest(
            plan_path=Path(args.plan_path),
            repo_root=repo_root,
            sparring_dir=sparring_dir,
            expected_branch=args.expected_branch,
            primary_repository=primary,
            context_repositories=context_repositories,
            repository_branches=_pairs(args.repository_branch, "--repository-branch"),
            answers=answers,
            models=models,
            allow_push_for_run=args.allow_push_for_run,
            execution=_start_execution(args),
            managed=bool(args.managed),
            target_branch=args.target_branch,
        )

        def prepare(parent: Path | None, given: dict, validate) -> Path:
            adapter, provider = _intake_adapter(args, sparring_dir, repo_root)
            print("preparing the plan: one read-only provider turn…", file=sys.stderr)
            return prepare_plan(
                Path(args.plan_path),
                repo_root,
                sparring_dir,
                adapter,
                mode="compile",
                primary_repository=primary,
                context_repositories=context_repositories,
                project_context=_project_context(sparring_dir),
                provider=provider,
                answer_parent=parent,
                answers=given,
                validate=validate,
            ).directory

        status = evaluate(request, prepare=None if args.confirm else prepare)
    except (IntakeError, ProjectConfigError, AgentConfigError, ManagedRunError, OSError) as exc:
        error = str(exc)
    else:
        error = status.payload["error"]
    if status is not None and args.confirm and status.status == STATUS_READY and status.token != args.confirm:
        error = (
            "the confirm token does not match what start-plan computes now: the plan, its "
            "preparation, the repositories, the models, the execution options (sparring dir, "
            "permission mode, executables, send-back limit) or the push choice changed since it was shown. Nothing was approved or run. Run start-plan again and confirm the new token"
        )
    elif status is not None and args.confirm and status.status == STATUS_NEEDS_DECISION:
        error = "the plan still needs decisions; answer them with --answer and confirm the new token"
    payload = dict(status.payload) if status is not None else None
    if payload is not None and error and args.confirm:
        payload.update(status="refused", confirm_token=None, error=error)

    if error or not args.confirm:
        if args.json:
            if payload is None:
                from agent_sparring.plan_start import refused_payload

                payload = refused_payload(
                    Path(args.plan_path), repo_root, args.expected_branch, error, execution=_start_execution(args)
                )
            json.dump(payload, sys.stdout, indent=2)
            print()
        else:
            _print_start_status(payload, args, repo_root, error)
        if error:
            return 1
        return 2 if payload["status"] == STATUS_NEEDS_DECISION else 0

    # Confirmed: what the person saw is what runs.
    assert status is not None
    run_args = argparse.Namespace(**vars(args))
    run_args.run_key = None
    run_args.adopt = False
    run_args.stop_after_stage = None
    if status.route == ROUTE_DIRECT and args.managed:
        run_args.manifest = None
        return _run_managed_plan(
            run_args,
            base_sha=status.payload["managed"]["base_sha"],
            expected_digest=status.source.digest(),
        )
    if status.route == ROUTE_DIRECT:
        run_args.manifest = None
        # The source the token was computed over, never the file read again.
        return _run_plan_command(run_args, resume=False, source=status.source)
    try:
        approval = seal_approval(status.pending, approved_via="start-plan")
    except (IntakeError, OSError) as exc:
        print(f"could not approve the plan: {exc}", file=sys.stderr)
        return 1
    verb = "approved" if approval.created else "already approved (identical)"
    print(f"{verb}: {approval.manifest_path}", file=sys.stderr)
    run_args.plan_path = None
    run_args.manifest = str(approval.manifest_path)
    run_args.run_key = approval.run_key
    run_args.expected_branch = approval.expected_branch
    return _run_plan_command(run_args, resume=False)


def _print_start_status(payload: dict | None, args: argparse.Namespace, repo_root: Path, error: str | None) -> None:
    if payload is None or payload.get("status") == "refused":
        print(f"start-plan refused: {error}", file=sys.stderr)
        return
    intake = payload.get("intake")
    if intake:
        how = "reused" if intake["reused"] else "prepared"
        print(f"{how} intake {intake['id']}; details: {intake['report']}")
    if payload["status"] == "needs_decision":
        print("needs decision:")
        for decision in payload["decisions"]:
            print(f"- {decision['id']}: {decision['question']} {decision['why']}".rstrip())
            for option in decision["options"]:
                print(f"    {option['id']}: {option['label']} -- {option['consequence']}")
        answers = dict(intake["answers"] if intake else {})
        for decision in payload["decisions"]:
            answers[decision["id"]] = "<option>"
        print("answer them with:")
        print("  " + shlex.join(_start_plan_command(args, repo_root, answers=answers, token=None)))
        return
    run = payload["slice"]
    managed = payload.get("managed")
    if managed:
        print(
            f"ready: {payload['plan']['label']} ({payload['route']} route), managed: a new branch and "
            f"worktree from {managed['target_branch']} at {managed['base_sha'][:12]}"
        )
        if managed["not_part_of_this_run"]:
            print(f"  uncommitted changes here are not part of this run: {', '.join(managed['not_part_of_this_run'])}")
    else:
        print(f"ready: {payload['plan']['label']} ({payload['route']} route) on {payload['expected_branch']}")
    execution = payload.get("execution") or {}
    print(
        f"  sparring dir {execution.get('sparring_dir')}, permission mode {execution.get('permission_mode')}, claude executable "
        f"{execution.get('claude_executable')}, codex executable {execution.get('codex_executable')}, "
        f"max send-back cycles {execution.get('max_send_back_cycles')}"
    )
    if run.get("run_id"):
        print(f"run slice {run['run_id']} (run key {run['run_key']}):")
    for stage in run["stages"]:
        for gate in stage["gates_before"]:
            print(f"  pause for {gate['kind']} gate {gate['id']}: {gate['title']}")
        print(f"  {stage['label']} — {stage['title']}" + ("" if stage["mode"] == "implementation" else f" ({stage['mode']})"))
    for gate in run["completion_gates"]:
        print(f"  pause before completion for {gate['kind']} gate {gate['id']}: {gate['title']}")
    for later in payload["later_slices"]:
        print(f"later slice {later['run_id']} ({later['primary_repository']}): {', '.join(later['stages'])}; start it separately")
    answers = dict(intake["answers"]) if intake else {}
    print("start the run with:")
    print("  " + shlex.join(_start_plan_command(args, repo_root, answers=answers, token=payload["confirm_token"])))


def _cmd_approve_plan(args: argparse.Namespace) -> int:
    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        approval = approve_plan(
            Path(args.intake_dir),
            run_id=args.run,
            repo_root=repo_root,
            sparring_dir=sparring_dir,
            primary_repository=_project_repository_name(args, sparring_dir, repo_root),
            expected_branch=args.expected_branch,
            repositories=_pairs(args.repository, "--repository"),
            repository_branches=_pairs(args.repository_branch, "--repository-branch"),
            confirmed_prerequisites=args.confirm_prerequisite or (),
            without_amendment=args.without_amendment,
        )
    except (IntakeError, ProjectConfigError, OSError) as exc:
        print(f"could not approve plan: {exc}", file=sys.stderr)
        return 1

    verb = "approved" if approval.created else "already approved (identical)"
    print(f"{verb}: {approval.manifest_path}", file=sys.stderr)
    print(f"manifest digest: {approval.manifest_digest}", file=sys.stderr)
    print(f"approval: {approval.approval_path}", file=sys.stderr)
    print(
        f"sparring run-plan --manifest {approval.manifest_path} --run-key {approval.run_key} "
        f"--repo-root {repo_root} --expected-branch {approval.expected_branch}"
    )
    return 0


def _add_role_model_arguments(parser: argparse.ArgumentParser) -> None:
    """The per-role model/effort overrides, for every command that configures
    both roles at once (the loop commands and show-config).

    Precedence for each of them is the same and lives in one place (see
    :mod:`agent_sparring.agent_config`): this flag, then the
    ``SPARRING_<ROLE>_<FIELD>`` environment variable, then your own saved
    preference for the resolved provider (``sparring set-config``), then the
    provider's own default. project.toml no longer carries model or effort.
    """

    parser.add_argument(
        "--stage-model",
        default=None,
        help=(
            "model override for the stage agent (default: your saved preference "
            "for the resolved provider, else the provider's own default)"
        ),
    )
    parser.add_argument(
        "--sparring-model",
        default=None,
        help=(
            "model override for the sparring agent (default: your saved preference "
            "for the resolved provider, else the provider's own default)"
        ),
    )
    parser.add_argument(
        "--stage-effort",
        default=None,
        help=(
            "reasoning/effort level for the stage agent, validated against the "
            "resolved provider (default: your saved preference for it, else the "
            "provider's own default)"
        ),
    )
    parser.add_argument(
        "--sparring-effort",
        default=None,
        help=(
            "reasoning/effort level for the sparring agent, validated against the "
            "resolved provider (default: your saved preference for it, else the "
            "provider's own default)"
        ),
    )


def _add_loop_arguments(
    parser: argparse.ArgumentParser, repo_root_help: str, *, branch_help: str, branch_required: bool = True
) -> None:
    """The flags run-loop, run-plan and resume-plan share: repo root,
    expected branch, provider selection and the SEND_BACK runaway limit.
    ``branch_required=False`` for the plan commands, whose managed form
    has the engine choose the branch (checked in the command instead)."""

    parser.add_argument("--repo-root", default=None, help=repo_root_help)
    parser.add_argument("--expected-branch", required=branch_required, default=None, help=branch_help)
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


def _add_recovery_arguments(parser: argparse.ArgumentParser) -> None:
    """The resume-only recovery flags run-loop and resume-plan share: start a
    fresh provider conversation for a role, and choose the next turn when
    legacy state is ambiguous. Neither ever happens without being asked."""

    parser.add_argument(
        "--fresh-sparrer",
        action="store_true",
        help=(
            "continue this stage with a new reviewer conversation (a new session "
            "generation): same stage, candidate and artifacts; the earlier conversation is "
            "closed, and --sparring-provider/--sparring-model/--sparring-effort may choose "
            "its configuration anew. Without this flag an override that conflicts with the "
            "active session is refused"
        ),
    )
    parser.add_argument(
        "--fresh-stage-agent",
        action="store_true",
        help=(
            "continue this stage with a new implementation-agent conversation; "
            "--stage-provider/--stage-model/--stage-effort may choose its configuration "
            "anew"
        ),
    )
    parser.add_argument(
        "--fresh-reason",
        default=None,
        metavar="REASON",
        help=(
            "why the fresh session was started, recorded with the new generation as "
            "'fresh:<REASON>' (default: manual)"
        ),
    )
    parser.add_argument(
        "--next-turn",
        choices=("stage", "sparring"),
        default=None,
        help=(
            "whose turn it is, only for ambiguous legacy state the engine refuses to read "
            "(it says so); recorded as a manual choice. Refused when a next_turn marker is "
            "recorded, when the engine can derive an answer itself, and while a person's "
            "gate (NEEDS_YOU / ESCALATE) is waiting. 'sparring' reviews the repository as "
            "it is now"
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

    choices_parser = subparsers.add_parser(
        "model-choices",
        help=(
            "list the models to offer for each role's provider (Codex from its own "
            "catalog; Claude from the engine's known exact ids) -- suggestions, not a "
            "closed list"
        ),
    )
    choices_parser.add_argument("--role", choices=list(ROLES), default=None, help="only this role")
    choices_parser.add_argument(
        "--provider", default=None, help="list for this provider instead of the one in effect (needs --role)"
    )
    choices_parser.add_argument("--codex-executable", default="codex", help="codex CLI executable to ask")
    choices_parser.add_argument("--json", action="store_true", help="report as JSON")
    choices_parser.set_defaults(func=_cmd_model_choices)

    fix_config = subparsers.add_parser(
        "fix-config",
        help=(
            "repair the fixable setup problems show-config --json reports: remove "
            "obsolete model/effort keys from project.toml (without choosing any "
            "preference for you), and append the missing workflow-state lines "
            "(.sparring/stages/, plans/, intake/) to the repository's .gitignore; nothing else"
        ),
    )
    fix_config.add_argument("--repo-root", default=None, help="repository root (default: [repo].root, else the parent of --sparring-dir)")
    fix_config.add_argument("--json", action="store_true", help="report what was added as JSON")
    fix_config.set_defaults(func=_cmd_fix_config)

    record_migration_history = subparsers.add_parser(
        "record-migration-history",
        help=(
            "parse a captured 'supabase migration list' (or other configured adapter) "
            "output and record it as a new, versioned migration-history snapshot -- "
            "never runs anything against a database or CLI itself"
        ),
        description=(
            "Record, don't interpret: parses --file with the project's configured "
            "[migrations].adapter and writes what it found (applied versions, observed "
            "time, a hash of the raw input) as a new snapshot under this repository's "
            "git common directory. Refuses clearly on unparseable input or when there "
            "is no [migrations] table or no usable git common directory."
        ),
    )
    record_migration_history.add_argument(
        "--file", required=True, help="path to the captured migration-listing output"
    )
    record_migration_history.add_argument(
        "--observed-at",
        default=None,
        help=(
            "ISO-8601 timestamp, with a timezone, for when --file was captured "
            "(default: now, UTC); refused if it lies more than a few minutes in the future"
        ),
    )
    record_migration_history.add_argument("--repo-root", default=None, help="repository root (default: [repo].root, else the parent of --sparring-dir)")
    record_migration_history.add_argument("--json", action="store_true", help="report what was recorded as JSON")
    record_migration_history.set_defaults(func=_cmd_record_migration_history)

    check_migrations = subparsers.add_parser(
        "check-migrations",
        help=(
            "read-only migration-order report against the last recorded snapshot: "
            "stale/tampered/remote-only migrations and remediation proposals as data"
        ),
        description=(
            "Classifies every migration version this branch and [migrations].main_ref "
            "know about, against the most recently recorded history snapshot and the "
            "deferred-migration registry (if configured). Never runs anything against "
            "a database or migration CLI, and never proposes running one -- proposals "
            "are plain data for a person to act on. Exit codes: 0 clean, "
            f"{CHECK_MIGRATIONS_EXIT_FINDINGS} when the report has findings, 1 when "
            "the answer could not be determined (misconfiguration, a bad main_ref, ...)."
        ),
    )
    check_migrations.add_argument("--repo-root", default=None, help="repository root (default: [repo].root, else the parent of --sparring-dir)")
    check_migrations.add_argument(
        "--branch-ref",
        default="HEAD",
        help="the branch/commit to read local migration files from (default: HEAD)",
    )
    check_migrations.add_argument("--json", action="store_true", help="emit the report as JSON")
    check_migrations.set_defaults(func=_cmd_check_migrations)

    usage_parser = subparsers.add_parser(
        "usage",
        help="report what a stage's provider turns used: agents, timing, tokens",
        description=(
            "Read a stage's observational activity.jsonl and report which "
            "provider, model and effort each agent role ran with and where "
            "that selection came from, how long each provider turn took, and "
            "the token totals the providers themselves reported. Everything "
            "shown is a quotation: a number a provider never reported is "
            "shown as '-' rather than guessed or defaulted to zero. This "
            "command reads telemetry and decides nothing; a missing or "
            "damaged log produces a partial report, not an error."
        ),
    )
    usage_parser.add_argument(
        "stage_id",
        nargs="*",
        help="stage id(s) to report on (default: every stage, oldest first)",
    )
    usage_parser.add_argument(
        "--json",
        action="store_true",
        help="emit the collected records as JSON instead of the text report",
    )
    usage_parser.set_defaults(func=_cmd_usage)

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
            "change one role's provider (in project.toml) or your model/effort "
            "preference (in your user config, shared by every project), validated "
            "the way a run would resolve it"
        ),
        description=(
            "Set a role's provider for this project, or set or clear your own model "
            "and effort preference for that role and provider. The provider is "
            "written to .sparring/project.toml; model and effort are written to "
            "your user configuration (see show-config for its path), keyed by role "
            "and provider, and never to a repository. Only these fields can be "
            "written. Everything is validated before anything is written, writes "
            "are atomic, and a request that matches what is stored writes nothing. "
            "Clearing a preference means the provider's own default applies. A "
            "change applies from the next stage: a stage keeps the configuration it "
            "started with until it ends."
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
        help="this project's provider for the role, validated against the providers implemented for it",
    )
    model_choice = set_config.add_mutually_exclusive_group()
    model_choice.add_argument(
        "--model",
        default=None,
        help=(
            "your preferred exact model for this role and provider (the provider knows "
            "its own names; see model-choices). Aliases such as 'opus' are refused"
        ),
    )
    model_choice.add_argument(
        "--model-default",
        action="store_true",
        help="clear your model preference, so the provider's own default applies",
    )
    effort_choice = set_config.add_mutually_exclusive_group()
    effort_choice.add_argument(
        "--effort",
        default=None,
        help="your preferred reasoning/effort level (must be one the provider accepts)",
    )
    effort_choice.add_argument(
        "--effort-default",
        action="store_true",
        help="clear your effort preference, so the provider's own default applies",
    )
    set_config.add_argument(
        "--for-provider",
        default=None,
        help=(
            "which provider's preference to change (default: the provider in effect for "
            "the role here). Needed outside a project, or to prepare another provider"
        ),
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

    ask = subparsers.add_parser(
        "ask",
        help="ask this stage's reviewer a question, read-only, without changing anything",
        description=(
            "Continue the reviewer's own conversation to ask it about its "
            "review: what evidence it used, why it concluded what it did, "
            "what a gate check actually requires, what happens if you pass "
            "or fail it. The turn resumes the recorded sparring session, so "
            "the answer comes from the reviewer that wrote the verdict "
            "rather than a fresh one reconstructing it from artifacts. "
            "It is read-only and verified to be: the worktree is "
            "fingerprinted before and after. It writes no verdict and "
            "changes no run state -- not state.json, not sparring.md, not "
            "the plan run -- so a recorded verdict still changes only when a "
            "real sparring turn writes a new one. The exchange is appended "
            "to the stage's dialogue.jsonl. Note that it does enter the "
            "reviewer's thread, so a later sparring turn has seen it, and it "
            "spends the reviewer's context window; 'sparring usage' reports "
            "both."
        ),
    )
    ask.add_argument("stage_id")
    ask.add_argument(
        "--message",
        required=True,
        help="the question to put to the reviewer",
    )
    ask.add_argument(
        "--check-id",
        default=None,
        help=(
            "the gate check this question is about; quoted to the reviewer "
            "verbatim from the recorded verdict. An id the gate does not ask "
            "is refused, listing the ones it does"
        ),
    )
    ask.add_argument(
        "--gate-instance",
        default=None,
        help=(
            "the gate instance this question belongs to, recorded in the "
            "transcript so an exchange stays tied to the asking it answers"
        ),
    )
    ask.add_argument(
        "--repo-root",
        default=None,
        help=(
            "repository root; overrides project.toml's [repo].root if set "
            "(default: [repo].root from project.toml, resolved against the "
            "project root, else the parent of --sparring-dir)"
        ),
    )
    ask.add_argument(
        "--expected-branch",
        default=None,
        help="the branch under review, shown to the reviewer for orientation",
    )
    ask.add_argument(
        "--provider",
        default=None,
        help=(
            "sparring agent provider (default: project.toml's "
            "[agents.sparring].provider, else codex-cli)"
        ),
    )
    ask.add_argument(
        "--codex-executable",
        default="codex",
        help="codex CLI executable to invoke (default: codex)",
    )
    _add_single_role_model_arguments(ask)
    ask.add_argument(
        "--dry-run",
        action="store_true",
        help="print the prompt this question would send, without invoking any provider",
    )
    ask.add_argument(
        "--json",
        action="store_true",
        help="emit the exchange as JSON instead of printing the answer",
    )
    ask.set_defaults(func=_cmd_ask)

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
    _add_recovery_arguments(run_loop)
    run_loop.set_defaults(func=_cmd_run_loop)

    manifest_help = (
        "path to an execution manifest (JSON) instead of a Markdown plan: explicit stage "
        "ids, labels such as 'Stage 3C', exact briefs and the execution order, produced by "
        "a caller that already interpreted the plan document"
    )

    prepare = subparsers.add_parser(
        "prepare-plan",
        help=(
            "read a human plan with one fresh read-only agent turn and write a reviewable "
            "intake (run slices, stages, exact briefs, gates, findings, an optional amendment "
            "diff) under .sparring/intake/; executes and approves nothing"
        ),
    )
    prepare.add_argument("plan_path", help="path to the human plan document (never modified)")
    prepare.add_argument(
        "--mode",
        choices=INTAKE_MODES,
        default=INTAKE_MODES[0],
        help=(
            "faithful: interpret the staging as written; refine: the agent may propose "
            "different stage boundaries and a plan amendment for review; compile: the agent "
            "may normalize the execution topology through engine-checked transforms, and "
            "findings carry a disposition (auto_resolved, plan_note, needs_decision, refuse); "
            "never an amendment (default: faithful)"
        ),
    )
    prepare.add_argument(
        "--context-repository",
        action="append",
        metavar="NAME=PATH",
        help=(
            "another repository the plan touches, for the agent to read (never modify) "
            "and name in run slices; repeatable"
        ),
    )
    prepare.add_argument(
        "--repository-name",
        default=None,
        help="this project's repository name (default: project.toml's project, else the directory name)",
    )
    prepare.add_argument("--repo-root", default=None, help=repo_root_help)
    prepare.add_argument(
        "--sparring-provider",
        default=None,
        help="provider override for the intake turn, which resolves through the sparring role",
    )
    prepare.add_argument("--codex-executable", default="codex", help="codex CLI executable to invoke")
    _add_role_model_arguments(prepare)
    prepare.set_defaults(func=_cmd_prepare_plan, stage_provider=None)

    approve = subparsers.add_parser(
        "approve-plan",
        help=(
            "approve one reviewed run slice of a prepare-plan intake and write its sealed "
            "execution manifest and approval, which run-plan verifies before running anything; "
            "refuses on blocking findings, a changed plan, interpretation or report, a "
            "repository that moved since intake, an unconfirmed gate, or an earlier run slice "
            "that is not approved and complete"
        ),
    )
    approve.add_argument("intake_dir", help="the .sparring/intake/<id>/ directory prepare-plan wrote")
    approve.add_argument("--run", required=True, metavar="RUN_ID", help="the run slice to approve")
    approve.add_argument(
        "--expected-branch",
        default=None,
        help=(
            "optional check: the branch this slice runs on, which is the branch the repository "
            "has checked out when it is approved; it must equal that one"
        ),
    )
    approve.add_argument(
        "--repository",
        action="append",
        metavar="NAME=PATH",
        help="path of a sibling repository the slice declares; repeatable",
    )
    approve.add_argument(
        "--repository-branch",
        action="append",
        metavar="NAME=BRANCH",
        help="branch of a sibling repository the slice declares; repeatable",
    )
    approve.add_argument(
        "--confirm-prerequisite",
        action="append",
        metavar="ID",
        help=(
            "confirm, by gate id, that a gate this slice waits for is actually satisfied; "
            "recorded in approval.json; repeatable. Earlier run slices are not confirmed: "
            "approval checks their own approval and completed run"
        ),
    )
    approve.add_argument(
        "--without-amendment",
        action="store_true",
        help="approve the manifest built from the unamended source text although intake proposed an amendment",
    )
    approve.add_argument(
        "--repository-name",
        default=None,
        help="this project's repository name (default: project.toml's project, else the directory name)",
    )
    approve.add_argument("--repo-root", default=None, help=repo_root_help)
    approve.set_defaults(func=_cmd_approve_plan)

    start = subparsers.add_parser(
        "start-plan",
        help=(
            "the normal way to start a human plan: route it directly (a '## Stage <n>' plan) or "
            "through a compile-mode intake (one read-only provider turn, reused when it still "
            "matches), and report refused / needs_decision / ready with a confirm token; "
            "--confirm TOKEN approves exactly that and runs it. Never checks out a branch"
        ),
    )
    start.add_argument("plan_path", help="path to the human plan document (never modified)")
    start.add_argument("--json", action="store_true", help="report the status as JSON (schema in docs/intake.md)")
    start.add_argument(
        "--context-repository",
        action="append",
        metavar="NAME=PATH",
        help="another repository the plan touches, inspected read-only and used as a declared sibling; repeatable",
    )
    start.add_argument(
        "--repository-branch",
        action="append",
        metavar="NAME=BRANCH",
        help="branch of a declared sibling (default: the branch intake saw it on); repeatable",
    )
    start.add_argument(
        "--answer",
        action="append",
        metavar="DECISION=OPTION",
        help="answer a decision the preparation asked; prepares again with the answers, never editing the plan; repeatable",
    )
    start.add_argument(
        "--confirm",
        default=None,
        metavar="TOKEN",
        help="the confirm token start-plan printed: recompute, refuse any difference, approve and run. Never prepares",
    )
    start.add_argument(
        "--repository-name",
        default=None,
        help="this project's repository name (default: project.toml's project, else the directory name)",
    )
    _add_loop_arguments(
        start,
        repo_root_help,
        branch_help="the branch checked out now, which the run uses; required unless --managed. start-plan never switches branches",
        branch_required=False,
    )

    start.add_argument(
        "--managed",
        action="store_true",
        help=(
            "run in a new engine-managed branch + worktree created from --target-branch; the "
            "invoking checkout is never switched or written to. Refused with --expected-branch"
        ),
    )
    start.add_argument(
        "--target-branch",
        default=None,
        metavar="BRANCH",
        help="with --managed: the branch the managed run starts from (default: the branch checked out at --repo-root)",
    )
    start.add_argument("--allow-push-for-run", action="store_true", help=_ALLOW_RUN_HELP)
    start.set_defaults(func=_cmd_start_plan)

    slice_branch = subparsers.add_parser(
        "slice-branch",
        help=(
            "say whether an intake run slice needs a feature branch before it can run (an "
            "implementation slice never runs on a protected branch); --create checks one out at "
            "the current commit, and moves an approval sealed on a protected branch to it"
        ),
    )
    slice_branch.add_argument("intake_dir", help="the .sparring/intake/<id>/ directory prepare-plan wrote")
    slice_branch.add_argument("--run", required=True, metavar="RUN_ID", help="the run slice")
    action = slice_branch.add_mutually_exclusive_group()
    action.add_argument(
        "--create",
        default=None,
        metavar="BRANCH",
        help="check out new branch BRANCH at the current commit (and move the approval to it)",
    )
    action.add_argument(
        "--move",
        action="store_true",
        help=(
            "move an approval sealed on a protected branch to the feature branch checked out "
            "now, at the same commit, before any provider turn; recorded beside the approval"
        ),
    )
    slice_branch.add_argument("--repo-root", default=None, help=repo_root_help)
    slice_branch.add_argument("--json", action="store_true", help="report as JSON")
    slice_branch.set_defaults(func=_cmd_slice_branch)

    check_plan = subparsers.add_parser(
        "check-plan",
        help=(
            "validate a Markdown plan or --manifest exactly as run-plan would read it, "
            "and list its stages; records, approves and runs nothing"
        ),
    )
    check_plan.add_argument("plan_path", help="path to a reviewed Markdown plan, or a manifest with --manifest")
    check_plan.add_argument(
        "--manifest",
        action="store_true",
        help="read plan_path as an execution manifest (JSON), including an approved intake manifest",
    )
    check_plan.add_argument("--repo-root", default=None, help=repo_root_help)
    check_plan.add_argument("--json", action="store_true", help="report as JSON")
    check_plan.set_defaults(func=_cmd_check_plan)

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
        branch_help="the feature branch every stage of this plan must modify; required unless --managed",
        branch_required=False,
    )

    run_plan.add_argument(
        "--managed",
        action="store_true",
        help=(
            "run in a new engine-managed branch + worktree created from --target-branch; the "
            "invoking checkout is never switched or written to. Refused with --expected-branch"
        ),
    )
    run_plan.add_argument(
        "--target-branch",
        default=None,
        metavar="BRANCH",
        help="with --managed: the branch the managed run starts from (default: the branch checked out at --repo-root)",
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
        branch_help=(
            "the feature branch the plan run was started for; required, except for a managed "
            "run named by --run-key, whose record supplies it"
        ),
        branch_required=False,
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
    _add_recovery_arguments(resume_plan_parser)
    resume_plan_parser.set_defaults(func=_cmd_resume_plan, adopt=False, managed=False, target_branch=None)

    runs_parser = subparsers.add_parser(
        "runs", help="list this repository's managed runs (read-only)"
    )
    runs_parser.add_argument("--repo-root", default=None, help=repo_root_help)
    runs_parser.add_argument("--json", action="store_true", help="report as JSON")
    runs_parser.set_defaults(func=_cmd_runs)

    prune_parser = subparsers.add_parser(
        "prune",
        help=(
            "report engine-recorded managed-run state believed unused, with why "
            "(read-only: --dry-run is required; nothing is deleted)"
        ),
    )
    prune_parser.add_argument("--repo-root", default=None, help=repo_root_help)
    prune_parser.add_argument("--dry-run", action="store_true", required=True, help="report only (required)")
    prune_parser.add_argument("--json", action="store_true", help="report as JSON")
    prune_parser.set_defaults(func=_cmd_prune)

    finish_run_parser = subparsers.add_parser(
        "finish-run",
        help=(
            "merge a complete managed run into its target branch and clean it up "
            "(archive its state, remove its worktree and branch), re-checking "
            "eligibility first; --dry-run only reports (exit 0 done/eligible, "
            "3 not eligible, 1 a step failed)"
        ),
    )
    finish_run_parser.add_argument("--run-key", required=True, help="the managed run to finish")
    finish_run_parser.add_argument("--repo-root", default=None, help=repo_root_help)
    finish_run_parser.add_argument("--dry-run", action="store_true", help="report eligibility only")
    finish_run_parser.add_argument(
        "--allow-merge-commit", action="store_true",
        help="accept a merge commit when the target has advanced past the run's base",
    )
    finish_run_parser.add_argument("--merge-only", action="store_true", help="merge (and push) but do not clean up")
    finish_run_parser.add_argument(
        "--push-target", action="store_true", help="push the target branch to the record's remote (never forced)"
    )
    finish_run_parser.add_argument("--json", action="store_true", help="report as JSON")
    finish_run_parser.set_defaults(func=_cmd_finish_run)

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
