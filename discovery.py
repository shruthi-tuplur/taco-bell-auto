"""
Discovery loop: observe -> decide -> act -> record, repeated until the LLM
says it's done, or we hit max_steps / timeout / a dead end.

This is the ONLY file that imports llm_client. replay.py and
playwright_page.py never touch the LLM.

What makes the output a reusable CAPABILITY and not just a transcript:
  - the caller-facing inputs are declared up front (INPUTS) and every place
    their values show up in the recorded steps is rewritten to {{name}}
  - what was typed is recorded (input_value), so replay can reproduce it
  - steps that target a caller-supplied value get an on_missing rule, so a
    missing target on a healthy screen replays as a business outcome
  - every LLM decision passes the SAME policy gate replay uses
  - the artifact is saved as status="draft"; a human promotes it with
    promote.py before production use

Run:  python discovery.py
      python discovery.py -i side_item="Nacho Fries" -i location="Cupertino, CA"
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import time

from escalation import ControlState, Escalator
from policy import Policy
from run_log import RunLog
from schema import (Artifact, Checkpoint, Fallback, InputParam, Locator, MissingTargetRule,
                    OutcomeSignature, OutputField, Provenance, Step, now_iso)

MODEL = "claude-sonnet-4-5"
TARGET_URL = "https://www.tacobell.com/"
MAX_STEPS = 25
TIMEOUT_SECONDS = 300

# The capability's typed inputs, with the values used for THIS discovery run.
INPUTS = [
    InputParam(name="location", type="string", default="San Jose, CA",
               description="City and state used to find a nearby store"),
    InputParam(name="side_item", type="string", default="Large Nacho Fries",
               description="Exact menu name of the second item to add"),
    InputParam(name="substitute_item", type="string", required=False, default=None,
               description="Optional item to use if side_item is unavailable. If omitted, an unavailable "
                           "side_item is reported as a business outcome instead of substituting."),
]

GOAL_TEMPLATE = (
    "Order one Black Bean Crunchwrap Supreme customized with Fiesta Strips and Seasoned Rice, "
    "and one '{side_item}'. Use the location '{location}' when asked for a city/store. "
    "The Black Bean Crunchwrap Supreme is in the SPECIALTIES menu category, not Veggie Cravings, "
    "so go to Specialties first. "
    "When asked to choose a pickup method, select Drive-Thru if it's available; if only In-Store "
    "pickup is offered, select that instead. When asked when you'd like your order, select 'Now'. "
    "When asked to confirm the store/pickup selection, select 'Finish'. "
    "Reach the cart / order review screen. NEVER enter payment info or complete the purchase. "
    "For the second item, add the item whose name is EXACTLY '{side_item}'. Do NOT pick a different "
    "variant with a similar name (e.g. 'Diablo Ranch Steak Nacho Fries' is a DIFFERENT product from "
    "'Large Nacho Fries'). The exact item may appear further down the page."
)


def run_discovery(overrides: dict | None = None) -> str:
    from llm_client import decide_next_action          # only discovery touches the LLM
    from playwright_page import PlaywrightPage

    values = {p.name: p.default for p in INPUTS}
    values.update(overrides or {})
    goal = GOAL_TEMPLATE.format(**values)

    policy = Policy.load()
    log = RunLog("discovery", "taco_bell_order_checkout")
    page = PlaywrightPage(headless=False)
    control = ControlState()
    escalator = Escalator(page, control, run_log=log)

    log.event("discovery_started", model=MODEL, target_url=TARGET_URL, goal=goal, inputs=values,
              max_steps=MAX_STEPS, timeout_s=TIMEOUT_SECONDS)

    page.navigate(TARGET_URL)
    history: list[str] = []
    recorded: list[Step] = []
    human_steps: list[int] = []
    stop_reason = "max_steps"
    start = time.time()

    for step_number in range(1, MAX_STEPS + 1):
        if time.time() - start > TIMEOUT_SECONDS:
            stop_reason = "timeout"
            break

        tree = page.get_accessibility_snapshot()
        decision = decide_next_action(goal, tree, history)
        action = decision.get("action")
        target = decision.get("target") or {}
        label = " ".join(x for x in [target.get("value", ""), target.get("button_text", "")] if x)
        log.event("llm_decision", step=step_number, action=action, target=target,
                  text=decision.get("text"), reasoning=decision.get("reasoning"),
                  observation_chars=len(tree))

        if action == "done":
            stop_reason = "goal_reported_done"
            break
        if action == "error":
            stop_reason = "unparseable_llm_response"
            break

        # ---- the same policy gate replay uses ---------------------------
        d = policy.check_action(action or "", label)
        if d.verdict == "block":
            log.event("policy_block", step=step_number, action=action, label=label, reason=d.reason)
            history.append(f"{step_number}. BLOCKED BY POLICY: {action} '{label}' ({d.reason}). "
                           f"Choose a different action.")
            continue
        if d.verdict == "needs_approval":
            rec = escalator.escalate(kind="approval", capability="discovery", step_number=step_number,
                                     reason=f"LLM wants a risky action: {d.reason}")
            if rec["status"] != "approved":
                history.append(f"{step_number}. DENIED BY OPERATOR: {action} '{label}'. Choose a different action.")
                continue

        try:
            if action == "wait":
                page.settle()
                recorded.append(Step(step_number=step_number, action="wait", locator=None,
                                     description=decision.get("reasoning", "")))
            elif action == "navigate":
                url = target.get("value", "")
                if not url.startswith("http"):
                    raise ValueError(f"'navigate' needs a full URL, got '{url}'. Use 'click' for links.")
                if policy.check_url(url).verdict != "allow":
                    raise ValueError(f"navigate to '{url}' blocked by domain allowlist")
                page.navigate(url)
                recorded.append(Step(step_number=step_number, action="navigate", locator=None,
                                     description=decision.get("reasoning", "")))
            elif action in ("click", "type"):
                if action == "click":
                    page.click(target)
                else:
                    page.type_text(target, decision.get("text", ""))
                recorded.append(Step(
                    step_number=step_number, action=action,
                    locator=Locator(type=target.get("type", "text"), value=target.get("value", ""),
                                    button_text=target.get("button_text")),
                    description=decision.get("reasoning", ""),
                    # v1 bug: what was typed was never recorded, so replay typed "".
                    input_value=decision.get("text") if action == "type" else None,
                ))
            else:
                raise ValueError(f"unsupported action '{action}'")

            url_ok = policy.check_url(page.current_url())
            if url_ok.verdict != "allow":
                stop_reason = "left_allowlisted_domain"
                log.event("policy_block", step=step_number, url=page.current_url(), reason=url_ok.reason)
                break
            log.event("action_ok", step=step_number, strategy=page.last_action())
            history.append(f"{step_number}. {action}: {label} -> {decision.get('reasoning')}")

        except Exception as e:
            # Stuck: hand the SAME live browser to a human, then let the LLM continue from there.
            log.event("action_failed", step=step_number, error=str(e)[:300])
            rec = escalator.escalate(kind="stuck", capability="discovery", step_number=step_number,
                                     reason=f"Discovery could not {action} '{label}': {e}",
                                     expected=f"{action} '{label}'")
            human_steps.append(step_number)
            history.append(f"{step_number}. {action} '{label}' FAILED. A human operator took over and "
                           f"handed back (note: {rec.get('operator_note') or 'none'}). Re-read the page.")

    log.event("discovery_finished", stop_reason=stop_reason, steps_recorded=len(recorded),
              elapsed_s=round(time.time() - start, 1), human_intervened_at=human_steps)

    artifact = build_artifact(goal, recorded, values, log.run_id, human_steps, stop_reason)
    out = log.write_json("discovered_artifact.json", artifact.to_dict())
    log.close()
    print(f"\nDiscovery stop reason: {stop_reason}")
    print(f"Draft artifact saved to: {out}")
    print("Review it, then promote it with: python promote.py " + os.path.relpath(out))
    page.close()
    return out


def build_artifact(goal: str, steps: list[Step], values: dict, run_id: str,
                   human_steps: list[int], stop_reason: str) -> Artifact:
    steps = parameterize(steps, values)
    steps = add_missing_target_rules(steps)
    notes = f"stop_reason={stop_reason}"
    if human_steps:
        notes += f"; a human intervened at discovery step(s) {human_steps}, so the recording may be incomplete"
    return Artifact(
        artifact_id="taco_bell_order_checkout",
        version="2.0.0",
        goal_description=GOAL_TEMPLATE.replace("'{side_item}'", "{{side_item}}").replace("'{location}'",
                                                                                          "{{location}}"),
        target_url=TARGET_URL,
        created_at=now_iso(),
        inputs=INPUTS,
        outputs=[OutputField(name="order_total", type="string", extractor="subtotal",
                             description="Subtotal shown in the bag/review screen, e.g. '$11.47'")],
        steps=steps,
        checkpoint=Checkpoint(
            locator=Locator(type="text", value="My Bag"),
            description="The bag / order review panel is open",
            must_contain=["Black Bean Crunchwrap Supreme", "{{side_item}}"],
        ),
        status="draft",
        app={"vendor": "Taco Bell", "product": "tacobell.com online ordering", "surface": "web"},
        outcome_signatures=[
            OutcomeSignature(outcome_code="item_sold_out", locator=Locator(type="text", value="Sold Out"),
                             message="The requested item is sold out at this store."),
        ],
        provenance=Provenance(discovered_by=f"llm:{MODEL}", discovery_run_id=run_id, notes=notes),
    )


def parameterize(steps: list[Step], values: dict) -> list[Step]:
    """Rewrite concrete input values in recorded steps into {{name}}
    placeholders. Longest values first so 'Large Nacho Fries' wins over
    any shorter overlapping value."""
    pairs = sorted(((k, str(v)) for k, v in values.items() if v), key=lambda kv: -len(kv[1]))

    def sub(s):
        if not s:
            return s
        for name, val in pairs:
            if val.lower() in s.lower():
                i = s.lower().index(val.lower())
                s = s[:i] + "{{" + name + "}}" + s[i + len(val):]
        return s

    out = []
    for st in steps:
        st = dataclasses.replace(st)
        if st.locator:
            st.locator = dataclasses.replace(st.locator, value=sub(st.locator.value))
        st.input_value = sub(st.input_value)
        out.append(st)
    return out


def add_missing_target_rules(steps: list[Step]) -> list[Step]:
    """A click whose target is a caller-supplied value (e.g. {{side_item}})
    might legitimately not exist. If so, and the screen is otherwise healthy
    (the generic control, e.g. 'Add to Order', is visible), that is a
    business outcome, not a crash. Also wire the optional substitute."""
    out = []
    for st in steps:
        if st.action == "click" and st.locator and "{{side_item}}" in st.locator.value:
            anchor = Locator(type="text", value=st.locator.button_text or "Add to Order")
            st = dataclasses.replace(
                st,
                on_missing=MissingTargetRule(outcome_code="item_unavailable", page_anchor=anchor,
                                             message="'{{side_item}}' is not available on this store's menu."),
                fallback=Fallback(
                    locator=Locator(type=st.locator.type, value="{{substitute_item}}",
                                    button_text=st.locator.button_text),
                    condition="side_item unavailable and caller supplied substitute_item"),
            )
        out.append(st)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="LLM-driven discovery run (records a draft artifact).")
    ap.add_argument("--input", "-i", action="append", default=[], metavar="NAME=VALUE")
    args = ap.parse_args()
    overrides = dict(kv.split("=", 1) for kv in args.input)
    run_discovery(overrides)
