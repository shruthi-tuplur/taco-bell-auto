"""
Replay engine — reads a saved artifact and executes its steps IN ORDER,
with ZERO LLM calls. This is the "production" path an AI agent would
trigger to actually invoke a saved capability.

Notice: this file does not import any LLM client, anywhere. That's not an
accident — it's the proof that replay is deterministic. A human reviewer
can see that fact just by looking at the imports at the top of this file.
"""

import json
import sys
from dataclasses import dataclass
from typing import Literal, Optional

from playwright_page import PlaywrightPage  # our real browser wrapper


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
    Wraps a "page" object (in production: a real Playwright page,
    via PlaywrightPage) and walks it through an artifact, with zero
    LLM calls at replay time.
    """

    def __init__(self, page):
        self.page = page

    def _capture_failure_evidence(self, step_num):
        """On any failure, grab a screenshot + accessibility snapshot so
        there's rich evidence of what the page actually looked like,
        instead of just a text error message."""
        try:
            self.page.screenshot(path=f"evidence/failure_step_{step_num}.png")
        except Exception:
            pass
        try:
            tree = self.page.get_accessibility_snapshot()
            with open(f"evidence/failure_step_{step_num}_tree.txt", "w") as f:
                f.write(tree)
        except Exception:
            pass

    def run(self, artifact: dict, inputs: dict) -> ReplayResult:
        substitution_used = False

        # Discovery navigates to the target URL as setup code before the
        # LLM loop even starts, so it's never recorded as a step in the
        # artifact. Replay has to do the same thing explicitly here,
        # rather than waiting to encounter a "navigate" action in the loop.
        self.page.navigate(artifact["target_url"])

        for step in artifact["steps"]:
            step_num = step["step_number"]
            action = step["action"]
            locator = step.get("locator")

            # --- navigate ---
            # (kept as a no-op safety net in case a navigate action ever
            # does show up mid-artifact, but this shouldn't normally fire)
            if action == "navigate":
                self.page.navigate(artifact["target_url"])
                continue

            # --- type ---
            if action == "type":
                text_to_type = step.get("text", "")
                try:
                    self.page.type_text(locator, text_to_type)
                    continue
                except Exception as e:
                    self._capture_failure_evidence(step_num)
                    return ReplayResult(
                        status="failure",
                        message=f"Step {step_num}: could not type into '{locator['value']}' ({e}).",
                        outputs={},
                        failed_at_step=step_num,
                    )

            # --- press_enter ---
            if action == "press_enter":
                self.page.press_enter()
                continue

            # --- wait ---
            if action == "wait":
                self.page.wait(seconds=2)
                continue

            # --- done (recorded goal-completion marker, not a real browser action) ---
            if action == "done":
                continue

            # --- click ---
            if action == "click":
                try:
                    self.page.click(locator)
                    continue
                except Exception:
                    fallback = step.get("fallback")
                    if fallback is not None:
                        try:
                            self.page.click(fallback["locator"])
                            substitution_used = True
                            continue
                        except Exception:
                            self._capture_failure_evidence(step_num)
                            return ReplayResult(
                                status="failure",
                                message=(
                                    f"Step {step_num}: neither '{locator['value']}' nor its "
                                    f"fallback '{fallback['locator']['value']}' could be clicked. "
                                    f"Escalating to human operator."
                                ),
                                outputs={},
                                failed_at_step=step_num,
                            )

                    self._capture_failure_evidence(step_num)
                    return ReplayResult(
                        status="failure",
                        message=f"Step {step_num}: could not click element '{locator['value']}', no fallback defined.",
                        outputs={},
                        failed_at_step=step_num,
                    )

            # --- unrecognized action ---
            self._capture_failure_evidence(step_num)
            return ReplayResult(
                status="failure",
                message=f"Step {step_num}: unrecognized action '{action}' — cannot replay deterministically.",
                outputs={},
                failed_at_step=step_num,
            )

        # All steps executed. Check the checkpoint before declaring success.
                # All steps executed. Check the checkpoint before declaring success.
        checkpoint = artifact["checkpoint"]
        if self.page.find(checkpoint["locator"]):
            # A generic checkpoint (like "the cart page loaded") can pass
            # even if an earlier step silently failed to add an item
            # without raising an exception. So beyond the checkpoint text,
            # explicitly verify every expected item actually made it into
            # the cart before calling this a real success.
            expected_items = artifact.get("expected_items", [
                "Black Bean Crunchwrap Supreme",
                "Large Nacho Fries",
            ])
            missing_items = [
                name for name in expected_items
                if not self.page.find({"type": "text", "value": name})
            ]

            if missing_items:
                self._capture_failure_evidence("checkpoint")
                return ReplayResult(
                    status="failure",
                    message=(
                        f"Checkpoint page was reached, but the cart is missing "
                        f"expected item(s): {', '.join(missing_items)}. A step "
                        f"earlier in the flow silently failed to add an item "
                        f"without raising an error."
                    ),
                    outputs={},
                )

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
                message="Order completed and checkout review screen confirmed, both items verified present.",
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


if __name__ == "__main__":
    artifact_path = sys.argv[1] if len(sys.argv) > 1 else "evidence/discovered_artifact.json"
    artifact = load_artifact(artifact_path)

    page = PlaywrightPage()
    engine = ReplayEngine(page)

    print(f"--- Replaying artifact: {artifact_path} ---")
    result = engine.run(artifact, inputs={})

    print(f"\nStatus: {result.status}")
    print(f"Message: {result.message}")
    print(f"Outputs: {result.outputs}")
    if result.failed_at_step is not None:
        print(f"Failed at step: {result.failed_at_step}")

    page.close(pause_before_close=True)