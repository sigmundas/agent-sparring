# Stage spec: per-role backend selection for `codex-cli`

**Status:** proposed, not implemented
**Repository:** `agent-sparring` (engine only)
**Written for:** the implementer who picks this up, and the reviewer who will
inspect the candidate. Assumes familiarity with `agent_config.py` and
`providers/codex_cli.py`; does not re-explain them.

---

## 1. Problem

`agent-sparring` already resolves three things per role: **provider** (which
CLI — `claude-cli` or `codex-cli`), **model**, and **effort**. It resolves
nothing about *where the model is served from*.

For Codex that destination is `model_provider` in `~/.codex/config.toml`: a
lookup key into a `[model_providers.<key>]` table, or Codex's built-in
`openai` when no table matches. For example, it can select between OpenAI
direct and an Azure AI Foundry deployment:

```toml
# ~/.codex/config.toml
model_provider = "openai"          # or "azure"

[model_providers.azure]
name     = "Azure OpenAI"
base_url = "https://<your-resource>.openai.azure.com/openai/v1"
env_key  = "AZURE_OPENAI_API_KEY"
wire_api = "responses"
```

`CodexCliAdapter._build_args` passes no `--profile` and no
`-c model_provider=…`, and `subprocess_runner` passes no `env=`, so every
sparring turn silently inherits whatever that file's top-level
`model_provider` happens to say. Consequences today:

- The destination is invisible. `sparring show-config --json` reports
  provider/model/effort and is silent about the endpoint, so a run's record
  does not say which service answered.
- It cannot vary per role or per run. Moving the sparrer to Foundry means
  editing a global file that also moves every interactive `codex` session.
- It is not reproducible. Two runs of the same stage, a `config.toml` edit
  apart, are indistinguishable in the artifacts.

## 2. Scope

**In scope:** a new per-role field, `backend`, resolved by the same
`CLI > project.toml > provider-default` chain as model and effort, honoured by
`CodexCliAdapter` and reported by `show-config`/`set-config`.

**Out of scope, deliberately:**

- **`claude-cli` backends.** Claude Code selects Bedrock/Vertex/custom
  endpoints through *environment variables* (`CLAUDE_CODE_USE_BEDROCK`,
  `ANTHROPIC_BASE_URL`, …), not argv or a config-override flag.
  `subprocess_runner.run_streaming` currently passes no `env=` at all, so
  supporting it means giving the runner a reviewed environment-injection
  capability — a larger, credential-adjacent change with its own security
  argument. This stage declares `claude-cli` as having **no** backend axis, so
  configuring one for the stage role is a clear configuration error rather
  than a silent no-op. See §8.
- Validating that a named backend exists in the user's `config.toml`. The
  engine does not read another tool's config; see §4.3.
- Anything about credentials. The engine never reads, forwards or logs
  `AZURE_OPENAI_API_KEY` or any other secret. The backend name is a
  configuration label, and `ResolvedAgentConfig.as_dict` stays
  secrets-free.

## 3. Naming

`provider` in this engine already means *which CLI binary*. Codex's own term
for the new axis is also `model_provider`, and reusing "provider" for both
would make `[agents.sparring] provider = "codex-cli"` sit next to
`provider = "azure"` meaning something unrelated.

The field is therefore called **`backend`**: *which service the chosen
provider CLI talks to*. `model_provider` appears only inside
`providers/codex_cli.py`, where it is Codex's vocabulary and correct.

## 4. Design

### 4.1 Why `-c model_provider=…` and not `--profile`

`codex exec` accepts `-p <name>`, which layers the whole of
`$CODEX_HOME/<name>.config.toml` on top of the base config. That file is
arbitrary TOML the engine has not read and cannot constrain — **including a
`sandbox_mode` key**. `CodexCliAdapter`'s central invariant (module docstring,
lines 52–70) is that read-only is not configurable: no field, no attribute, no
`extra_args`, and the only two `-c` keys it can ever emit are the hard-coded
`sandbox_mode` and a closed-enum `model_reasoning_effort`. Accepting `-p`
would hand exactly the capability that invariant exists to deny to a file
named by project configuration.

`-c model_provider="<name>"` is a single key that cannot name another key.
It preserves the invariant and is accepted by both `codex exec` and
`codex exec resume` — the same mechanism `sandbox_mode` and
`model_reasoning_effort` already use.

### 4.2 The value is a closed *charset*, but not for the reason first given

**Corrected 2026-09-23 after probing the installed CLI.** An earlier draft of
this section claimed that a value containing `"` could close the TOML string
and append a second assignment — `x", sandbox_mode="danger-full-access` —
and justified the charset restriction as a fix for that. **That is not true
of codex-cli 0.153.4.** The claim was tested directly, with the exact argv
this adapter would build:

```
$ codex exec -c 'model_provider="x", sandbox_mode="danger-full-access"' …
Error: Model provider `x", sandbox_mode="danger-full-access` not found
```

The entire payload became one literal provider name. `codex --help` explains
why: for `-c key=value`, "The `value` portion is parsed as TOML. If it fails
to parse as TOML, the raw string is used as a literal." Codex splits on the
first `=` and parses the remainder as a single TOML *value*, so no value can
introduce a second key. The injection was never reachable.

The security argument does not depend on it. It is simpler and still holds:
`-c model_provider="<name>"` grants strictly less configuration authority
than `--profile`, which layers an arbitrary file that may carry
`sandbox_mode`. That is §4.1, and it stands on its own.

`backend` is still validated at construction against:

```python
_BACKEND_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
```

but for three honest reasons rather than one false one: it keeps a recorded
`backend` a meaningful label rather than arbitrary text; it keeps argv
predictable and greppable; and it is cheap insurance should Codex's `-c`
parser ever change. Codex provider-table keys are TOML bare keys in practice,
so this rejects nothing real. Validation lives in
`CodexCliAdapter.__post_init__` beside the existing effort check, so an
invalid value fails before any process exists — and is mirrored in
`agent_config` so it fails at configuration time with a configuration error.

Do not restate the injection rationale in the module docstring. An untrue
security claim is worse than none: it invites the next contributor to relax
the charset once they discover the claim is false, taking the three real
reasons with it. Record the §4.1 authority argument instead.

The docstring currently states "the one other `-c` this adapter can emit is
`model_reasoning_effort`"; that sentence becomes wrong on landing and must be
updated, not left to drift. Worth adding beside it: dotted `-c` paths *do*
reach nested tables (`-c model_providers.azure.base_url="…"` was verified to
work), which is why the *key* in any such template must stay hard-coded and
never be composed from configuration.

### 4.3 Backend names are free-form, like model names

`agent_config` deliberately does not enumerate model names: "both installed
CLIs accept free-form model names/aliases and gain new ones without an engine
release. The provider remains the authority on its own names."

Backend names are the same kind of fact, more so: they are keys in a file on
the *user's* machine. A closed engine-side list would reject a provider table
the user added five minutes ago. The engine validates the **shape** (§4.2) and
whether the resolved provider has a backend axis **at all** (§4.4); the
existence of a named backend is Codex's business, and Codex already reports a
clear error for an unknown one.

### 4.4 Capability model

`ProviderCapability` gains one field, mirroring how `effort_levels` works:

```python
supports_backend: bool = False
backend_argv_note: str | None = None
```

- `codex-cli`: `supports_backend=True`,
  `backend_argv_note='-c model_provider="<name>"'`
- `claude-cli`: `supports_backend=False`

`supports_backend=False` means configuring a backend for that role raises
`AgentConfigError` with the same phrasing shape the effort path already uses
("the engine will not pretend to apply one"), rather than accepting a value
and ignoring it.

Note the asymmetry with `effort_levels`: effort exposes a vocabulary because
one exists; backend exposes only a boolean, because the vocabulary lives on
the user's machine. A UI that wants to *offer* backend choices must read
`~/.codex/config.toml` itself — the engine will not claim to know them. (This
is the contract the `agent-backends-vscode` extension relies on — see its
`SPEC.md` in the sibling checkout.)

## 5. Changes, by file

| File | Change |
| --- | --- |
| `providers/codex_cli.py` | `backend: str \| None = None` field; `_BACKEND_RE` + validation in `__post_init__`; emit `-c model_provider="<backend>"` in `_build_args` after the sandbox arg; update the module docstring's "only other `-c`" claim and add the §4.1/§4.2 reasoning |
| `agent_config.py` | `ProviderCapability.supports_backend` / `backend_argv_note`; `RoleOverrides.backend`; `ResolvedAgentConfig.backend` + `backend_source` + `backend_supported`; `_resolve_backend()` beside `_resolve_effort()`; `as_dict()` and the `provider_choices` entries carry it |
| `config.py` | `stage_agent_backend` / `sparring_agent_backend` on `ProjectConfig`; add `"backend"` to `_AGENT_ROLE_KEYS`; parse via the existing `_optional_str` |
| `config_edit.py` | `RoleEdit` gains `backend` / `backend_default`; `_apply` writes or removes the key; `_explain` describes it |
| `cli.py` | `--stage-backend` / `--sparring-backend` on the both-roles parsers; `--backend` on the single-role parsers; `--backend` / `--backend-default` mutually-exclusive pair on `set-config`; thread through `RoleOverrides` |
| adapter construction sites | Pass `backend=resolved.backend` wherever `model=`/`effort=` are already passed. Grep for `CodexCliAdapter(` — every construction must be updated or the field silently stays `None` |
| `README.md`, `docs/design.md` | Document the axis and the `-c`-not-`--profile` rationale |

`None` continues to mean **"pass no flag"** — the CLI then uses whatever
`~/.codex/config.toml` says, and the engine claims nothing about what that
is. `backend_source` reports `provider-default` in that case. This keeps the
change fully backward compatible: an unconfigured project behaves exactly as
today.

## 6. Configuration surface

```toml
# .sparring/project.toml
[agents.sparring]
provider = "codex-cli"
model    = "gpt-6-astra"
effort   = "high"
backend  = "azure"        # new; omit for "whatever config.toml says"
```

```
sparring set-config sparring --backend azure
sparring set-config sparring --backend-default
sparring run-sparring --sparring-backend openai ...
sparring show-config --json      # now reports backend + backend_source
```

## 7. Verification

Unit, following existing patterns:

1. `tests/test_providers_codex_cli.py` — argv contains
   `-c model_provider="azure"` on **both** `start` and `resume`; absent when
   `backend is None`; the sandbox arg is still present and unchanged in every
   case.
2. Same file — each of `az ure`, `az"ure`, `a=b`, `""`, a 65-character name,
   and `x", sandbox_mode="danger-full-access` raises `ProviderError` at
   construction, and no process is launched.
3. `tests/test_agent_config.py` — precedence `cli > project > provider-default`
   for `backend`; `backend_source` correct at each layer.
4. Same file — `backend` on the `stage` role (`claude-cli`) raises
   `AgentConfigError` naming the unsupported axis.
5. `tests/test_config.py` — `backend` parses; a misspelling (`backned`) is
   rejected by `_reject_unknown_agent_keys`.
6. `tests/test_config_edit.py` — set, change and clear `backend`; comments and
   unrelated keys preserved; a no-op request writes nothing.
7. `tests/test_cli.py` — `show-config --json` includes `backend` and
   `backend_source`; `set-config --backend` round-trips.

Live probes the implementer must run and record in the handoff (this repo's
convention is that provider behaviour is verified, not assumed):

8. `codex exec -c model_provider="azure" -c sandbox_mode="read-only" …`
   reaches the Foundry endpoint and still refuses a write.
9. The same via `codex exec resume`.
10. **Duplicate-key precedence — already answered, 2026-09-23.** The **last
    `-c` occurrence wins**, verified in both orders against codex-cli
    0.153.4:

    ```
    -c model_provider="azure" -c model_provider="bogus"  →  Error: … `bogus` not found
    -c model_provider="bogus" -c model_provider="other"  →  Error: … `other` not found
    ```

    Record it in the docstring beside the other verified provider facts.
    Nothing should *rely* on it: per `gpt-advice.md` §3, if an invocation
    ever carries an explicit `model_provider` override while a `backend` is
    configured, fail with a configuration conflict. The precedence is
    unambiguous to the parser but not to the person reading two settings that
    disagree. (There is no `extra_args` passthrough today, so this guard is
    currently unreachable — write it so it stays correct if one is added,
    not as live logic pretending to guard something.)
11. **Already answered, 2026-09-23.** An unknown backend name produces a
    clear error and **no silent fallback**:

    ```
    $ codex exec -c model_provider="definitely_not_a_provider" …
    Error: Model provider `definitely_not_a_provider` not found
    ```

    Non-zero exit, no session started. The reported `backend` can therefore
    be trusted as a record of what actually answered: a mismatch cannot
    silently occur.

Probes 8 and 9 (live Foundry reachability and read-only enforcement on both
`exec` and `exec resume`) remain outstanding — they spend real tokens against
the deployment and need explicit go-ahead. Probes 10 and 11, and the §4.2
correction, were run from the `backend-switcher` checkout; the full records
are in `backend-switcher/docs/verified-cli-behaviour.md`.

## 8. Follow-on, not this stage

`claude-cli` backends via a reviewed `env=` capability on
`subprocess_runner.run_streaming`. Worth a separate security argument because
it puts the engine on the path of credential-bearing environment variables,
which nothing in it touches today. `supports_backend=False` on `claude-cli` is
the honest interim state and needs no migration when that lands.
