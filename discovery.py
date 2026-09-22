"""
Discovery loop: observe -> decide -> act -> record, repeated until the LLM
says it's done, or we hit max_steps / timeout.

This is the ONLY file that should ever import llm_client -- replay.py and
playwright_page.py never touch the LLM.
"""

import time
import json
from playwright_page import PlaywrightPage
from llm_client import decide_next_action
from schema import Artifact, Step, Locator, Checkpoint, InputParam, OutputField, now_iso

GOAL = (
    "Order one Black Bean Crunchwrap Supreme customized with Fiesta Strips "
    "and Seasoned Rice, and one Nacho Fries (substitute if unavailable). "
    "The Black Bean Crunchwrap Supreme is located in the SPECIALTIES menu "
    "category, not Veggie Cravings -- go to Specialties first. "
    "When asked to choose a pickup method, select Drive-Thru if it's available; "
    "if only In-Store pickup is offered, select that instead. "
    "When asked when you'd like your order, select 'Now'. "
    "When asked to confirm the store/pickup selection, select 'Finish'. "
    "Reach the checkout / order review screen. NEVER enter payment info "
    "or complete the purchase."
    "For the second item, add the PLAIN 'Large Nacho Fries' to the order — this is the "
"basic/regular Nacho Fries item "
"Do NOT select 'Diablo Ranch Steak Nacho Fries'" 
"or any other Nacho Fries variant — those are DIFFERENT products. The plain"
"'Nacho Fries' item may appear further down the page, below the Diablo Ranch"
"Steak version. When you see multiple items containing the words 'Nacho Fries'"
"pick the one whose name is EXACTLY 'Large Nacho Fries' and nothing else."
)
TARGET_URL = "https://www.tacobell.com/"
MAX_STEPS = 20
TIMEOUT_SECONDS = 120


def run_discovery():
    page = PlaywrightPage(headless=False)
    page.navigate(TARGET_URL)

    steps_so_far = []
    recorded_steps = []
    start_time = time.time()
    step_number = 1

    while step_number <= MAX_STEPS:
        if time.time() - start_time > TIMEOUT_SECONDS:
            print("TIMEOUT reached. Stopping discovery.")
            break

        tree = page.get_accessibility_snapshot()
        decision = decide_next_action(GOAL, tree, steps_so_far)

        print(f"\n--- Step {step_number} ---")
        print(f"LLM decided: {decision}")

        action = decision.get("action")

        if action == "done":
            print("LLM believes goal is complete.")
            break

        if action == "error":
            print(f"LLM response could not be parsed: {decision.get('reasoning')}")
            break

        if action == "wait":
            page.page.wait_for_timeout(2000)
            recorded_steps.append(
                Step(step_number=step_number, action="wait", locator=None,
                     description=decision.get("reasoning", ""))
            )
            steps_so_far.append(f"{step_number}. wait -> {decision.get('reasoning')}")
            step_number += 1
            continue

        if action in ("click", "type", "navigate"):
            target = decision.get("target", {})
            try:
                if action == "click":
                    page.click(target)
                elif action == "navigate":
                    nav_value = target.get("value", "")
                    if not nav_value.startswith("http"):
                        raise Exception(f"'navigate' requires a full URL, got '{nav_value}' -- use 'click' for links instead")
                    page.navigate(nav_value)
                elif action == "type":
                    page.type_text(target, decision.get("text", ""))

                recorded_steps.append(
                    Step(
                        step_number=step_number,
                        action=action,
                        locator=Locator(type=target.get("type", "text"), value=target.get("value", "")),
                        description=decision.get("reasoning", ""),
                    )
                )
                steps_so_far.append(f"{step_number}. {action}: {target.get('value')} -> {decision.get('reasoning')}")

            except Exception as e:
                print(f"Action failed: {e}")
                tree = page.get_accessibility_snapshot()
                with open("failure_tree.txt", "w") as f:
                    f.write(tree)
                print("Full accessibility tree at failure saved to failure_tree.txt (only pasting it here if you need me to see it)")
                break

        step_number += 1

    print("\n--- Discovery finished ---")
    print(f"Total steps recorded: {len(recorded_steps)}")

    # Build the artifact from what actually happened
    artifact = Artifact(
        artifact_id="taco_bell_order_checkout_v1",
        version="1.0.0",
        goal_description=GOAL,
        target_url=TARGET_URL,
        created_at=now_iso(),
        inputs=[InputParam(name="crunchwrap_modifications", type="string",
                            description="Modifications to apply", required=True)],
        outputs=[OutputField(name="order_total", type="string", description="Order subtotal"),
                 OutputField(name="substitution_used", type="boolean", description="Whether a fallback fired")],
        steps=recorded_steps,
        checkpoint=Checkpoint(
            locator=Locator(type="text", value="Order Review"),
            description="Checkout review screen confirms items were added",
        ),
    )

    with open("discovered_artifact.json", "w") as f:
        json.dump(artifact.to_dict(), f, indent=2)
    print("Saved to ../evidence/discovered_artifact.json")

    page.close()


if __name__ == "__main__":
    run_discovery()