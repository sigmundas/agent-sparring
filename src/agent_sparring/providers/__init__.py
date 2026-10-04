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


class ProviderSessionUnresumable(ProviderError):
    """The provider refused to continue a recorded conversation: it no longer
    knows it, or can no longer read it (Codex ``invalid_encrypted_content``,
    thread/session not found; Claude "No conversation found"). Retrying the
    same session cannot succeed; a fresh session for that role can."""


class ProviderUnavailable(ProviderError):
    """The provider declined the turn for capacity reasons -- quota, usage or
    rate limit. Nothing is wrong with the session; a later retry, or a fresh
    session on another provider, can succeed."""


# Matched case-insensitively against the provider's own failure text. Only
# ever used to *classify* a failure the adapter already raises; a match never
# turns a success into a failure, and no match leaves a plain ProviderError.
_UNRESUMABLE_MARKERS = (
    "invalid_encrypted_content",
    "no conversation found",
    "conversation not found",
    "thread not found",
    "session not found",
    "no such thread",
    "no such session",
)
_UNAVAILABLE_MARKERS = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "usage limit",
    "usage_limit",
    "quota",
    "too many requests",
    "overloaded",
)


def classify_failure_text(text: str, *, resuming: bool) -> type[ProviderError] | None:
    """Which recoverable failure ``text`` describes, if any.

    Unresumable is only claimed for a turn that was resuming a session: a
    brand-new conversation has nothing to be unresumable, and recommending a
    fresh session for it would be advice that cannot help.
    """

    lowered = text.lower()
    if resuming and any(marker in lowered for marker in _UNRESUMABLE_MARKERS):
        return ProviderSessionUnresumable
    if any(marker in lowered for marker in _UNAVAILABLE_MARKERS):
        return ProviderUnavailable
    return None


def classify_provider_error(exc: ProviderError, *, resuming: bool) -> ProviderError:
    """``exc`` as its recoverable subclass when its text says so, else
    ``exc`` itself. The classified error keeps the original message."""

    if isinstance(exc, (ProviderSessionUnresumable, ProviderUnavailable)):
        return exc
    kind = classify_failure_text(str(exc), resuming=resuming)
    if kind is None:
        return exc
    classified = kind(str(exc))
    classified.__cause__ = exc
    return classified


def recoverable_provider_failure(
    exc: BaseException,
) -> "ProviderSessionUnresumable | ProviderUnavailable | None":
    """The classified provider failure ``exc`` was ultimately caused by,
    following its explicit ``raise ... from`` chain, or ``None``."""

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, (ProviderSessionUnresumable, ProviderUnavailable)):
            return current
        seen.add(id(current))
        current = current.__cause__
    return None


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

    def converse(self, session_id: str, prompt: str) -> SparringAgentResult:
        """Resume a session for a free-form answer rather than a verdict.

        The same read-only session as :meth:`resume`, but the final message
        is prose meant for a person (see :mod:`agent_sparring.dialogue`), so
        an implementation must not impose whatever structured-output
        constraint it uses for verdicts. That constraint is not advisory: a
        provider given an output schema *cannot* answer outside it, however
        the prompt is worded.
        """


@runtime_checkable
class StructuredAgentAdapter(Protocol):
    """A provider that can run one fresh, read-only, schema-constrained turn.

    Deliberately generic: the caller supplies the schema and owns what the
    answer means. The adapter contributes only the guarantees it gives every
    turn -- a read-only sandbox where the provider enforces one, and a final
    message the provider has validated against ``output_schema``. There is
    no session parameter: such a turn always starts a new conversation and
    can never be appended to an existing reviewer thread. Callers still
    prove read-only with a before/after repository fingerprint rather than
    trusting the provider alone.
    """

    def start_structured(
        self, prompt: str, output_schema: Mapping[str, Any]
    ) -> SparringAgentResult:
        """Start a fresh read-only session and return its structured result."""


__all__ = [
    "LineSink",
    "ProviderError",
    "ProviderSessionUnresumable",
    "ProviderUnavailable",
    "Runner",
    "StructuredAgentAdapter",
    "StageAgentResult",
    "StageAgentAdapter",
    "SparringAgentResult",
    "SparringAgentAdapter",
    "classify_failure_text",
    "classify_provider_error",
    "recoverable_provider_failure",
]
