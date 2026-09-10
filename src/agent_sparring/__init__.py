"""Sparring V2: a small, generic stage/sparring primitive library.

This package intentionally contains no project-specific (e.g. Sporely)
behavior. Project knowledge lives in project-local ``.sparring/project.toml``
and ``.sparring/PROJECT.md`` files, loaded by :mod:`sparring_v2.config`.
"""

from sparring_v2.config import (
    ProjectConfig,
    ProjectConfigError,
    load_project_config,
    load_project_markdown,
)
from sparring_v2.routing import (
    NeedsYouReason,
    RoutingAction,
    RoutingResult,
    RoutingResultError,
)
from sparring_v2.stage import (
    Stage,
    StageError,
    StageState,
    StageStatus,
)

__all__ = [
    "ProjectConfig",
    "ProjectConfigError",
    "load_project_config",
    "load_project_markdown",
    "NeedsYouReason",
    "RoutingAction",
    "RoutingResult",
    "RoutingResultError",
    "Stage",
    "StageError",
    "StageState",
    "StageStatus",
]
