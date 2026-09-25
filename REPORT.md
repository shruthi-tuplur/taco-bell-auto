# Design Report

## 1. Architecture

Single Python process, synchronous, four layers with one-way dependencies:

```
discovery.py ──> llm_client.py (the ONLY LLM dependency)
     │
     ├──> schema.py      (artifact format: the contract between discovery and replay)
     ├──> policy.py      (allowlist, risk classes, redaction; shared by both paths)
     ├──> escalation.py  (control state + human handoff; shared by both paths)
     ├──> run_log.py     (structured, redacted evidence per run)
     └──> playwright_page.py (surface adapter: perceive + act)
replay.py ──> same five modules, and nothing else
```

**Key decisions and trade-offs**

- **Discovery and replay share everything except the brain.** The same surface adapter, policy gate, escalation path and logger are used by both. The only difference is who picks the next action: the LLM (discovery) or the artifact (replay). `replay.py` does not import the LLM client, and a test asserts that.
- **Observation is the accessibility tree, not the DOM or screenshots.** The LLM reads Playwright's ARIA snapshot and names targets by accessible name or visible text. This is the representation that survives on legacy markup and also exists on desktop apps (UIA on Windows, AX on macOS), so the artifact format does not depend on the web.
- **One process, no services.** A queue, a worker pool, and an operator web app are the obvious production shape, but none of them change the interesting decisions (schema, error taxonomy, control transfer). Building them would have been infrastructure for its own sake.
- **Proxy target: tacobell.com ordering.** It gives a real multi-step flow (search, detail, customize, confirm) with async loading, modals, a cookie interstitial, lots of identically-labeled buttons ("Add to Order" on every card) and look-alike items ("Large Nacho Fries" vs "Diablo Ranch Steak Nacho Fries"), which are the same locator problems a legacy bank UI has. Trade-off: it is a live third-party site, so I only run it manually, at human speed, with no account, and never past the bag (enforced by policy). A local mock app covers everything else offline.

## 2. Artifact schema

Defined in `schema.py` as typed dataclasses, serialized as JSON (`artifacts/taco_bell_order_checkout.json`). Shape:

```
Artifact
  schema_version "2.0"     format version (engine refuses incompatible majors)
  artifact_id, version     capability identity; version bumps when the flow changes
  status                   draft | approved (discovery emits draft; promote.py approves)
  app                      {vendor, product, surface}  (the key for multi-tenant reuse)
  goal_description, target_url, created_at
  provenance               {discovered_by: "llm:claude-sonnet-4-5", discovery_run_id, reviewed_by, notes}
  inputs[]                 {name, type, description, required, default, sensitive}
  outputs[]                {name, type, description, extractor}
  steps[]                  {step_number, action, locator{type, value, button_text},
                            description, input_value, fallback?, on_missing?}
  checkpoint               {locator, description, must_contain[]}
  outcome_signatures[]     {outcome_code, locator, message}
```

**Why it is shaped this way**

- **It is a function signature, not a transcript.** `inputs` and `outputs` are the capability's typed contract. Recorded values are rewritten into `{{placeholders}}` (discovery does this automatically for every declared input, e.g. the typed location becomes `{{location}}` and the side item's locator becomes `{{side_item}}`). Replay validates caller inputs (unknown names, missing required, wrong type) before opening a browser. Validation also rejects an artifact that references an undeclared placeholder or has a `type` step with nothing to type (the exact bug the v1 schema had).
- **Locators are semantic.** `type: role` means accessible name; `type: text` means visible text. `button_text` scopes a generic control ("Add to Order") to the card named by `value`. No CSS selectors, XPaths or coordinates are stored, because those encode layout and break across tenants and versions. The adapter owns the fallback chain for resolving a semantic locator (exact accessible name, then contains, then visible text, then a low-confidence positional fallback allowed only right after typing), and logs which strategy matched on every step.
- **Error semantics live in the artifact, not the engine.** `fallback`, `on_missing` and `outcome_signatures` are declared per capability, so `replay.py` has no app-specific strings in it (v1 hardcoded item names in the engine; v2 removed that).
- **Checkpoint is a real assertion.** The final state must show the checkpoint text AND every `must_contain` item. This catches the case where a click "succeeds" but the item never lands in the bag.
- **Reviewable.** Every step keeps the LLM's one-line reasoning as `description`, and provenance records which model and run produced it and who approved it.

The approved artifact came from the first real discovery run (2026-09-18, `evidence/v1/`). It was migrated to schema 2.0 by `migrate_v1_artifact.py`, which kept the recorded steps and locators unchanged and added the typed parameters and outcome rules.

## 3. Determinism & error handling

**Determinism.** Replay executes the artifact's steps in order with no model in the loop. Same artifact, same inputs, same sequence of actions. Waits are condition-based (network idle plus "no loading indicator visible", capped) rather than fixed sleeps, and element lookups poll until a timeout instead of checking once. (The v1 `find()` passed a timeout to Playwright's `is_visible`, which ignores it, so every check was instantaneous; that was the real cause of a flaky checkpoint after human handoff.)

**Error taxonomy.** Every run ends in exactly one of three statuses, each with a machine-readable `outcome_code`:

| Status | Meaning | Examples |
|---|---|---|
| `success` | Goal reached, checkpoint verified, outputs returned | `completed`, `completed_with_human_intervention` |
| `business_outcome` | A legitimate answer the caller must act on. Not a crash. | `item_unavailable`, `substituted`, `item_sold_out` |
| `failure` | Hard stop with step, expected, observed, screenshot and accessibility tree | `step_failed`, `checkpoint_not_met`, `checkpoint_incomplete`, `policy_blocked`, `invalid_input` |

Recoverable conditions do not change the status; they are handled and reported in `recovered[]` (cookie banner dismissed, a card that rendered slowly and appeared on retry).

**The key distinction: "no such item" vs "broken page".** When a step's target is missing, the engine walks a fixed decision order:

1. A declared `fallback` exists and the caller supplied a value for it: use it, and report `substituted`.
2. A known business-state signature is on screen (e.g. "Sold Out"): report that business outcome.
3. The step has an `on_missing` rule AND its `page_anchor` is visible (e.g. the menu grid's "Add to Order" controls are rendered): the screen is provably healthy, so the item genuinely is not offered. Report `item_unavailable`.
4. Otherwise the page itself is wrong. That is a hard failure, and it is escalated to a human.

Step 3 is the important one. Missing target plus healthy anchor means business outcome; missing target plus missing anchor means failure. Without the anchor check, a broken page would be misreported as "item unavailable", which is the conflation the brief warns about. A test covers exactly that case.

**Evidence.** Each run writes `events.jsonl` (step start, strategy that matched, fallbacks, policy decisions, escalations, final result), `result.json`, and on any failure a screenshot plus accessibility snapshot.

**UI drift (secondary).** Semantic locators absorb cosmetic changes. Real drift shows up as `step_failed` at a specific step with expected vs observed, which is the signal to re-run discovery for that capability and bump its `version`.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is the adapter interface in `playwright_page.py`: `navigate, find, click, type_text, press_enter, settle, get_accessibility_snapshot, screenshot, current_url, extract`, plus human-capture hooks. Discovery and replay only call that interface and only pass semantic locators (role/name/text). To extend:
- **Legacy web** (framesets, nested tables, no test IDs): same Playwright adapter, but resolution walks all frames and falls back to label-proximity ("the input to the right of the cell that says 'Member #'"). The artifact would gain an optional `frame_hint` and a `near` relation on `Locator`. The schema already avoids CSS/XPath, so nothing recorded today would need to change.
- **Desktop**: a new adapter over UI Automation (Windows) or AX (macOS) implementing the same methods. Accessible names and roles exist there too, so artifacts keep the same shape. Screenshot plus coordinates is the last-resort strategy for controls with no accessible name, stored as a low-confidence locator that always requires a checkpoint after it.

**Multi-tenant reuse.** Artifacts are keyed by `app` (vendor + product + version range), not by tenant. Proposed layering:
- A **base artifact** per vendor product, recorded once.
- A small **tenant overlay** that patches only what differs: label renames ("Member #" vs "Account No."), extra confirmation dialogs, tenant-specific `outcome_signatures`, and policy (allowed domains, which actions need approval). Overlays are merged at load time and must pass the same validation.
- Inputs stay the same across tenants, so a calling agent invokes one capability name everywhere.

**Drift detection.** Every replay records which locator strategy matched per step. Tracking that per tenant gives an early-warning signal: a step that moves from "exact accessible name" to "contains" or "positional" is drifting before it fails. Hard failures cluster by (app version, step), which says whether to fix the base artifact or one tenant's overlay.

Not implemented: overlays, the desktop adapter, and drift dashboards. The schema fields they depend on (`app`, semantic locators, per-step strategy logging) are.

## 5. Escalation & handoff

**Detecting "stuck".** Replay escalates when a step's target is missing and none of the fallback / business-outcome rules apply (step 4 of the decision order above). Discovery escalates when an action the LLM chose cannot be executed. Both also escalate for a policy `needs_approval` action.

**Routing.** An intervention record is written to `escalations/<id>.json` with everything an operator needs: capability, step, reason, expected action, URL, screenshot, accessibility tree, and what automation will check when control comes back. The terminal is the mock operator console; the record is the interface a real queue or web console would consume.

**Control transfer model.** `ControlState.controller` is either `automation` or `human`, and every transition is appended to `control_history`. On escalation automation stops issuing commands and flips control to `human`; the engine checks `require_automation()` before every step. The browser is a headed Playwright session, so the operator works in the **same live session** (same cookies, same store, same bag). While the human is in control, a listener injected into the page records what they click and which fields they edit (never the values typed), and page navigations are recorded from the automation side so they survive page unloads.

**Handing back.** The operator returns control from the terminal in one of two modes:
- **resume** (press Enter): "I fixed this one step." Automation takes control back and continues with the next recorded step.
- **finished** (type `done`): "I completed the flow." Automation only verifies.

If a resume did not actually fix things, the next step fails and escalates again with a note saying automation just resumed from a handoff; after three escalations in one run the engine stops with `too_many_escalations` instead of paging the operator forever. Either way the engine does not trust the human's word. The escalation stays open until the final checkpoint is verified by automation, and the record is then marked `resolved` or `unresolved`. The record keeps the human's captured actions, URL before and after, an accessibility-tree diff, and the operator's note.

**What is mocked.** The operator console (terminal instead of a web queue) and remote viewing (the operator sits at the machine running the headed browser). In production the same record would go to a queue, and the operator would attach to the session through a remote browser stream (e.g. CDP screencast); the control-state and resume logic would not change.

## 6. Safety

**Guardrail model** (`policy.py`, configured by `policy.json`, enforced for both discovery and replay):
- **Allowlist.** Only listed domains (`tacobell.com`) and listed action types (`click, type, navigate, wait, press_enter`). Checked before navigation and again after every action, so a redirect off-domain stops the run.
- **Risk classes.** Every action label is classified before it executes:
  - `block`: irreversible or out of scope ("Place Order", "Pay", "Submit Payment", "Add Card"...). Never executed. In discovery the LLM is told it was blocked and must choose another action; in replay the run stops with `policy_blocked`.
  - `needs_approval`: risky but sometimes legitimate ("Remove", "Delete", "Sign In"...). Execution pauses and a human must approve or deny through the same escalation channel.
  - everything else: allowed.
  I chose hard-block for irreversible financial actions (no approval path) because in a bank setting the cost of a wrong approval is much higher than the cost of routing that task to a human-owned workflow.
- **Artifact approval gate.** Discovery only produces `draft` artifacts. `promote.py` marks one `approved` after review, and `replay.py --require-approved` refuses to run anything else, which is how unattended production callers would run.
- **Redaction.** Every log event, escalation record and saved accessibility tree passes through `redact()`: emails, phone numbers, SSN patterns, card or account-like digit runs, bearer tokens and API keys. Inputs declared `sensitive: true` are masked by value everywhere. Typed values are never recorded from humans. Secrets come from the environment and `.env` is gitignored.

**Limits (honest).**
- Label matching is lexical. A dangerous button with an innocent label ("Continue" that actually submits) would pass. The real fix is per-app risk annotations on specific steps and screens, plus checkpoint assertions before and after risky steps.
- Redaction is pattern-based and will miss free-text PII such as names. Production would add field-level classification from the app schema and default-deny persistence of raw page content.
- The LLM still sees full page content during discovery. That is acceptable against a public menu; against real member data, discovery should run only on sandbox tenants or synthetic records.
- Screenshots are saved unredacted (only text artifacts are scrubbed). For regulated data they would need masking or to stay in access-controlled storage.

## 7. Cuts

**Stretch goals attempted**
- **Confidence and approval (approval half).** Discovery only ever saves artifacts as `draft`. `promote.py` marks one `approved` after a human reviews it, and `replay.py --require-approved` refuses to run anything that isn't approved. That is how an unattended production caller would run. I did not build the confidence score half (see multi-run stability below).
- **Canonicalization (parameterization half).** Discovery automatically rewrites the concrete values it used into typed placeholders (for example "San Jose, CA" becomes `{{location}}` and "Large Nacho Fries" becomes `{{side_item}}`), which is what turns one recording into a reusable capability. The cross-tenant half (one base artifact applied to a second variant of the same app) is designed in section 4 but not built.

**Deliberately left out**
- **Operator web console and remote session viewing.** Mocked with the terminal plus headed browser (see section 5). The record format and control-state model are the real parts.
- **Legacy-web and desktop adapters, tenant overlays.** Designed in section 4, not built.
- **Assisted LLM fallback during replay.** Replay is fully deterministic; a failure goes to a human, never back to the model.
- **Robust output extraction.** `order_total` uses a named extractor (regex on "Subtotal"), which is fine for one app but should become a declared extraction rule (locator plus pattern) in the artifact.
- **Multi-run stability scoring.** I cut this on purpose, for three reasons. First, the only live surface is a real third-party site, and running the same order N times in a row means creating N real carts at a real restaurant's stores, which goes against the brief's ask to respect a public site's terms and rate limits. Second, a stability score measured on a live consumer site mixes up two different things: flakiness in my engine, and normal changes on the site (a store closing, a menu item selling out, a promo banner). In the real environment those have different owners, so one number that blends them would be misleading. The right place to measure engine stability is a stable surface, like the included mock app or a vendor sandbox. Third, the brief says to pick at most one or two stretch goals, and I chose the approval gate because it is part of the safety story. The data a stability score needs is already recorded: every replay logs which locator strategy matched each step and how long it took, and I did replay the same artifact more than once with the same result and order total. Scoring is a thin layer on top of that, and it is #2 on my next-steps list.

**Known weak spots**
- Step 3 of the approved artifact (recorded 2026-09-18) targets an icon-only search button that the LLM named just "button". It replays via the positional fallback (the button next to the field just typed into), which is now restricted to the click immediately after typing and logged as low confidence. The fresh discovery run in `evidence/runs/` recorded the same button correctly by its accessible name, "Search", so the fix is to review and promote that newer recording.
- Discovery's goal prompt still contains site hints (which category holds the item, which variant not to pick) that I added after early runs went wrong. A more general agent would get these from a short retry-with-reflection loop instead.
- `outcome_signatures` include one example ("Sold Out") that I could not trigger on the live site; `item_unavailable` via the page-anchor rule is demonstrated end to end.

**What I would build next**
1. A capability catalog: expose approved artifacts as tools (name, description, typed input schema generated from `inputs`) so an agent can discover and invoke them, and return the `ReplayResult` as the tool result.
2. Stability scoring: replay N times, track per-step strategy and latency, and gate `approved` on a stability threshold.
3. Tenant overlays with a second mock "tenant" of the same app (different labels, extra confirm dialog) to prove the base plus overlay model.
4. A real operator queue (escalation records to a queue, a small web page that lists them and links to a live CDP screencast).
