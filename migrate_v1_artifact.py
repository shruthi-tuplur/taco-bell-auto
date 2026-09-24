"""
One-off: upgrade the v1 artifact from the first real discovery run
(evidence/v1/discovered_artifact.json, recorded 2026-09-18 by Claude
Sonnet 4.5 against tacobell.com) to schema 2.0.

What changes:
  - the typed location is restored as input_value (v1 never recorded it)
  - concrete values become {{params}} (location, side_item)
  - the side-item step gets an on_missing rule and an optional substitute
  - checkpoint gains must_contain, outputs gain a named extractor
The recorded steps and locators themselves are NOT changed.

Run:  python migrate_v1_artifact.py
"""

import json
import os

from discovery import INPUTS, add_missing_target_rules, build_artifact, parameterize
from schema import Locator, Step

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "evidence", "v1", "discovered_artifact.json")
DEST = os.path.join(HERE, "artifacts", "taco_bell_order_checkout.json")


def main():
    with open(SRC) as f:
        v1 = json.load(f)

    steps = []
    for s in v1["steps"]:
        loc = s.get("locator")
        steps.append(Step(
            step_number=s["step_number"], action=s["action"],
            locator=Locator(type=loc["type"], value=loc["value"], button_text=loc.get("button_text")) if loc else None,
            description=s.get("description", ""),
            # The v1 recording dropped the typed text. The LLM's reasoning for
            # step 3 shows it searched "San Jose, CA", the run's location input.
            input_value="San Jose, CA" if s["action"] == "type" else None,
        ))

    values = {p.name: p.default for p in INPUTS}
    art = build_artifact(goal="", steps=steps, values=values, run_id="v1_2026-09-18T20:09:38Z",
                         human_steps=[], stop_reason="goal_reported_done")
    art.created_at = v1["created_at"]
    art.status = "approved"
    art.provenance.reviewed_by = "Shruthi Tuplur"
    art.provenance.notes = ("Recorded by a real LLM discovery run on 2026-09-18 (schema 1.0), replayed "
                            "successfully several times, then migrated to schema 2.0 by migrate_v1_artifact.py. "
                            "Steps and locators unchanged.")
    os.makedirs(os.path.dirname(DEST), exist_ok=True)
    with open(DEST, "w") as f:
        json.dump(art.to_dict(), f, indent=2)
    print("Wrote", DEST)


if __name__ == "__main__":
    main()
