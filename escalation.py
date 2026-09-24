"""
Human-in-the-loop escalation and control transfer (assignment section 3.6).

Control-transfer model:
  - A ControlState object records who is in control: "automation" or "human".
    Automation code checks it before acting; it only flips on an explicit
    handoff and flips back on an explicit hand-back.
  - The browser is a real headed Playwright session. On escalation the
    automation STOPS issuing commands, sets controller="human", and the
    operator works in that exact same window (same cookies, same cart, same
    session). No fresh session, no re-login.
  - While the human is in control, a small listener injected into the page
    records what they click and which fields they edit (never the values).
  - The operator hands control back from the terminal (Enter) and leaves a
    short note. Automation takes control back and re-verifies state before
    trusting anything (it never assumes the human "fixed it").

Every escalation writes a structured record to escalations/<id>.json:
pending -> resolved | unresolved | approved | denied, with the context an
operator needs: capability, step, reason, screenshot, accessibility tree,
URL before/after, captured human actions, and the operator's note.

The operator "console" is the terminal. That is the deliberate mock: the
record format is exactly what a real queue / web console would consume.
"""

from __future__ import annotations

import difflib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Literal, Optional

from policy import redact_obj

HERE = os.path.dirname(os.path.abspath(__file__))
ESCALATIONS_DIR = os.path.join(HERE, "escalations")

Controller = Literal["automation", "human"]
EscalationKind = Literal["stuck", "approval"]


@dataclass
class ControlState:
    controller: Controller = "automation"
    history: list = field(default_factory=list)

    def hand_to(self, who: Controller, reason: str) -> None:
        self.history.append({"at": datetime.now(timezone.utc).isoformat(),
                             "from": self.controller, "to": who, "reason": reason})
        self.controller = who

    def require_automation(self) -> None:
        if self.controller != "automation":
            raise RuntimeError("Automation tried to act while a human is in control")


@dataclass
class OperatorResponse:
    mode: Literal["resume", "finished", "answered"]
    # resume   = human fixed just the stuck step; automation continues with the NEXT step
    # finished = human completed the rest of the flow; automation only verifies the checkpoint
    # answered = approval question answered (see decision)
    decision: Optional[str] = None   # for approvals: "approve" | "deny"
    note: str = ""


def terminal_operator(kind: EscalationKind, summary: str) -> OperatorResponse:
    """The mock operator console: the human answers in the terminal."""
    if kind == "approval":
        ans = input("\nType 'approve' to let automation perform this action, anything else to deny: ").strip().lower()
        note = input("Optional note for the record (press Enter to skip): ").strip()
        return OperatorResponse(mode="answered", decision="approve" if ans == "approve" else "deny", note=note)
    print("\nWhen you're done in the browser, hand control back here:")
    print("  - press ENTER        -> you fixed just this step; automation CONTINUES from the next step")
    print("  - type 'done' + ENTER -> you finished the whole flow; automation just VERIFIES the result")
    ans = input("> ").strip().lower()
    note = input("In a few words, what did you do? (press Enter to skip): ").strip()
    return OperatorResponse(mode="finished" if ans == "done" else "resume", note=note)


class Escalator:
    def __init__(self, page, control: ControlState, run_log=None,
                 operator: Callable[[EscalationKind, str], OperatorResponse] = terminal_operator,
                 secrets=(), out_dir: str = ESCALATIONS_DIR):
        self.page = page
        self.control = control
        self.log = run_log
        self.operator = operator
        self.secrets = list(secrets)
        self.out_dir = out_dir
        self.records: list[str] = []

    def _write(self, path: str, record: dict) -> None:
        with open(path, "w") as f:
            json.dump(redact_obj(record, self.secrets), f, indent=2, default=str)

    def escalate(self, *, kind: EscalationKind, capability: str, step_number, reason: str,
                 expected: str = "", checkpoint_hint: str = "") -> dict:
        os.makedirs(self.out_dir, exist_ok=True)
        req_id = f"intervention_{int(time.time() * 1000)}"
        path = os.path.join(self.out_dir, f"{req_id}.json")

        # --- snapshot the state BEFORE handing over -----------------------
        before_tree = _safe(self.page.get_accessibility_snapshot, "")
        before_url = _safe(self.page.current_url, "")
        shot = None
        if self.log is not None:
            shot = self.log.path(f"escalation_step_{step_number}.png")
            _safe(lambda: self.page.screenshot(path=shot), None)
            self.log.write_text(f"escalation_step_{step_number}_tree.txt", before_tree)

        record = {
            "id": req_id,
            "kind": kind,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "capability": capability,
            "step_number": step_number,
            "reason": reason,
            "expected": expected,
            "checkpoint_hint": checkpoint_hint,
            "url_before": before_url,
            "screenshot": shot,
            "run_id": getattr(self.log, "run_id", None),
            "controller": "human",
        }
        self._write(path, record)
        self.records.append(req_id)

        # --- hand control to the human ------------------------------------
        self.control.hand_to("human", reason)
        if self.log:
            self.log.event("escalation_opened", id=req_id, kind=kind, step=step_number, reason=reason)
        _safe(self.page.start_human_capture, None)

        print("\n" + "=" * 64)
        print(f"ESCALATION ({kind}): human operator needed")
        print(f"  Capability : {capability}")
        print(f"  Step       : {step_number}")
        print(f"  Why        : {reason}")
        if expected:
            print(f"  Expected   : {expected}")
        print(f"  Record     : {path}")
        print("  Control    : HUMAN. The automation will not touch the browser until you hand it back.")
        if kind == "stuck":
            print("  The browser window on your screen is the SAME live session.")
            print("  Fix it / finish the flow there by hand.")
            if checkpoint_hint:
                print(f"  When you hand back, automation will verify: {checkpoint_hint}")
        print("=" * 64)

        response = self.operator(kind, reason)

        # --- take control back and record what the human did -------------
        human_actions = _safe(self.page.stop_human_capture, []) or []
        self.control.hand_to("automation", "operator handed control back")
        after_tree = _safe(self.page.get_accessibility_snapshot, "")
        after_url = _safe(self.page.current_url, "")

        if kind == "approval":
            record["status"] = "approved" if response.decision == "approve" else "denied"
        else:
            record["status"] = "handed_back"   # replay decides resolved/unresolved after re-verifying
        record.update({
            "controller": "automation",
            "handed_back_at": datetime.now(timezone.utc).isoformat(),
            "handback_mode": response.mode,
            "operator_note": response.note,
            "decision": response.decision,
            "url_after": after_url,
            "human_actions": human_actions,
            "page_changes": _tree_diff(before_tree, after_tree),
            "control_history": self.control.history[-2:],
        })
        self._write(path, record)
        if self.log:
            self.log.event("escalation_handed_back", id=req_id, status=record["status"],
                           human_actions=len(human_actions), note=response.note)
        record["_path"] = path
        return record

    def finalize(self, record: dict, resolved: bool, detail: str = "") -> None:
        """Called by the engine AFTER it re-verified state itself."""
        path = record.get("_path")
        if not path or record.get("kind") != "stuck":
            return
        record = {k: v for k, v in record.items() if k != "_path"}
        record["status"] = "resolved" if resolved else "unresolved"
        record["verification"] = detail
        record["verified_by_automation_at"] = datetime.now(timezone.utc).isoformat()
        self._write(path, record)


def _tree_diff(before: str, after: str, limit: int = 25) -> dict:
    b, a = (before or "").splitlines(), (after or "").splitlines()
    added = [l.strip() for l in difflib.unified_diff(b, a, lineterm="", n=0)
             if l.startswith("+") and not l.startswith("+++")]
    removed = [l.strip() for l in difflib.unified_diff(b, a, lineterm="", n=0)
               if l.startswith("-") and not l.startswith("---")]
    return {"lines_added": len(added), "lines_removed": len(removed), "sample_added": added[:limit]}


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default
