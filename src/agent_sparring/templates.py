"""Minimal initial content for human-readable stage artifacts.

These are short skeletons, not reusable prompt libraries. Project-specific
content belongs in project-local PROJECT.md / stage prompts, not here.
"""

from __future__ import annotations

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
