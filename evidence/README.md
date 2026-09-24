# Evidence index

## `runs/` (schema 2.0 engine)
One folder per run, named `<timestamp>_<discovery|replay>_<capability>`. Each contains:
- `events.jsonl`: structured, redacted log of every step (what was tried, which locator strategy matched, why the LLM chose it for discovery runs, policy decisions, escalations)
- `result.json`: the final structured replay result (replay runs)
- `discovered_artifact.json`: the draft artifact (discovery runs)
- `failure_step_*.png` / `*_tree.txt`, `escalation_step_*`: screenshot + accessibility snapshot captured on failure or escalation

Runs included for the submission:
| Run | Command | Shows |
|---|---|---|
| discovery | `python discovery.py` | A real LLM-driven run against the live site |
| replay: success | `python replay.py` | Deterministic replay, checkpoint verified, `order_total` returned |
| replay: business outcome | `python replay.py -i side_item="McNuggets"` | Item not on the menu reported as `item_unavailable`, not a crash |
| replay: substitution | `python replay.py -i side_item="McNuggets" -i substitute_item="Cheesy Roll Up"` | Caller-allowed substitute, `substituted` |
| replay: failure + human | `python replay.py --inject-failure 9` | Injected broken control, escalation, human fixes it in the same session, automation resumes, `completed_with_human_intervention` |

Matching intervention records are in `/escalations/`.

## `v1/` (first iteration, schema 1.0)
Earlier evidence from before the v2 engine, kept for history:
- `discovered_artifact.json`: the original real LLM discovery output (2026-09-18). The approved artifact in `/artifacts/` is this recording migrated to schema 2.0 (`migrate_v1_artifact.py`).
- `replay_run_*.log`: v1 replays (success, substitution, hard failure, human escalation), with the hand-edited `_BROKEN_demo` / `_sub` artifacts used to inject them.
- `escalations/`: v1 intervention records.
