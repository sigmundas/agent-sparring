# Live browser verification: minimum guards

Status: proposal. Nothing on this page is implemented in the engine yet.

Agent-executed verification is preferred over human gates (see
[intake](intake.md) and [human gates](gates.md)). Before an agent resumes
live Playwright/browser or HTTP verification against a shared environment,
these guards must be enforced by code, not by prompt wording. The trigger was
the sporely-landing incident (`docs/perf/2026-10-11-rpc-timeout-incident.md`,
"Proposed safeguards for future Playwright runs"), where ad-hoc browser runs
loaded a production database.

## Minimum enforceable guards

1. **Target allowlist.** The run names its allowed hosts explicitly. Any
   request to a host not on the list is aborted and counted. Production hosts
   are refused unless the run carries a specific, recorded production
   authorization for that target.
2. **Per-session budget.** A fixed cap on requests and page loads, set before
   the run starts. Exceeding it stops the run; it is not raised mid-run.
3. **Max concurrency.** A hard cap on in-flight requests/pages (the
   sporely-landing harness uses 2).
4. **Min spacing.** A minimum interval between requests (the harness uses
   250 ms).
5. **Stop on error.** Any 429 or 5xx, or a transport error to an allowed
   host, stops the run. No automatic or unapproved retries.
6. **Execution log.** Every request is recorded: time, method, host/path,
   status, duration, plus the run's target, budget and authorization. The
   log is the evidence the stage reports and the review checks.
7. **No privileged credentials for access-control tests.** Owner/non-owner/
   anonymous checks use test accounts with ordinary roles; never a service
   role, admin key or production user's session.
8. **Synthetic data by default.** Visual checks use routed fixture responses
   (`page.route`) and an unroutable backend URL; live loads are the exception.

## Where each guard lives

| Guard | Engine (agent-sparring) | Application repository |
| --- | --- | --- |
| Allowlist and production refusal | Records the authorized target and refuses to treat a production target as authorized without a recorded human authorization gate | Enforces it in the browser/HTTP harness (request interception, host check at start) |
| Budget, concurrency, spacing, stop-on-error, no retries | Can require the stage to report the limits used as test evidence | Enforces them in the shared harness; only the harness knows the requests |
| Execution log | Carries the log through existing test evidence and review | Writes it |
| Credential rule | Prompt/plan guidance; review checks it | Test-account setup; harness refuses service-role keys |
| Synthetic data | None | Fixture routing in the harness |

The engine cannot see an application's network traffic, so the enforcement
point is the application's harness. sporely-landing's `scripts/perf/lib.mjs`
already enforces 2 in flight, 250 ms spacing and stop on 5xx/429, but only
for its gated perf harness; ad-hoc Playwright scripts and other repositories
have no equivalent. Each application needs one shared, guarded entry point
that all agent browser runs go through, and plans should name it as the only
permitted way to run live verification.
