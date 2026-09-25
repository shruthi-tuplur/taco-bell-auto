"""
Replay engine tests. They drive the REAL ReplayEngine and the REAL saved
artifact with a scripted fake surface, so they run offline in about a
second, with no browser, no network, and no LLM.

Run:  python -m pytest -q        (or: python -m unittest discover -s tests)
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from escalation import OperatorResponse  # noqa: E402
from policy import Policy  # noqa: E402
from replay import ReplayEngine, load_artifact  # noqa: E402

ARTIFACT = load_artifact(os.path.join(ROOT, "artifacts", "taco_bell_order_checkout.json"))
MENU = {"Start your order", "Please enter City and State", "button", "Start Your Order", "Drive-Thru",
        "Finish", "Specialties", "Black Bean Crunchwrap Supreme®", "Fiesta Strips", "Seasoned Rice",
        "Add to Order", "Close cart", "Your Order", "Large Nacho Fries", "Cheesy Roll Up"}


class FakePage:
    """A scripted stand-in for PlaywrightPage with the same interface."""

    def __init__(self, clickable=MENU, broken=()):
        self.clickable = set(clickable) - set(broken)
        self.visible = {"Add to Order", "Specialties"}
        self.cart = []
        self.url = "https://www.tacobell.com/"
        self.recovered_events = []
        self.actions_log = []
        self.clicks = []
        self.typed = []
        self.capturing = False

    # --- interface used by the engine ---
    def navigate(self, url):
        self.url = url
        self.recovered_events.append({"kind": "dismissed_interstitial", "detail": "cookie consent banner"})

    def click(self, loc):
        v = loc["value"]
        if v not in self.clickable:
            raise Exception(f"No visible match for '{v}'")
        self.clicks.append(v)
        self.actions_log.append(f"CLICK '{v}'")
        if v == "Black Bean Crunchwrap Supreme®":
            self.cart.append("Black Bean Crunchwrap Supreme")
        elif loc.get("button_text") == "Add to Order":
            self.cart.append(v)
        elif v == "Your Order":
            self.visible |= {"My Bag", "Subtotal $11.47", *self.cart}

    def type_text(self, loc, text):
        self.typed.append((loc["value"], text))

    def find(self, loc, timeout_ms=0):
        v = loc["value"] if isinstance(loc, dict) else loc.value
        return any(v in t for t in self.visible)

    def settle(self, max_seconds=8):
        pass

    def press_enter(self):
        pass

    def current_url(self):
        return self.url

    def last_action(self):
        return self.actions_log[-1] if self.actions_log else ""

    def extract(self, name):
        return "$11.47" if name == "subtotal" else None

    def screenshot(self, path):
        with open(path, "wb") as f:
            f.write(b"fakepng")

    def get_accessibility_snapshot(self):
        return "\n".join(sorted(self.visible)) + "\ncontact: jane@bank.com"

    def start_human_capture(self):
        self.capturing = True

    def stop_human_capture(self):
        self.capturing = False
        return [{"kind": "click", "target": "Specialties"}]


def _read_json(path):
    with open(path) as f:
        return json.load(f)


def never_called_operator(kind, summary):
    raise AssertionError("operator should not have been asked")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.policy = Policy.load()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def engine(self, page, operator=never_called_operator, **kw):
        return ReplayEngine(page, policy=self.policy, operator=operator, echo=False,
                            runs_root=os.path.join(self.tmp, "runs"),
                            escalations_dir=os.path.join(self.tmp, "esc"), **kw)

    def escalation_records(self):
        d = os.path.join(self.tmp, "esc")
        return [_read_json(os.path.join(d, f)) for f in sorted(os.listdir(d))] if os.path.isdir(d) else []


class TestHappyPath(Base):
    def test_success_returns_outputs_and_recovered(self):
        page = FakePage()
        r = self.engine(page).run(ARTIFACT, {})
        self.assertEqual(r.status, "success", r.message)
        self.assertEqual(r.outcome_code, "completed")
        self.assertEqual(r.outputs["order_total"], "$11.47")
        self.assertFalse(r.human_intervened)
        self.assertEqual(r.recovered[0]["kind"], "dismissed_interstitial")
        # the typed location comes from the artifact, not "" (the v1 bug)
        self.assertEqual(page.typed, [("Please enter City and State", "San Jose, CA")])
        self.assertTrue(os.path.exists(os.path.join(r.evidence_dir, "result.json")))

    def test_inputs_are_substituted(self):
        page = FakePage()
        r = self.engine(page).run(ARTIFACT, {"location": "Cupertino, CA", "side_item": "Cheesy Roll Up"})
        self.assertEqual(r.status, "success", r.message)
        self.assertEqual(page.typed[0][1], "Cupertino, CA")
        self.assertIn("Cheesy Roll Up", page.cart)


class TestBusinessOutcomes(Base):
    def test_item_not_on_menu_is_business_outcome_not_crash(self):
        r = self.engine(FakePage()).run(ARTIFACT, {"side_item": "McNuggets"})
        self.assertEqual(r.status, "business_outcome")
        self.assertEqual(r.outcome_code, "item_unavailable")
        self.assertEqual(r.failed_at_step, 15)
        self.assertIn("McNuggets", r.message)
        self.assertFalse(r.human_intervened)

    def test_substitute_used_when_caller_allows_it(self):
        r = self.engine(FakePage()).run(ARTIFACT, {"side_item": "McNuggets", "substitute_item": "Cheesy Roll Up"})
        self.assertEqual(r.status, "business_outcome")
        self.assertEqual(r.outcome_code, "substituted")
        self.assertEqual(r.substitutions[0]["used"], "Cheesy Roll Up")
        self.assertEqual(r.outputs["order_total"], "$11.47")

    def test_broken_page_is_not_misread_as_business_outcome(self):
        # target missing AND the page anchor missing -> hard failure, not "unavailable"
        page = FakePage()
        page.visible = set()
        r = self.engine(page, escalate=False).run(ARTIFACT, {"side_item": "McNuggets"})
        self.assertEqual(r.status, "failure")
        self.assertEqual(r.outcome_code, "step_failed")


class TestHardFailuresAndEscalation(Base):
    def test_injected_failure_without_escalation_reports_step_expected_observed(self):
        r = self.engine(FakePage(), escalate=False).run(ARTIFACT, {}, inject_failure_step=9)
        self.assertEqual(r.status, "failure")
        self.assertEqual(r.failed_at_step, 9)
        self.assertIn("Specialties", r.expected)
        self.assertTrue(r.observed)
        self.assertTrue(os.path.exists(os.path.join(r.evidence_dir, "failure_step_9.png")))

    def test_human_fixes_step_and_automation_resumes(self):
        page = FakePage()

        def operator(kind, summary):
            self.assertEqual(kind, "stuck")
            page.clicks.append("Specialties (by human)")
            return OperatorResponse(mode="resume", note="clicked the Specialties tab")

        r = self.engine(page, operator=operator).run(ARTIFACT, {}, inject_failure_step=9)
        self.assertEqual(r.status, "success", r.message)
        self.assertEqual(r.outcome_code, "completed_with_human_intervention")
        self.assertTrue(r.human_intervened)
        # automation really continued with steps 10..17 after the handoff
        self.assertIn("Your Order", page.clicks)
        rec = self.escalation_records()[0]
        self.assertEqual(rec["status"], "resolved")
        self.assertEqual(rec["handback_mode"], "resume")
        self.assertEqual(rec["controller"], "automation")
        self.assertEqual(rec["human_actions"], [{"kind": "click", "target": "Specialties"}])
        self.assertEqual([h["to"] for h in rec["control_history"]], ["human", "automation"])

    def test_operator_instruction_is_plain_and_escalations_are_capped(self):
        # human keeps pressing Enter without fixing anything
        page = FakePage(broken={"Specialties", "Black Bean Crunchwrap Supreme®", "Fiesta Strips", "Seasoned Rice"})
        calls = []

        def operator(kind, summary):
            calls.append(summary)
            return OperatorResponse(mode="resume")

        r = self.engine(page, operator=operator).run(ARTIFACT, {}, inject_failure_step=9)
        self.assertEqual(r.status, "failure")
        self.assertEqual(r.outcome_code, "too_many_escalations")
        self.assertEqual(len(calls), ReplayEngine.MAX_ESCALATIONS)
        recs = self.escalation_records()
        self.assertIn("click 'Specialties'", recs[0]["operator_instruction"])
        self.assertNotIn("__INJECTED", recs[0]["operator_instruction"])
        self.assertIn("resumed here right after a human handoff", recs[1]["reason"])
        self.assertTrue(all(rec["status"] == "unresolved" for rec in recs))

    def test_human_says_done_but_checkpoint_not_met_is_failure(self):
        def operator(kind, summary):
            return OperatorResponse(mode="finished", note="I think I fixed it")

        r = self.engine(FakePage(), operator=operator).run(ARTIFACT, {}, inject_failure_step=9)
        self.assertEqual(r.status, "failure")
        self.assertTrue(r.human_intervened)
        self.assertEqual(self.escalation_records()[0]["status"], "unresolved")

    def test_checkpoint_reached_but_item_missing(self):
        page = FakePage(broken={"Large Nacho Fries"})
        page.clickable.add("Large Nacho Fries")

        orig = page.click

        def click_without_adding(loc):  # "click succeeds" but the item silently isn't added
            if loc["value"] == "Large Nacho Fries":
                return None
            return orig(loc)

        page.click = click_without_adding
        r = self.engine(page).run(ARTIFACT, {})
        self.assertEqual(r.status, "failure")
        self.assertEqual(r.outcome_code, "checkpoint_incomplete")


class TestSafety(Base):
    def _artifact_with_step_label(self, label):
        art = copy.deepcopy(ARTIFACT)
        art["steps"][16]["locator"]["value"] = label
        return art

    def test_irreversible_action_blocked_and_never_clicked(self):
        page = FakePage(clickable=MENU | {"Place Order"})
        r = self.engine(page).run(self._artifact_with_step_label("Place Order"), {})
        self.assertEqual(r.status, "failure")
        self.assertEqual(r.outcome_code, "policy_blocked")
        self.assertNotIn("Place Order", page.clicks)

    def test_risky_action_needs_approval_denied(self):
        page = FakePage(clickable=MENU | {"Remove item"})

        def deny(kind, summary):
            self.assertEqual(kind, "approval")
            return OperatorResponse(mode="answered", decision="deny")

        r = self.engine(page, operator=deny).run(self._artifact_with_step_label("Remove item"), {})
        self.assertEqual(r.outcome_code, "action_denied")
        self.assertNotIn("Remove item", page.clicks)
        self.assertEqual(self.escalation_records()[0]["status"], "denied")

    def test_off_allowlist_navigation_blocked(self):
        art = copy.deepcopy(ARTIFACT)
        art["target_url"] = "https://evil.example.com/"
        page = FakePage()
        r = self.engine(page).run(art, {})
        self.assertEqual(r.outcome_code, "policy_blocked")
        self.assertEqual(page.clicks, [])

    def test_sensitive_input_and_pii_never_hit_disk(self):
        art = copy.deepcopy(ARTIFACT)
        for p in art["inputs"]:
            if p["name"] == "location":
                p["sensitive"] = True
        r = self.engine(FakePage(), escalate=False).run(art, {"location": "123 Secret Lane"},
                                                        inject_failure_step=9)
        blob = ""
        for f in os.listdir(r.evidence_dir):
            if not f.endswith(".png"):
                with open(os.path.join(r.evidence_dir, f)) as fh:
                    blob += fh.read()
        self.assertNotIn("123 Secret Lane", blob)
        self.assertNotIn("jane@bank.com", blob)
        self.assertIn("[REDACTED", blob)

    def test_require_approved_refuses_drafts(self):
        art = copy.deepcopy(ARTIFACT)
        art["status"] = "draft"
        page = FakePage()
        r = self.engine(page, require_approved=True).run(art, {})
        self.assertEqual(r.outcome_code, "artifact_not_approved")
        self.assertEqual(page.clicks, [])


class TestContract(Base):
    def test_unknown_input_rejected(self):
        r = self.engine(FakePage()).run(ARTIFACT, {"member_id": "123"})
        self.assertEqual(r.outcome_code, "invalid_input")

    def test_missing_required_input_rejected(self):
        art = copy.deepcopy(ARTIFACT)
        art["inputs"][0]["default"] = None
        r = self.engine(FakePage()).run(art, {})
        self.assertEqual(r.outcome_code, "invalid_input")

    def test_malformed_artifact_rejected_before_browser(self):
        art = copy.deepcopy(ARTIFACT)
        art["steps"][1]["input_value"] = None      # a type step that doesn't say what to type
        page = FakePage()
        r = self.engine(page).run(art, {})
        self.assertEqual(r.outcome_code, "invalid_artifact")
        self.assertEqual(page.clicks, [])

    def test_undeclared_placeholder_rejected(self):
        art = copy.deepcopy(ARTIFACT)
        art["steps"][8]["locator"]["value"] = "{{category}}"
        self.assertEqual(self.engine(FakePage()).run(art, {}).outcome_code, "invalid_artifact")

    def test_replay_never_imports_the_llm(self):
        with open(os.path.join(ROOT, "replay.py")) as f:
            src = f.read()
        self.assertNotIn("llm_client", src)
        self.assertNotIn("anthropic", src)


class TestDiscoveryParameterization(unittest.TestCase):
    def test_concrete_values_become_placeholders(self):
        from discovery import add_missing_target_rules, parameterize
        from schema import Locator, Step
        steps = [Step(1, "type", Locator("text", "Please enter City and State"), "", input_value="San Jose, CA"),
                 Step(2, "click", Locator("role", "Large Nacho Fries", "Add to Order"), "")]
        out = add_missing_target_rules(parameterize(steps, {"location": "San Jose, CA",
                                                             "side_item": "Large Nacho Fries"}))
        self.assertEqual(out[0].input_value, "{{location}}")
        self.assertEqual(out[1].locator.value, "{{side_item}}")
        self.assertEqual(out[1].on_missing.outcome_code, "item_unavailable")
        self.assertEqual(out[1].fallback.locator.value, "{{substitute_item}}")


if __name__ == "__main__":
    unittest.main()
