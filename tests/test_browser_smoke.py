"""
Real-browser smoke test: the REAL PlaywrightPage + ReplayEngine against a
local mock app (mock_app/index.html), headless, no network, no LLM.

Covers the same five outcomes as the live demo: success, business outcome
(item unavailable), substitution, hard failure, and a human handoff where
a (simulated) operator clicks in the SAME live session and automation resumes.

Skipped automatically if Playwright/Chromium isn't installed.
"""

import functools
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from escalation import OperatorResponse  # noqa: E402
from policy import Policy  # noqa: E402
from replay import ReplayEngine  # noqa: E402

try:
    from playwright_page import PlaywrightPage
    HAVE_PW = True
except Exception:  # pragma: no cover
    HAVE_PW = False

PORT = 8765


@unittest.skipUnless(HAVE_PW, "playwright not installed")
class BrowserSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=os.path.join(ROOT, "mock_app"))
        http.server.SimpleHTTPRequestHandler.log_message = lambda *a, **k: None
        cls.server = http.server.ThreadingHTTPServer(("localhost", PORT), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        with open(os.path.join(ROOT, "mock_app", "mock_order_artifact.json")) as f:
            cls.artifact = json.load(f)
        cls.policy = Policy(allowed_domains=["localhost"],
                            allowed_actions=["click", "type", "navigate", "wait", "press_enter"],
                            blocked_labels=["place order"], approval_labels=["remove"])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, inputs, inject=None, human=False):
        page = PlaywrightPage(headless=True)

        def operator(kind, summary):
            page.page.get_by_text("Start Your Order").click()   # the "human" fixes the step
            return OperatorResponse(mode="resume", note="clicked Start Your Order")

        eng = ReplayEngine(page, policy=self.policy, operator=operator, escalate=human, echo=False,
                           runs_root=os.path.join(self.tmp, "runs"), escalations_dir=os.path.join(self.tmp, "esc"))
        try:
            return eng.run(self.artifact, inputs, inject_failure_step=inject)
        finally:
            page.close()

    def test_success(self):
        r = self._run({})
        self.assertEqual((r.status, r.outcome_code), ("success", "completed"), r.message)
        self.assertEqual(r.outputs["order_total"], "$11.47")

    def test_item_unavailable(self):
        r = self._run({"side_item": "McNuggets"})
        self.assertEqual((r.status, r.outcome_code), ("business_outcome", "item_unavailable"), r.message)

    def test_substitution(self):
        r = self._run({"side_item": "McNuggets", "substitute_item": "Cheesy Roll Up"})
        self.assertEqual((r.status, r.outcome_code), ("business_outcome", "substituted"), r.message)

    def test_hard_failure(self):
        r = self._run({}, inject=4)
        self.assertEqual((r.status, r.failed_at_step), ("failure", 4), r.message)

    def test_human_handoff_resumes_same_session(self):
        r = self._run({}, inject=4, human=True)
        self.assertEqual((r.status, r.outcome_code), ("success", "completed_with_human_intervention"), r.message)
        esc_dir = os.path.join(self.tmp, "esc")
        with open(os.path.join(esc_dir, os.listdir(esc_dir)[0])) as f:
            rec = json.load(f)
        self.assertEqual(rec["status"], "resolved")
        self.assertEqual(rec["human_actions"][0]["target"], "Start Your Order")


if __name__ == "__main__":
    unittest.main()
