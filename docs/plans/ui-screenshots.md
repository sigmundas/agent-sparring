# Agent Sparring — Automated UI visual review

**Status:** Proposed, not approved or started  
**Primary repository:** `agent-sparring`  
**Pilot repository:** `sporely-landing`  
**Later consumers:** `sporely-web`, `sporely-py`

## Goal

Allow Agent Sparring to render application screenshots and have the independent sparrer inspect them against reference mockups and explicit visual requirements before declaring a UI stage READY.

Screenshot capture is repository-specific. Evidence validation, delivery, candidate binding and review are owned by the generic engine.

The existing implementation/review loop, candidate identities, human gates and non-UI workflows must remain unchanged unless visual review is explicitly enabled.

---

# Run A — Agent Sparring engine

## Stage 1 — Visual evidence contract and image-capability proof

**Outcome:** Establish that the configured reviewer can actually inspect image pixels, and define how visual evidence will be represented.

Implementation:

- Verify that the Codex CLI adapter can deliver images to the selected reviewer model, including on resumed sessions.
- Prove the reviewer can distinguish an intentionally different rendered image from its reference. Merely reading the PNG filename is insufficient.
- Define a small versioned evidence manifest containing screenshot ID, relative path, viewport, capture status and optional reference image.
- Specify how evidence is bound to the exact candidate content being reviewed.
- Document an explicit unsupported state for providers that cannot inspect images.
- Establish that screenshot capture must remain independent of reviewer write permissions.

**Acceptance:**

- A real image-inspection test passes through the production provider adapter.
- A deliberately mismatched reference is detected.
- Unsupported image input is reported, never silently treated as successful inspection.
- The contract does not depend on Playwright, JavaScript, React or Qt.

**Gate:** Do not proceed until image delivery has been demonstrated with the actual Codex reviewer configuration. If the existing adapter cannot do this, resolve that capability first.

## Stage 2 — Generic capture execution and evidence management

**Outcome:** The engine can invoke a repository-provided screenshot command and collect its results.

Implementation:

- Add an optional visual-review configuration to the repository's Agent Sparring project settings.
- The repository supplies a trusted capture command and output contract.
- The engine invokes capture after a successful implementation handoff, before independent review.
- Capture runs again after each implementation correction.
- Save outputs under an ignored, run-owned artifact directory.
- Validate manifest structure, file paths, image formats and successful completion.
- Record candidate identity, viewport, paths and image hashes.
- Enforce a timeout and prevent stale captures from being reused.
- Preserve normal behavior when visual review is disabled.

**Acceptance:**

- A test capture command produces a valid manifest and PNG files.
- Missing, corrupt, stale or out-of-directory screenshots are rejected.
- Capture failure does not become a READY verdict.
- Screenshots do not dirty the Git candidate or change its accepted identity.
- Ordinary non-UI stage execution remains unchanged.

## Stage 3 — Image-aware sparrer review

**Outcome:** The sparrer receives actual screenshots and references as part of its independent review.

Implementation:

- Deliver the manifest's screenshots and configured reference images to the reviewer through the verified image-capable provider path.
- Include written visual acceptance criteria alongside the images.
- Instruct the reviewer to compare layout, proportions, clipping, responsive behavior, legibility and visual hierarchy.
- Distinguish structural mismatches from permitted variations, such as differing numeric measurements.
- Require actionable findings identifying the failing screenshot and criterion.
- Preserve `SEND_BACK` and reviewer session continuity.
- Keep subjective product/aesthetic decisions eligible for human review.
- Do not allow a textual claim of visual success to replace actual image evidence.

**Acceptance:**

- An intentionally defective screenshot produces an actionable review finding.
- Correct evidence can produce READY without unnecessary manual layout checks.
- On SEND_BACK, new candidate screenshots replace the previous capture set for the next review.
- Reviewer independence and read-only constraints remain intact.
- Existing human-gate and deferred-verification behavior is preserved.

---

# Run B — sporely-landing pilot

## Stage 4 — Deterministic Playwright screenshot capture

**Outcome:** `sporely-landing` can reliably reproduce the spore panel and capture it at desktop and mobile sizes.

Implementation:

- Add `@playwright/test` as a development dependency, with Chromium as the initial browser.
- Add a repository-owned screenshot script and configuration.
- Use deterministic fixture data representing an observation with multiple preparation groups and literature overlays.
- Avoid production credentials, real cloud writes and dependence on live Supabase data.
- Capture the observation spore panel at 1280 px and 375 px viewport widths.
- Produce PNGs and a manifest compatible with the Stage 1 contract.
- Use the existing desktop and mobile mockups as reference assets.
- Keep generated images outside tracked source directories.
- Provide one documented command for local capture.

**Acceptance:**

- One command reproducibly generates both screenshots and their manifest.
- The application is ready before capture, with fonts and charts rendered.
- The screenshots contain the intended populated panel, not a loading state or error page.
- No external account or manually prepared browser session is required.
- Existing build and unit tests pass.

## Stage 5 — End-to-end visual review pilot

**Outcome:** A normal Agent Sparring implementation stage can be reviewed visually without manual screenshot handling.

Implementation:

- Enable the new visual-review capability for a focused spore-panel stage.
- Compare captured screenshots against `spore-plot-mockup-web.png` and `spore-plot-mockup-mobile.png`.
- Verify the picker, legend, scatter/table proportions, mini histograms, footnote and reference-card placement.
- Introduce a controlled test defect, such as clipping or incorrect mobile column layout.
- Confirm the sparrer detects the defect and requests correction.
- Confirm the corrected implementation is recaptured and reviewed.
- Test ordinary Playwright interactions separately from static visual comparison.
- Confirm Species and Compare retain their existing appearances and behavior.

**Acceptance:**

- The independent reviewer demonstrably inspects the screenshot pixels.
- An introduced layout defect is detected.
- The normal SEND_BACK correction loop works.
- New screenshots are available automatically on subsequent review cycles.
- A passing visual review can satisfy the relevant objective visual checks.
- Final subjective design approval remains with the human.
- No change to existing non-visual stages.

---

## Follow-up — sporely-web and desktop

Once the landing pilot passes:

**sporely-web:** Reuse the same browser capture contract, with repository-specific routes, fixtures and authentication-safe test setup. No new engine capability should be required.

**sporely-py:** Integrate the existing Qt renderer and `manifest.json` through the generic capture contract. Adapt the manifest only where necessary. Do not introduce Playwright or build a second screenshot renderer.

## Non-goals

- Pixel-perfect screenshot matching.
- Replacing functional, numerical or accessibility tests with visual judgments.
- Autonomous decisions about subjective aesthetics.
- Adding browser automation dependencies to the Python desktop app or the Agent Sparring engine.
- Building a VS Code screenshot-comparison interface in the initial implementation.
- Automatically enabling visual review for all stages.

## Completion criteria

The feature is complete when a real Agent Sparring run can:

1. Implement an application UI change.
2. Automatically capture reproducible screenshots.
3. Provide the screenshots and reference mockups to the independent sparrer.
4. Detect and correct meaningful visual defects through SEND_BACK.
5. Reach READY only after the required visual evidence has actually been inspected.

**Stop after Run A is accepted before beginning the landing pilot.** Run B validates the new capability against a real application and should not require architectural changes to the engine.