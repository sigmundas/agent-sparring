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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
