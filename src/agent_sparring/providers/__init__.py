"""Generic stage-agent provider boundary.

Per the project plan, provider adapters must obtain session/process identity
from the provider or process itself, never trust an agent's prose claim about
what it did. This module defines the minimal shape every provider adapter
returns and implements against; it contains no provider-specific behavior.
Concrete adapters (e.g. Claude Code CLI) live in sibling modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


class ProviderError(RuntimeError):
    """Raised when a provider invocation fails or returns unusable output."""


@dataclass(frozen=True)
class StageAgentResult:
    """One provider turn's outcome.

    ``session_id`` is the provider's own identifier for this conversation,
    read from its machine-readable output — never invented or taken from
    agent-authored text. ``raw`` is the provider's full parsed output, kept
    for callers (e.g. handoff generation) that want provider-specific detail
    without every adapter needing a bespoke result type.
    """

    session_id: str
    text: str
    is_error: bool
    raw: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class StageAgentAdapter(Protocol):
    """What the orchestrator needs from any stage-agent provider.

    Not every provider need support every operation forever; a provider that
    genuinely cannot resume should say so by raising :class:`ProviderError`
    from ``resume`` rather than the caller pretending compatibility exists.
    """

    def start(self, prompt: str) -> StageAgentResult:
        """Start a fresh session and return its result."""

    def resume(self, session_id: str, prompt: str) -> StageAgentResult:
        """Resume an existing session by its provider-issued id."""


__all__ = [
    "ProviderError",
    "StageAgentResult",
    "StageAgentAdapter",
]
