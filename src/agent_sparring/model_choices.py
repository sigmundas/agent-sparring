"""Which models a UI can offer for a role's provider, and where that list came from.

This is a *suggestion list*, never a validation boundary: every provider here
accepts exact model ids the engine has not heard of, so ``custom_allowed`` is
always true and resolution never checks a model against these choices.

- ``codex-cli``: Codex's own catalog (``codex debug models``), source
  ``provider-catalog``. If the catalog cannot be read the choices are empty
  and ``error`` says why -- an empty list is never passed off as "Codex has no
  models".
- ``claude-cli``: the CLI cannot enumerate its models, so the engine's few
  known exact ids (:data:`agent_sparring.providers.claude_cli.KNOWN_MODELS`),
  source ``engine-known`` and marked ``complete: false``.

Choices belong to a (role, provider) pair and are reported with it, so a UI
cannot offer one provider's models for another.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_sparring.agent_config import capability
from agent_sparring.providers import ProviderError, claude_cli, codex_cli

SOURCE_PROVIDER_CATALOG = "provider-catalog"
SOURCE_ENGINE_KNOWN = "engine-known"
SOURCE_NONE = "none"


@dataclass(frozen=True)
class ModelChoice:
    model: str
    display_name: str | None = None
    effort_levels: tuple[str, ...] = ()
    default_effort: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "display_name": self.display_name,
            # The provider's own per-model subset, where it states one. Shown,
            # not enforced: the engine validates the provider's vocabulary.
            "effort_levels": list(self.effort_levels),
            "default_effort": self.default_effort,
        }


@dataclass(frozen=True)
class ModelChoices:
    role: str
    provider: str
    source: str
    complete: bool
    choices: tuple[ModelChoice, ...] = field(default_factory=tuple)
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "provider": self.provider,
            "source": self.source,
            "complete": self.complete,
            "custom_allowed": True,
            "choices": [choice.as_dict() for choice in self.choices],
            "error": self.error,
        }


def model_choices(role: str, provider: str, *, codex_executable: str = "codex") -> ModelChoices:
    """The models to offer for ``role`` run by ``provider``."""

    cap = capability(provider)
    if not cap.supports_model:
        return ModelChoices(role=role, provider=provider, source=SOURCE_NONE, complete=True)
    if provider == codex_cli.PROVIDER_ID:
        try:
            catalog = codex_cli.list_models(codex_executable)
        except ProviderError as exc:
            return ModelChoices(role=role, provider=provider, source=SOURCE_NONE, complete=False, error=str(exc))
        return ModelChoices(
            role=role,
            provider=provider,
            source=SOURCE_PROVIDER_CATALOG,
            # The catalog lists what Codex offers by name; an account may
            # still reach models it hides, so it is not a closed set.
            complete=False,
            choices=tuple(
                ModelChoice(
                    model=entry.model,
                    display_name=entry.display_name,
                    effort_levels=entry.effort_levels,
                    default_effort=entry.default_effort,
                )
                for entry in catalog
            ),
        )
    if provider == claude_cli.PROVIDER_ID:
        return ModelChoices(
            role=role,
            provider=provider,
            source=SOURCE_ENGINE_KNOWN,
            complete=False,
            choices=tuple(ModelChoice(model=model, display_name=name) for model, name in claude_cli.KNOWN_MODELS),
        )
    return ModelChoices(role=role, provider=provider, source=SOURCE_NONE, complete=False)


__all__ = [
    "ModelChoice",
    "ModelChoices",
    "SOURCE_ENGINE_KNOWN",
    "SOURCE_NONE",
    "SOURCE_PROVIDER_CATALOG",
    "model_choices",
]
