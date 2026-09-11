"""Generic stage-agent provider boundary.

Per the project plan, provider adapters must obtain session/process identity
from the provider or process itself, never trust an agent's prose claim about
what it did. This module defines the minimal shape every provider adapter
returns and implements against; it contains no provider-specific behavior.
Concrete adapters (e.g. Claude Code CLI) live in sibling modules.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from agent_sparring.providers.subprocess_runner import LineSink


class ProviderError(RuntimeError):
    """Raised when a provider invocation fails or returns unusable output."""


# The injectable process runner every CLI adapter uses:
#
#     runner(args, cwd, timeout_seconds, on_line) -> CompletedProcess[str]
#
# ``on_line`` receives each stdout line (without its newline) while the
# child is still running, or is ``None`` when nobody is listening. A runner
# is free to ignore it (the final ``CompletedProcess`` is still what the
# adapter parses); a test fake that never calls it simply produces no live
# provider telemetry. The default implementation is
# :func:`agent_sparring.providers.subprocess_runner.run_streaming`.
Runner = Callable[
    [list[str], Path, "float | None", "LineSink | None"],
    "subprocess.CompletedProcess[str]",
]


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


@dataclass(frozen=True)
class SparringAgentResult:
    """One sparring-provider turn's outcome.

    Shape mirrors :class:`StageAgentResult` deliberately: ``session_id`` is
    the provider's own identifier for this sparring conversation, read from
    its machine-readable output, never invented or taken from agent-authored
    text. ``text`` is the provider's final message, expected (by prompt
    contract, not enforced here) to be a JSON routing verdict that the
    caller parses into a :class:`~agent_sparring.routing.RoutingResult`.
    Kept as a distinct type from ``StageAgentResult`` rather than reused,
    since a given provider may support one role and not the other (see the
    project plan's provider-adapters section: not every provider must
    initially support every operation).
    """

    session_id: str
    text: str
    is_error: bool
    raw: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class SparringAgentAdapter(Protocol):
    """What the orchestrator needs from any sparring-agent provider.

    A sparring adapter must be read-only by contract (per the project plan:
    "Sparring is read-only by default"): implementations should invoke their
    underlying provider in a read-only mode where the provider supports one,
    and callers additionally verify the repository was not modified (see
    :mod:`agent_sparring.sparring_agent`) rather than trusting the provider
    alone.
    """

    def start(self, prompt: str) -> SparringAgentResult:
        """Start a fresh sparring session and return its result."""

    def resume(self, session_id: str, prompt: str) -> SparringAgentResult:
        """Resume an existing sparring session by its provider-issued id."""


__all__ = [
    "LineSink",
    "ProviderError",
    "Runner",
    "StageAgentResult",
    "StageAgentAdapter",
    "SparringAgentResult",
    "SparringAgentAdapter",
]
