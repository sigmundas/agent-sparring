"""agent_sparring: a small, generic stage/sparring primitive library.

This package intentionally contains no project-specific (e.g. Sporely)
behavior. Project knowledge lives in project-local ``.sparring/project.toml``
and ``.sparring/PROJECT.md`` files, loaded by :mod:`agent_sparring.config`.
"""

from agent_sparring.acceptance import (
    AcceptanceError,
    AcceptanceResult,
    FreezeResult,
    StaleCandidateError,
    accept_candidate,
    freeze_candidate,
)
from agent_sparring.config import (
    ProjectConfig,
    ProjectConfigError,
    load_project_config,
    load_project_markdown,
)
from agent_sparring.routing import (
    NEEDS_YOU_REASON_CATEGORIES,
    RoutingAction,
    RoutingResult,
    RoutingResultError,
)
from agent_sparring.stage import (
    Stage,
    StageError,
    StageState,
    StageStatus,
)

__all__ = [
    "AcceptanceError",
    "AcceptanceResult",
    "FreezeResult",
    "StaleCandidateError",
    "accept_candidate",
    "freeze_candidate",
    "ProjectConfig",
    "ProjectConfigError",
    "load_project_config",
    "load_project_markdown",
    "NEEDS_YOU_REASON_CATEGORIES",
    "RoutingAction",
    "RoutingResult",
    "RoutingResultError",
    "Stage",
    "StageError",
    "StageState",
    "StageStatus",
]
