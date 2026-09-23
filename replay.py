"""
Replay engine — reads a saved artifact and executes its steps IN ORDER,
with ZERO LLM calls. This is the "production" path an AI agent would
trigger to actually invoke a saved capability.

Notice: this file does not import any LLM client, anywhere. That's not an
accident — it's the proof that replay is deterministic. A human reviewer
can see that fact just by looking at the imports at the top of this file.
"""

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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

    def _find_with_retry(self, locator, attempts=5, delay_seconds=1.5):
        """Poll for a locator instead of checking once and giving up.
        A human just clicked through a live page, so the DOM needs a beat
        to settle (page loads, re-renders, animations) before a single
        immediate find() can be trusted as a real miss."""
        for attempt in range(1, attempts + 1):
            if self.page.find(locator):
                return True
            if attempt < attempts:
                self.page.wait(seconds=delay_seconds)
        return False

    def _escalate_to_human(self, step_num, message, checkpoint_locator=None):
        """
        Human-in-the-loop escalation. The browser is real and visible
        (headless=False), so the human takes over the SAME live session
        directly -- no separate process or reattachment needed. We still
        write a structured intervention record for evidence purposes.
        """
        os.makedirs("escalations", exist_ok=True)
        request_id = f"intervention_{int(time.time())}"
        record = {
            "id": request_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "step_number": step_num,
            "message": message,
            "screenshot": f"evidence/failure_step_{step_num}.png",
            "accessibility_tree": f"evidence/failure_step_{step_num}_tree.txt",
            "checkpoint_locator": checkpoint_locator,
            "status": "pending",
        }
        path = os.path.join("escalations", f"{request_id}.json")
        with open(path, "w") as f:
            json.dump(record, f, indent=2)

        print("\n" + "=" * 60)
        print("ESCALATION: human intervention needed")
        print(f"Step {step_num} failed: {message}")
        print(f"Intervention record: {path}")
        print("The browser window is open on your screen right now.")
        print("Take over there and manually fix or complete the order.")
        print(
            "\nWhen you're done, the script will look for this checkpoint "
            "to confirm the fix worked:"
        )
        print(f"  {record.get('checkpoint_locator', '(checkpoint printed by caller)')}")
        print("=" * 60)
        input("\nPress ENTER here once you're done and want to hand control back to the automation...\n")

        record["status"] = "resolved"
        record["resolved_at"] = datetime.now(timezone.utc).isoformat()
        with open(path, "w") as f:
            json.dump(record, f, indent=2)

        return path

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
                            pass  # fall through to escalation below

                    # Either there was no fallback, or the fallback also
                    # failed. Rather than giving up immediately, escalate
                    # to a human before declaring a hard failure.
                    self._capture_failure_evidence(step_num)
                    failure_message = f"Step {step_num}: could not click element '{locator['value']}', no fallback defined."
                    checkpoint_locator = artifact["checkpoint"]["locator"]
                    self._escalate_to_human(step_num, failure_message, checkpoint_locator)

                    if self._find_with_retry(checkpoint_locator):
                        return ReplayResult(
                            status="business_outcome",
                            message=f"Step {step_num} required human intervention; operator resolved it manually and the checkpoint was reached.",
                            outputs={
                                "order_total": self.page.get_order_total(),
                                "substitution_used": True,
                            },
                        )

                    return ReplayResult(
                        status="failure",
                        message=failure_message + " Escalated to human, but the issue was not resolved.",
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
        checkpoint = artifact["checkpoint"]
        if self.page.find(checkpoint["locator"]):
            # A generic checkpoint (like "the cart page loaded") can pass
            # even if an earlier step silently failed to add an item
            # without raising an exception. So beyond the checkpoint text,
            # explicitly verify every expected item actually made it into
            # the cart before calling this a real success.
            crunchwrap_ok = self.page.find({"type": "text", "value": "Black Bean Crunchwrap Supreme"})

            second_item_names = ["Large Nacho Fries"]
            for step in artifact["steps"]:
                fb = step.get("fallback")
                if fb:
                    second_item_names.append(fb["locator"]["value"])
            second_item_ok = any(
                self.page.find({"type": "text", "value": name})
                for name in second_item_names
            )

            missing_items = []
            if not crunchwrap_ok:
                missing_items.append("Black Bean Crunchwrap Supreme")
            if not second_item_ok:
                missing_items.append(f"one of {second_item_names}")

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