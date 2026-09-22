"""
Replay engine — reads a saved artifact and executes its steps IN ORDER,
with ZERO LLM calls. This is the "production" path an AI agent would
trigger to actually invoke a saved capability.

Notice: this file does not import any LLM client, anywhere. That's not an
accident — it's the proof that replay is deterministic. A human reviewer
can see that fact just by looking at the imports at the top of this file.
"""

import json
from dataclasses import dataclass
from typing import Literal, Optional


# ---------------------------------------------------------------------------
# The three-way result contract (from the decision log):
#   success          -> checkpoint confirmed, happy path
#   business_outcome -> flow completed but landed somewhere unexpected
#                        (e.g. a fallback/substitution was used) - not a crash
#   failure           -> couldn't find an expected element, hard error
# ---------------------------------------------------------------------------
@dataclass
class ReplayResult:
    status: Literal["success", "business_outcome", "failure"]
    message: str
    outputs: dict
    failed_at_step: Optional[int] = None


class ReplayEngine:
    """
    Wraps a "page" object (in production: a real Playwright page;
    here: a mock page for demonstration) and walks it through an artifact.
    """

    def __init__(self, page):
        self.page = page  # anything with a .find(locator) and .click(locator) method

    def run(self, artifact: dict, inputs: dict) -> ReplayResult:
        substitution_used = False

        for step in artifact["steps"]:
            step_num = step["step_number"]
            action = step["action"]
            locator = step["locator"]

            if action == "navigate":
                self.page.navigate(artifact["target_url"])
                continue

            if action == "click":
                found = self.page.find(locator)

                if found:
                    self.page.click(locator)
                    continue

                # Primary locator not found. Do we have a fallback for this?
                fallback = step.get("fallback")
                if fallback is not None:
                    fallback_found = self.page.find(fallback["locator"])
                    if fallback_found:
                        self.page.click(fallback["locator"])
                        substitution_used = True
                        continue
                    else:
                        # Even the fallback isn't there. This is beyond a
                        # single-item substitution -> escalate to a human
                        # rather than guess further. (See escalation design.)
                        return ReplayResult(
                            status="failure",
                            message=(
                                f"Step {step_num}: neither '{locator['value']}' nor its "
                                f"fallback '{fallback['locator']['value']}' could be found. "
                                f"Escalating to human operator."
                            ),
                            outputs={},
                            failed_at_step=step_num,
                        )

                # No locator found, and no fallback exists for this step.
                # This is a genuine "couldn't complete" business outcome for
                # a single-item case, or a hard failure -- for a step with
                # no fallback defined, we treat it as a hard failure since
                # nothing in the artifact tells us how to proceed.
                return ReplayResult(
                    status="failure",
                    message=f"Step {step_num}: could not find element '{locator['value']}', no fallback defined.",
                    outputs={},
                    failed_at_step=step_num,
                )

        # All steps executed. Check the checkpoint before declaring success.
        checkpoint = artifact["checkpoint"]
        if self.page.find(checkpoint["locator"]):
            outputs = {
                "order_total": self.page.get_order_total(),
                "substitution_used": substitution_used,
            }
            if substitution_used:
                return ReplayResult(
                    status="business_outcome",
                    message="Order completed, but a fallback substitution was used.",
                    outputs=outputs,
                )
            return ReplayResult(
                status="success",
                message="Order completed and checkout review screen confirmed.",
                outputs=outputs,
            )
        else:
            return ReplayResult(
                status="failure",
                message="All steps executed, but checkpoint was not found. Unexpected end state.",
                outputs={},
            )


def load_artifact(path: str) -> dict:
    with open(path) as f:
        return json.load(f)
