"""Minimal initial content for human-readable stage artifacts.

These are short skeletons, not reusable prompt libraries. Project-specific
content belongs in project-local PROJECT.md / stage prompts, not here.
"""

from __future__ import annotations

from agent_sparring.agent_config import (
    DEFAULT_PROVIDERS,
    PROVIDER_CAPABILITIES,
    ROLE_SPARRING,
    ROLE_STAGE,
)

# The engine-owned starting point for a project's machine-readable
# configuration. It is deliberately minimal: it names the project, pins the
# two roles to the providers that are actually implemented, and leaves
# ``model``/``effort`` commented out. Writing a value there merely because a
# provider has an internal default would turn an unstated preference into a
# stated one, and would go stale the moment the provider's default moves.
#
# Anything that creates a project.toml -- the ``init-config`` command, and
# through it the editor extension -- renders this, so there is exactly one
# template and no second copy to drift.
PROJECT_CONFIG_TEMPLATE = """\
# Agent Sparring project configuration.
#
# This file is yours. Managed runs read it and never write it; agents taking
# part in a run are instructed not to edit it.

project = "{project}"

[repo]
# Repository root, relative to this file's project (the parent of .sparring).
root = "."

[agents.stage]
provider = "{stage_provider}"
# Optional. Omit either line to use the provider's own default; the engine
# then passes no flag at all and does not guess what the default is.
# model = ""
# effort = ""  # {stage_provider} accepts: {stage_efforts}

[agents.sparring]
provider = "{sparring_provider}"
# model = ""
# effort = ""  # {sparring_provider} accepts: {sparring_efforts}
"""


def render_project_config(project: str) -> str:
    """The minimal valid project.toml for a new project.

    The effort vocabularies are taken from the provider capability table
    rather than retyped, so the comments cannot claim a level the adapter
    would refuse.
    """

    stage = PROVIDER_CAPABILITIES[DEFAULT_PROVIDERS[ROLE_STAGE]]
    sparring = PROVIDER_CAPABILITIES[DEFAULT_PROVIDERS[ROLE_SPARRING]]
    return PROJECT_CONFIG_TEMPLATE.format(
        project=project,
        stage_provider=stage.provider_id,
        stage_efforts=", ".join(stage.effort_levels),
        sparring_provider=sparring.provider_id,
        sparring_efforts=", ".join(sparring.effort_levels),
    )


BRIEF_TEMPLATE = """\
# Stage brief: {stage_id}

## Goal

(Describe the bounded goal for this stage.)

## Out of scope

(What this stage explicitly does not cover.)
"""

NOTES_TEMPLATE = """\
# Notes: {stage_id}

## Implementation notes

(Running notes, decisions, evidence.)

## Deferred checks

(What is deferred, why, and when it becomes required.)
"""

HANDOFF_TEMPLATE = """\
# Handoff: {stage_id}

## Stage goal

(Copy or summarize from brief.md.)

## Claims

(What the implementation agent believes is true.)

## Git / test context

(Branch, base/candidate SHA, changed files, test results.)

## Open / deferred checks

(Anything not yet resolved.)
"""

SPARRING_TEMPLATE = """\
# Sparring: {stage_id}

## Finding / discussion

(Open discussion, questions, challenges.)

## SEND BACK TO STAGE

(Bounded corrections for the same stage agent, if any.)

## NEEDS YOU

(A concrete human choice/check/action, if any. Name the reason category:
product/preference, UI/visual check, device/manual check, external
condition, or scope expansion.)

## ESCALATE

(Why this deserves a different/stronger sparring environment, if any.)

## READY

(Why no further stage-agent pass is needed, if applicable.)

## Deferred

(Checks deferred with what/why/when-required.)
"""
