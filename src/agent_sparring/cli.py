"""A small Stage-1 CLI for exercising configuration and stage creation.

This is intentionally minimal: config validation and stage skeleton
creation, so the primitives above have an obvious entry point. Building a
full command surface (invocation, sparring, acceptance, loops) is later
work.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent_sparring.config import (
    CONFIG_FILENAME,
    ProjectConfigError,
    load_project_config,
    load_project_markdown,
)
from agent_sparring.git_context import GitContextError
from agent_sparring.handoff import generate_handoff
from agent_sparring.providers.claude_cli import (
    DEFAULT_PERMISSION_MODE,
    ClaudeCliAdapter,
)
from agent_sparring.routing import RoutingAction, RoutingResult, RoutingResultError
from agent_sparring.sparring_exchange import record_sparring
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
    return 0


def _cmd_new_stage(args: argparse.Namespace) -> int:
    sparring_dir = Path(args.sparring_dir)
    try:
        stage = Stage.resolve(sparring_dir, args.stage_id)
        stage.create(exist_ok=args.exist_ok)
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
        result = RoutingResult(
            action=RoutingAction.from_str(args.action),
            summary=args.summary,
            needs_you_reason=args.needs_you_reason,
            details={"deferred": args.deferred} if args.deferred else {},
        )
        content = record_sparring(stage, result, findings=args.findings or "")
    except (StageError, RoutingResultError) as exc:
        print(f"could not record sparring outcome: {exc}", file=sys.stderr)
        return 1
    print(content)
    print(f"wrote sparring outcome to {stage.directory / 'sparring.md'}", file=sys.stderr)
    return 0


def _resolve_stage_provider(args: argparse.Namespace, sparring_dir: Path) -> str:
    """Precedence: explicit --provider > configured [agents.stage].provider >
    ``claude-cli`` (the initial default, per the project plan)."""

    if args.provider:
        return args.provider
    if (sparring_dir / CONFIG_FILENAME).is_file():
        config = load_project_config(sparring_dir)
        if config.stage_agent_provider:
            return config.stage_agent_provider
    return "claude-cli"


def _cmd_run_stage(args: argparse.Namespace) -> int:
    sparring_dir = Path(args.sparring_dir)
    try:
        repo_root = _resolve_repo_root(args, sparring_dir)
        stage = Stage.resolve(sparring_dir, args.stage_id)
        if not stage.exists():
            raise StageError(f"stage {args.stage_id!r} does not exist at {stage.directory}")

        if args.dry_run:
            state = stage.read_state()
            prompt = build_stage_prompt(
                stage,
                sparring_dir,
                resume=state.implementation_session_id is not None,
                expected_branch=args.expected_branch,
            )
            print(prompt)
            return 0

        provider = _resolve_stage_provider(args, sparring_dir)
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
        )
        run_result = run_stage_agent(
            stage, sparring_dir, repo_root, adapter, expected_branch=args.expected_branch
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sparring")
    parser.add_argument(
        "--sparring-dir",
        default=".sparring",
        help="path to the project's .sparring directory (default: .sparring)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_config = subparsers.add_parser(
        "check-config", help="load and print project.toml / PROJECT.md status"
    )
    check_config.set_defaults(func=_cmd_check_config)

    new_stage = subparsers.add_parser("new-stage", help="create a new stage skeleton")
    new_stage.add_argument("stage_id")
    new_stage.add_argument(
        "--exist-ok",
        action="store_true",
        help="do not fail if the stage directory already exists",
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
        help="print the bounded stage prompt without invoking any provider",
    )
    run_stage.set_defaults(func=_cmd_run_stage)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
