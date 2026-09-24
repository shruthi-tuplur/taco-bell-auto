# Computer-Use Automation System (interface.ai take-home, Assignment A)

An LLM discovers how to do a task in a real web app once. The run is saved as a typed, versioned **capability artifact**. A **deterministic replay engine** then runs that capability with caller-supplied inputs and zero LLM calls, returns a structured result (success, business outcome, or failure), and hands the **same live browser session** to a human when it gets stuck.

Proxy target: the public **tacobell.com** ordering flow (find a store, pick pickup options, customize a Black Bean Crunchwrap Supreme, add a side item, open the bag). Automation **never** enters payment details or places an order; that is enforced in code by the policy layer, not just by the prompt. See `REPORT.md` for the design write-up.

## Repo map

| Path | What it is |
|---|---|
| `discovery.py` | LLM-driven observe, decide, act loop. The only file that imports the LLM client. Writes a **draft** artifact. |
| `llm_client.py` | Anthropic API call + JSON action parsing (Claude Sonnet 4.5). |
| `replay.py` | Deterministic replay engine and CLI. No LLM import anywhere. |
| `schema.py` | The artifact schema (typed dataclasses, validation, `{{param}}` templating). |
| `policy.py`, `policy.json` | Guardrails: domain + action allowlist, blocked vs needs-approval actions, redaction. |
| `escalation.py` | Human-in-the-loop handoff: control state, intervention records, capture of human actions. |
| `run_log.py` | Structured, redacted per-run logs (`evidence/runs/<run_id>/events.jsonl`). |
| `playwright_page.py` | Surface adapter (Playwright + accessibility tree). |
| `promote.py` | Promote a reviewed draft artifact to `approved`. |
| `artifacts/taco_bell_order_checkout.json` | The approved capability artifact used by replay. |
| `mock_app/` | Tiny local app + artifact used by the offline browser smoke test. |
| `tests/` | Unit tests (fake surface) + real-browser smoke tests (local mock app). |
| `evidence/` | Discovery and replay evidence. Start at `evidence/README.md`. |
| `escalations/` | Intervention records written by live runs. |

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

Discovery needs an Anthropic API key. Replay and the tests do **not**.

```bash
cp .env.example .env                 # then paste your key into .env
# or: export ANTHROPIC_API_KEY=sk-ant-...
```

`.env` is gitignored. The key is never logged (the redactor also scrubs anything shaped like one).

## Run without live services

```bash
python -m pytest -q
```

25 tests, about 25 seconds, no network, no API key:
- `tests/test_replay.py` drives the real engine and the real saved artifact against a scripted fake surface: success, input substitution, item unavailable, substitution, broken page vs business outcome, injected failure, human resume, human "done" that fails re-verification, silent missing item, blocked irreversible action, denied risky action, off-allowlist navigation, redaction, draft refusal, input and artifact validation.
- `tests/test_browser_smoke.py` runs the real Playwright adapter, headless, against `mock_app/index.html`: success, item unavailable, substitution, hard failure, and a human handoff in the same live session.

## Demo path (live site)

Run these from the repo root. A visible Chromium window opens for each run. Each run writes its own evidence folder under `evidence/runs/`.

**1. Discovery (LLM run, records a draft artifact)**
```bash
python discovery.py
```
The LLM drives the site until it reaches the bag. Output: `evidence/runs/<timestamp>_discovery_.../events.jsonl` (every LLM decision with its reasoning) and `discovered_artifact.json` (status `draft`). To make a draft the production artifact after reviewing it:
```bash
python promote.py evidence/runs/<run_id>/discovered_artifact.json --reviewer "Your Name"
```

**2. Replay, happy path (no LLM)**
```bash
python replay.py
```
Expected: `STATUS: success`, `OUTCOME CODE: completed`, `OUTPUTS: {'order_total': '$...'}`.

**3. Replay with different inputs** (same artifact, new parameters)
```bash
python replay.py -i location="Cupertino, CA" -i side_item="Nacho Fries"
```

**4. Replay hitting a business outcome** (the item does not exist on this store's menu)
```bash
python replay.py -i side_item="McNuggets"
```
Expected: `STATUS: business_outcome`, `OUTCOME CODE: item_unavailable`. The engine confirms the menu page itself is healthy before calling it "unavailable" rather than a failure.

**5. Same, but the caller allows a substitute**
```bash
python replay.py -i side_item="McNuggets" -i substitute_item="Cheesy Roll Up"
```
Expected: `STATUS: business_outcome`, `OUTCOME CODE: substituted`, with the order total.

**6. Replay hitting a hard failure, escalated to a human**
```bash
python replay.py --inject-failure 9
```
Step 9 (the "Specialties" tab) is deliberately broken. The engine escalates: the terminal shows the intervention request, and control passes to you in the same browser window. Click **Specialties** in the browser, then press **Enter** in the terminal and type a short note. Automation takes control back, continues from step 10, and verifies the checkpoint. Expected: `STATUS: success`, `OUTCOME CODE: completed_with_human_intervention`, and a resolved record in `escalations/`.

Add `--no-escalate` to see the same failure reported as a hard failure without a human.

**7. Guardrail demo**
```bash
python policy.py
```

## Result contract (what a calling agent gets back)

```json
{
  "status": "success | business_outcome | failure",
  "outcome_code": "completed | completed_with_human_intervention | item_unavailable | substituted | item_sold_out | step_failed | checkpoint_not_met | checkpoint_incomplete | policy_blocked | action_denied | invalid_input | invalid_artifact | artifact_not_approved | engine_error",
  "message": "human-readable explanation",
  "outputs": {"order_total": "$11.47"},
  "failed_at_step": 15, "expected": "...", "observed": "...",
  "recovered": [{"kind": "dismissed_interstitial", "detail": "cookie consent banner"}],
  "substitutions": [], "human_intervened": false, "interventions": [],
  "artifact": "taco_bell_order_checkout@2.0.0", "run_id": "...", "evidence_dir": "..."
}
```

## Notes on the target site

tacobell.com is a real public site, used only as a stand-in for a bank back-office app. Runs are manual and one at a time at human speed, use no account or credentials, and never submit an order or payment (blocked by `policy.json`). A local mock app is included so everything except the live demo runs offline.
