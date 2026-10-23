"""What this engine can do, for clients that feature-detect it, and the typed
refusals of the flags that local-first runs retired.

Runs are local-first: implementation, review and acceptance happen entirely
in the local repositories, and no run pushes, pins a remote or waits for
permission to push. Publishing a commit for external review is a separate,
explicit, human-triggered action (Push to review), and it never implies
acceptance.

A flag an older client still sends is never silently reinterpreted: it
refuses with a typed code and guidance, before anything is created or
changed.
"""

from __future__ import annotations

#: The capability a client checks before hiding its push UI.
LOCAL_FIRST = "local_first"

#: Every capability this engine reports, in ``runs --json``, the handoff
#: JSON, ``finish-run --json`` and ``sparring capabilities --json``.
CAPABILITIES: tuple[str, ...] = (LOCAL_FIRST,)

#: ``--allow-push-for-run``: runs never push.
PUSH_NOT_PART_OF_RUNS = "push_not_part_of_runs"
#: ``resume-plan --allow-push-candidate``: there is no push permission to give.
PUSH_PERMISSION_DEPRECATED = "push_permission_deprecated"


def capabilities() -> list[str]:
    return list(CAPABILITIES)


def push_not_part_of_runs() -> str:
    return (
        f"refusing: {PUSH_NOT_PART_OF_RUNS}: --allow-push-for-run is no longer accepted. Runs "
        "are local-first: implementation, review and acceptance happen in your local "
        "repositories, and a run never pushes, so there is no push to allow. Nothing was "
        "created or changed. To publish a commit for external review, use Push to review, a "
        "separate explicit action that never implies acceptance. Run the same command without "
        "--allow-push-for-run."
    )


def push_permission_deprecated(run_key: str | None = None) -> str:
    resume = "sparring resume-plan" + (f" --run-key {run_key}" if run_key else "") + " ..."
    return (
        f"refusing: {PUSH_PERMISSION_DEPRECATED}: --allow-push-candidate is no longer accepted. "
        "Runs are local-first and never push, so there is no push permission to give, and it "
        "is never taken as acceptance. Nothing was changed. A run an older engine paused for "
        "push permission converts only on a plain resume, without this flag, and only under "
        f"the legacy migration rules (L-D1): `{resume}`. To publish a commit for external "
        "review, use Push to review."
    )


__all__ = [
    "CAPABILITIES",
    "LOCAL_FIRST",
    "PUSH_NOT_PART_OF_RUNS",
    "PUSH_PERMISSION_DEPRECATED",
    "capabilities",
    "push_not_part_of_runs",
    "push_permission_deprecated",
]
