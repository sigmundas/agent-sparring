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

from agent_sparring.config import ProjectConfigError, load_project_config, load_project_markdown
from agent_sparring.git_context import GitContextError
from agent_sparring.handoff import generate_handoff
from agent_sparring.routing import RoutingAction, RoutingResult, RoutingResultError
from agent_sparring.sparring_exchange import record_sparring
from agent_sparring.stage import Stage, StageError


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
    against the project root (the parent of --sparring-dir), never CWD."""

    if args.repo_root:
        return Path(args.repo_root)

    project_root = sparring_dir.resolve().parent
    try:
        config = load_project_config(sparring_dir)
    except ProjectConfigError:
        return project_root

    configured = Path(config.repo_root)
    return configured if configured.is_absolute() else (project_root / configured).resolve()


def _cmd_handoff(args: argparse.Namespace) -> int:
    sparring_dir = Path(args.sparring_dir)
    repo_root = _resolve_repo_root(args, sparring_dir)
    try:
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
    except (StageError, GitContextError) as exc:
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
