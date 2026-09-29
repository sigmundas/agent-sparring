"""The one place that resolves which provider, model and effort a role runs with.

Two roles take a provider turn: the stage agent and the sparrer (the
independent reviewer is the sparring role pointed at a different prompt, so
it resolves through the sparring role, not a third one). Every orchestration
path -- ``run-stage``, ``run-sparring``, ``run-loop``, ``run-plan``,
``resume-plan``, the independent-review stage and the finalization turn --
builds its adapters from :func:`resolve_agent_configs`, so none of them can
quietly disagree about what the project configured.

Precedence. The provider is a project decision:

    explicit CLI override  >  environment  >  .sparring/project.toml  >  engine default

Model and effort are the person's own preferences, shared by every project
and keyed by role *and* the provider just resolved:

    explicit CLI override  >  environment  >  user preference  >  provider default

There is no project-level model or effort any more: such keys in
``project.toml`` are reported as obsolete setup problems and never used.

"Provider default" means the engine passes no flag at all and the provider
CLI does whatever it normally does. The engine never writes a guessed model
name into that gap, and :func:`resolve_agent_configs` reports the source of
each value so a UI can say "provider default" honestly instead of inventing
one.

The user preference layer (:mod:`agent_sparring.user_config`) lives outside
every repository, so choosing a model never dirties a working tree, and it
is keyed by provider, so a preference for one provider is never applied to
another. A preference changed while a managed run is going applies from the
next stage: the loop re-resolves before each one.

The environment layer is for a one-off: trying a different model for one run
in one shell, without changing the saved preference. It sits below the
command line (an explicit flag is still the most specific thing a person can
say) and above the saved preference. The variables are
listed in :data:`ENV_VARS` and validated exactly as the file's values are --
an unsupported effort level or an unimplemented provider is the same
configuration error whichever layer supplied it.

Provider capabilities below were probed against the installed CLIs, not
assumed -- see :data:`PROVIDER_CAPABILITIES` for what was verified and how.
Effort is deliberately modelled as a per-provider enumeration rather than a
single engine-wide one: the two installed CLIs overlap on
``low|medium|high|xhigh|max`` but do not agree beyond that, and they express
the setting through completely different argv. Translating an unsupported
level into "the nearest thing" would be the engine claiming support a
provider does not have, so an unsupported level is a configuration error
raised before any provider process starts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from agent_sparring.config import ProjectConfig, ProjectConfigError
from agent_sparring.user_config import UserPreferences, load_user_preferences
from agent_sparring.providers import claude_cli, codex_cli

ROLE_STAGE = "stage"
ROLE_SPARRING = "sparring"
ROLES: tuple[str, ...] = (ROLE_STAGE, ROLE_SPARRING)

# The three fields a role can have set, in the order they are reported.
FIELDS: tuple[str, ...] = ("provider", "model", "effort")

# Where a resolved value came from. Reported verbatim in the effective-config
# output so a UI never has to guess.
SOURCE_CLI = "cli"
SOURCE_ENV = "env"
SOURCE_PROJECT = "project"
SOURCE_USER = "user"
SOURCE_ENGINE_DEFAULT = "engine-default"
SOURCE_PROVIDER_DEFAULT = "provider-default"

# The environment variable that sets one role's one field. Mechanically
# derived rather than hand-picked so the rule can be stated in a sentence --
# ``SPARRING_`` + the role name + ``_`` + the field name, upper-cased -- and
# so a new role or field cannot gain a variable name by accident. The
# sparring role's variables do read ``SPARRING_SPARRING_*``; the doubling is
# the honest spelling of "the sparring role of the sparring engine" and is
# preferred over a second, prettier name for the same role.
ENV_PREFIX = "SPARRING_"


def env_var(role: str, field: str) -> str:
    """The environment variable name for one role's one field."""

    return f"{ENV_PREFIX}{role.upper()}_{field.upper()}"


#: Every variable this module reads, as ``{role: {field: variable}}``.
#: Nothing outside this mapping is consulted, so an unrecognised
#: ``SPARRING_*`` variable in the environment is inert rather than
#: mysteriously effective.
ENV_VARS: Mapping[str, Mapping[str, str]] = {
    role: {field: env_var(role, field) for field in FIELDS} for role in ROLES
}


class AgentConfigError(ProjectConfigError):
    """Raised when a role's provider/model/effort selection cannot be honoured.

    Subclasses :class:`~agent_sparring.config.ProjectConfigError` so the
    existing command error handling reports it as the configuration problem
    it is, rather than as a provider failure after a process has started.
    """


@dataclass(frozen=True)
class ProviderCapability:
    """What one provider CLI can actually be told to do.

    ``effort_levels`` empty means the provider exposes no effort/reasoning
    setting at all; configuring one is then an error rather than a no-op.
    ``effort_argv_note`` is documentation, not behaviour -- the argv itself
    is built by the adapter module that owns the provider.
    """

    provider_id: str
    display_name: str
    roles: tuple[str, ...]
    supports_model: bool
    effort_levels: tuple[str, ...]
    effort_argv_note: str | None = None

    @property
    def supports_effort(self) -> bool:
        return bool(self.effort_levels)


# Verified against the installed CLIs on 2026-09-18:
#
# claude 2.1.277:
#   --model <model>   "Provide an alias for the latest model (e.g. 'fable',
#                     'opus', or 'sonnet') or a model's full name". Free-form:
#                     the CLI does not enumerate accepted values, so the
#                     engine must not either.
#   --effort <level>  "Effort level for the current session (low, medium,
#                     high, xhigh, max)". A stable, documented enum. An
#                     unrecognised value is NOT rejected by the CLI: it prints
#                     "Warning: Unknown --effort value 'bogus' - ignoring it
#                     and using the default effort" and runs the turn anyway.
#                     That silent downgrade is exactly why the engine
#                     validates the level itself, before launching.
#
# codex-cli 0.153.4:
#   -m/--model <MODEL>            Free-form, on both "codex exec" and
#                                 "codex exec resume".
#   -c model_reasoning_effort=... Codex has no --effort flag. Reasoning
#                                 effort is a config override, accepted by
#                                 both "codex exec" and "codex exec resume"
#                                 (the same mechanism the read-only sandbox
#                                 already uses). The level enum was read out
#                                 of the installed binary and cross-checked
#                                 against "codex debug models", whose catalog
#                                 reports per-model "supported_reasoning_levels"
#                                 drawn from this same set.
#
# Per-model narrowing is deliberately NOT enforced here. "codex debug models"
# shows that the levels a given model accepts are a subset of this enum and
# that the subset changes with the catalog; pinning today's subset into the
# engine would reject a level a future model gains. The engine validates the
# vocabulary, and the provider remains the authority on its own models.
#
# The level vocabularies themselves live with the adapters that turn them
# into argv, so a provider's facts stay in one module.
CLAUDE_EFFORT_LEVELS: tuple[str, ...] = claude_cli.EFFORT_LEVELS
CODEX_EFFORT_LEVELS: tuple[str, ...] = codex_cli.EFFORT_LEVELS

PROVIDER_CAPABILITIES: Mapping[str, ProviderCapability] = {
    claude_cli.PROVIDER_ID: ProviderCapability(
        provider_id=claude_cli.PROVIDER_ID,
        display_name=claude_cli.DISPLAY_NAME,
        roles=(ROLE_STAGE,),
        supports_model=True,
        effort_levels=CLAUDE_EFFORT_LEVELS,
        effort_argv_note="--effort <level>",
    ),
    codex_cli.PROVIDER_ID: ProviderCapability(
        provider_id=codex_cli.PROVIDER_ID,
        display_name=codex_cli.DISPLAY_NAME,
        roles=(ROLE_SPARRING,),
        supports_model=True,
        effort_levels=CODEX_EFFORT_LEVELS,
        effort_argv_note='-c model_reasoning_effort="<level>"',
    ),
}

# What a role falls back to when neither the command line nor project.toml
# names a provider. Matches the historical defaults of run-stage/run-sparring.
DEFAULT_PROVIDERS: Mapping[str, str] = {
    ROLE_STAGE: "claude-cli",
    ROLE_SPARRING: "codex-cli",
}

_ROLE_LABELS: Mapping[str, str] = {
    ROLE_STAGE: "stage agent",
    ROLE_SPARRING: "sparring agent",
}


@dataclass(frozen=True)
class ResolvedAgentConfig:
    """One role's effective provider turn configuration.

    ``model``/``effort`` being ``None`` means "pass no flag; let the provider
    do what it normally does" -- never "the engine could not work it out".
    The ``*_source`` fields say which layer of the precedence chain supplied
    each value.
    """

    role: str
    provider: str
    provider_display_name: str
    provider_source: str
    model: str | None
    model_source: str
    effort: str | None
    effort_source: str
    effort_supported: bool
    effort_levels: tuple[str, ...]
    # Every provider implemented for this role, so a UI can offer the real
    # choice -- or, seeing one entry, show the provider as the fixed fact it
    # currently is -- without keeping its own list of who exists.
    provider_choices: tuple[ProviderCapability, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form for ``sparring show-config --json``.

        Contains resolved configuration only. A value the environment
        supplied appears as an ordinary value with source ``env``; the
        environment itself is never enumerated here, and no secret or
        provider credential is ever included.
        """

        return {
            "role": self.role,
            "provider": self.provider,
            "provider_display_name": self.provider_display_name,
            "provider_source": self.provider_source,
            "model": self.model,
            "model_source": self.model_source,
            "effort": self.effort,
            "effort_source": self.effort_source,
            "effort_supported": self.effort_supported,
            "effort_levels": list(self.effort_levels),
            "provider_choices": [
                {
                    "provider": cap.provider_id,
                    "display_name": cap.display_name,
                    "supports_model": cap.supports_model,
                    "effort_supported": cap.supports_effort,
                    "effort_levels": list(cap.effort_levels),
                }
                for cap in self.provider_choices
            ],
        }


@dataclass(frozen=True)
class RoleOverrides:
    """The explicit command-line overrides for one role. All optional."""

    provider: str | None = None
    model: str | None = None
    effort: str | None = None


def read_role_env(
    role: str, environ: Mapping[str, str] | None = None
) -> RoleOverrides:
    """One role's settings as the environment states them.

    A variable that is absent is simply not set. A variable that is present
    but empty or whitespace is an error rather than a silent fallback to
    ``project.toml``: someone who exported the name meant to select
    something, and quietly running the previous model instead is exactly the
    failure this module exists to prevent. To stop overriding, unset the
    variable.
    """

    source = os.environ if environ is None else environ
    values: dict[str, str | None] = {}
    for field in FIELDS:
        name = ENV_VARS[role][field]
        if name not in source:
            values[field] = None
            continue
        raw = source[name]
        if not raw.strip():
            raise AgentConfigError(
                f"environment variable {name} is set but empty; unset it to use "
                f"the project's configured {role} agent {field}"
            )
        values[field] = raw.strip()
    return RoleOverrides(**values)


def capability(provider: str) -> ProviderCapability:
    """The capability record for ``provider``, or raise :class:`AgentConfigError`."""

    try:
        return PROVIDER_CAPABILITIES[provider]
    except KeyError:
        known = ", ".join(sorted(PROVIDER_CAPABILITIES))
        raise AgentConfigError(
            f"unknown provider {provider!r}; known providers: {known}"
        ) from None


def _layered(
    override: str | None, env: str | None, stored: str | None, stored_source: str
) -> tuple[str | None, str | None]:
    """The most specific value of the layers, and which one supplied it.

    ``stored`` is the persistent layer for the field -- ``project.toml`` for a
    provider, the user preference for model and effort -- named by
    ``stored_source``. ``(None, None)`` means no layer set the field, which
    each caller turns into the right kind of default.
    """

    if override is not None:
        return override, SOURCE_CLI
    if env is not None:
        return env, SOURCE_ENV
    if stored is not None:
        return stored, stored_source
    return None, None


def _resolve_provider(
    role: str, override: str | None, env: str | None, configured: str | None
) -> tuple[str, str]:
    value, source = _layered(override or None, env or None, configured or None, SOURCE_PROJECT)
    if value is None or source is None:
        return DEFAULT_PROVIDERS[role], SOURCE_ENGINE_DEFAULT
    return value, source


def _require_role(role: str, cap: ProviderCapability) -> None:
    if role in cap.roles:
        return
    # Phrased as it always has been: the provider is a real provider, it just
    # is not implemented for this role yet.
    servers = sorted(
        other.provider_id
        for other in PROVIDER_CAPABILITIES.values()
        if role in other.roles
    )
    only = ", ".join(repr(name) for name in servers) or "no provider"
    raise AgentConfigError(
        f"unsupported {_ROLE_LABELS[role]} provider {cap.provider_id!r}; only "
        f"{only} is implemented so far"
    )


def _where(role: str, cap: ProviderCapability, field: str, source: str) -> str:
    """Name the layer a value came from, so a refusal sends the reader to the
    right place to change it. The CLI flag is spelled --effort by run-stage and
    --stage-effort / --sparring-effort by the loop commands, so name the role
    rather than one command's flag."""

    return {
        SOURCE_CLI: f"the {role} agent {field} override",
        SOURCE_ENV: env_var(role, field),
        SOURCE_USER: f"your {role} agent {field} preference for {cap.provider_id}",
    }[source]


def _check_effort(role: str, cap: ProviderCapability, value: str, source: str) -> None:
    where = _where(role, cap, "effort", source)
    if not cap.supports_effort:
        raise AgentConfigError(
            f"{where} is not supported by provider {cap.provider_id!r}: that provider "
            f"exposes no effort/reasoning setting, so the engine will not pretend to "
            f"apply one. Remove the setting, or choose a provider that supports it"
        )
    if value not in cap.effort_levels:
        supported = ", ".join(cap.effort_levels)
        raise AgentConfigError(
            f"{where} value {value!r} is not supported by provider "
            f"{cap.provider_id!r}; supported levels: {supported}"
        )


def _check_model(role: str, cap: ProviderCapability, source: str) -> None:
    if not cap.supports_model:
        raise AgentConfigError(
            f"{_where(role, cap, 'model', source)} is not supported by provider "
            f"{cap.provider_id!r}: that provider does not accept a model selection"
        )
    # Model names are deliberately not enumerated: both installed CLIs accept
    # free-form model names and gain new ones without an engine release. The
    # provider remains the authority on which names exist.


def _resolve_effort(
    role: str,
    cap: ProviderCapability,
    override: str | None,
    env: str | None,
    preferred: str | None,
) -> tuple[str | None, str]:
    value, source = _layered(override, env, preferred, SOURCE_USER)
    if value is None or source is None:
        return None, SOURCE_PROVIDER_DEFAULT
    _check_effort(role, cap, value, source)
    return value, source


def _resolve_model(
    role: str,
    cap: ProviderCapability,
    override: str | None,
    env: str | None,
    preferred: str | None,
) -> tuple[str | None, str]:
    value, source = _layered(override, env, preferred, SOURCE_USER)
    if value is None or source is None:
        return None, SOURCE_PROVIDER_DEFAULT
    _check_model(role, cap, source)
    return value, source


def validate_preference(role: str, provider: str, field: str, value: str) -> None:
    """Refuse a user preference no provider turn could run with, before it is saved.

    The same checks a run applies to a resolved value, plus one only a saved
    preference needs: a provider alias that means "the latest of a family"
    (``opus``) is refused, because a preference is shown as the exact model
    it names and an alias would silently change underneath it. A one-off
    command-line or environment override may still use one.
    """

    if role not in ROLES:
        raise AgentConfigError(f"unknown agent role {role!r}; known roles: {', '.join(ROLES)}")
    cap = capability(provider)
    _require_role(role, cap)
    if field == "effort":
        _check_effort(role, cap, value, SOURCE_USER)
    elif field == "model":
        _check_model(role, cap, SOURCE_USER)
        if value.strip().startswith("-"):
            raise AgentConfigError(
                f"{value!r} is not a model id: a saved model must not begin with '-', which a "
                f"provider command line would read as an option"
            )
        if provider == claude_cli.PROVIDER_ID and value.strip().lower() in claude_cli.MODEL_ALIASES:
            exact = ", ".join(model for model, _ in claude_cli.KNOWN_MODELS)
            raise AgentConfigError(
                f"{value!r} is an alias for whichever {cap.display_name} model is latest, not an "
                f"exact model; save an exact model id instead (for example {exact})"
            )
    else:
        raise AgentConfigError(f"{field!r} is not a user preference; only model and effort are")


def resolve_role_config(
    role: str,
    config: ProjectConfig | None,
    overrides: RoleOverrides = RoleOverrides(),
    *,
    environ: Mapping[str, str] | None = None,
    preferences: UserPreferences | None = None,
) -> ResolvedAgentConfig:
    """Resolve one role's effective provider/model/effort.

    ``config`` may be ``None`` when the project has no ``project.toml`` at
    all; the command line, the environment, the user preferences and the
    engine defaults then supply everything. ``environ`` defaults to the real
    process environment and ``preferences`` to the user preference file
    (:func:`~agent_sparring.user_config.load_user_preferences`); both exist so
    tests can supply their own.
    """

    if role not in ROLES:
        raise AgentConfigError(f"unknown agent role {role!r}; known roles: {', '.join(ROLES)}")

    env = read_role_env(role, environ)

    configured_provider = None
    if config is not None:
        configured_provider = (
            config.stage_agent_provider if role == ROLE_STAGE else config.sparring_agent_provider
        )

    provider, provider_source = _resolve_provider(
        role, overrides.provider, env.provider, configured_provider
    )
    cap = capability(provider)
    _require_role(role, cap)

    if preferences is None:
        preferences = load_user_preferences()
    # Keyed by the provider just resolved: a preference saved for another
    # provider is never applied to this one.
    preferred = preferences.for_role(role, provider)
    model, model_source = _resolve_model(role, cap, overrides.model, env.model, preferred.model)
    effort, effort_source = _resolve_effort(role, cap, overrides.effort, env.effort, preferred.effort)

    return ResolvedAgentConfig(
        role=role,
        provider=provider,
        provider_display_name=cap.display_name,
        provider_source=provider_source,
        model=model,
        model_source=model_source,
        effort=effort,
        effort_source=effort_source,
        effort_supported=cap.supports_effort,
        effort_levels=cap.effort_levels,
        provider_choices=providers_for_role(role),
    )


def providers_for_role(role: str) -> tuple[ProviderCapability, ...]:
    """Every provider implemented for ``role``, in a stable order.

    The engine answers "which providers could this role use" so that no UI
    has to keep its own copy of the answer and then go stale when a provider
    is added or one gains a role.
    """

    return tuple(
        cap
        for cap in sorted(PROVIDER_CAPABILITIES.values(), key=lambda item: item.provider_id)
        if role in cap.roles
    )


@dataclass(frozen=True)
class EffectiveAgents:
    """Both roles resolved together, as every orchestration path needs them."""

    stage: ResolvedAgentConfig
    sparring: ResolvedAgentConfig

    def as_dict(self) -> dict[str, object]:
        return {ROLE_STAGE: self.stage.as_dict(), ROLE_SPARRING: self.sparring.as_dict()}


def resolve_agent_configs(
    config: ProjectConfig | None,
    *,
    stage: RoleOverrides = RoleOverrides(),
    sparring: RoleOverrides = RoleOverrides(),
    environ: Mapping[str, str] | None = None,
    preferences: UserPreferences | None = None,
) -> EffectiveAgents:
    """Resolve both roles. The single entry point every command goes through.

    The preference file is read once, so both roles see the same snapshot.
    """

    if preferences is None:
        preferences = load_user_preferences()
    return EffectiveAgents(
        stage=resolve_role_config(ROLE_STAGE, config, stage, environ=environ, preferences=preferences),
        sparring=resolve_role_config(
            ROLE_SPARRING, config, sparring, environ=environ, preferences=preferences
        ),
    )


__all__ = [
    "AgentConfigError",
    "CLAUDE_EFFORT_LEVELS",
    "CODEX_EFFORT_LEVELS",
    "DEFAULT_PROVIDERS",
    "ENV_PREFIX",
    "ENV_VARS",
    "EffectiveAgents",
    "FIELDS",
    "PROVIDER_CAPABILITIES",
    "ProviderCapability",
    "ROLES",
    "ROLE_SPARRING",
    "ROLE_STAGE",
    "ResolvedAgentConfig",
    "RoleOverrides",
    "SOURCE_CLI",
    "SOURCE_ENGINE_DEFAULT",
    "SOURCE_ENV",
    "SOURCE_PROJECT",
    "SOURCE_PROVIDER_DEFAULT",
    "SOURCE_USER",
    "capability",
    "env_var",
    "providers_for_role",
    "read_role_env",
    "resolve_agent_configs",
    "resolve_role_config",
    "validate_preference",
]
