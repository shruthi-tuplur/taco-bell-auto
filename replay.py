"""
Replay engine: reads a saved artifact and executes its steps IN ORDER,
with ZERO LLM calls. This is the production path an AI agent triggers to
invoke a saved capability.

This file does not import any LLM client, anywhere. A reviewer can verify
that determinism claim just by reading the imports.

Everything app-specific (what to click, what "done" looks like, which
missing element means "no such item") comes from the ARTIFACT. The engine
itself is generic.

Result contract (see ReplayResult):
  status = "success"          goal reached, checkpoint verified, outputs returned
  status = "business_outcome" a legitimate answer the caller must handle,
                              NOT a crash (item unavailable, substitution used)
  status = "failure"          hard failure with step / expected / observed
  plus: recovered[]  -> recoverable conditions the engine handled itself
                        (dismissed interstitials, slow renders it waited out)
        human_intervened / interventions[] -> a human touched this run
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from dataclasses import dataclass, field
from typing import Literal, Optional

from escalation import ControlState, Escalator, terminal_operator
from policy import Policy
from run_log import RunLog
from schema import (Artifact, ArtifactValidationError, Locator, Step, artifact_from_dict, fill)

Status = Literal["success", "business_outcome", "failure"]
INJECT_PREFIX = "__INJECTED_BROKEN__ "


@dataclass
class ReplayResult:
    status: Status
    outcome_code: str                 # machine-readable, e.g. "completed", "item_unavailable"
    message: str                      # human-readable
    outputs: dict = field(default_factory=dict)
    failed_at_step: Optional[int] = None
    expected: Optional[str] = None
    observed: Optional[str] = None
    recovered: list = field(default_factory=list)
    substitutions: list = field(default_factory=list)
    human_intervened: bool = False
    interventions: list = field(default_factory=list)
    artifact: str = ""
    run_id: str = ""
    evidence_dir: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class StepFailed(Exception):
    pass


class ReplayEngine:
    MAX_ESCALATIONS = 3
    """
    Wraps a surface adapter ("page", e.g. PlaywrightPage) and walks it
    through an artifact. The page only needs the small interface used below,
    which is what lets tests drive the engine with a fake page and what would
    let a desktop adapter slot in later.
    """

    def __init__(self, page, policy: Optional[Policy] = None, operator=terminal_operator,
                 escalate: bool = True, require_approved: bool = False, runs_root: Optional[str] = None,
                 escalations_dir: Optional[str] = None, echo: bool = True):
        self.page = page
        self.policy = policy or Policy.load()
        self.operator = operator
        self.escalate_enabled = escalate
        self.require_approved = require_approved
        self.runs_root = runs_root
        self.escalations_dir = escalations_dir
        self.echo = echo

    # ==================================================================
    # public entry point
    # ==================================================================
    def run(self, artifact_in, inputs: dict, inject_failure_step: Optional[int] = None) -> ReplayResult:
        # ---- 1. load + validate the artifact (fail before touching a browser)
        try:
            art = artifact_in if isinstance(artifact_in, Artifact) else artifact_from_dict(artifact_in)
        except ArtifactValidationError as e:
            return ReplayResult("failure", "invalid_artifact", str(e))

        # ---- 2. validate + coerce the caller's typed inputs
        try:
            params = self._resolve_inputs(art, inputs)
        except ValueError as e:
            return ReplayResult("failure", "invalid_input", str(e), artifact=art.artifact_id)

        secrets = [str(params[p.name]) for p in art.inputs if p.sensitive and params.get(p.name)]
        kwargs = {"root": self.runs_root} if self.runs_root else {}
        log = RunLog("replay", art.artifact_id, secrets=secrets, echo=self.echo, **kwargs)
        control = ControlState()
        esc_kwargs = {"out_dir": self.escalations_dir} if self.escalations_dir else {}
        escalator = Escalator(self.page, control, run_log=log, operator=self.operator,
                              secrets=secrets, **esc_kwargs)

        if inject_failure_step is not None:
            art = _inject_failure(art, inject_failure_step)

        log.event("replay_started", artifact=art.artifact_id, version=art.version,
                  artifact_status=art.status, inputs=_mask(art, params),
                  injected_failure_step=inject_failure_step)

        ctx = _RunCtx(art=art, params=params, log=log, control=control, escalator=escalator)
        try:
            result = self._execute(ctx)
        except Exception as e:  # truly unexpected: still return a structured failure
            self._capture(ctx, "crash")
            result = self._fail(ctx, "engine_error", f"Unexpected engine error: {e!r}")

        # close out every escalation with the engine's OWN verdict, not the human's say-so
        for rec in ctx.open_escalations:
            escalator.finalize(rec, resolved=result.status != "failure",
                               detail=f"final replay status={result.status} ({result.outcome_code})")

        result.recovered = list(getattr(self.page, "recovered_events", [])) + ctx.recovered
        result.substitutions = ctx.substitutions
        result.interventions = escalator.records
        result.human_intervened = bool(escalator.records)
        result.artifact = f"{art.artifact_id}@{art.version}"
        result.run_id = log.run_id
        result.evidence_dir = log.dir
        log.event("replay_finished", status=result.status, outcome_code=result.outcome_code,
                  message=result.message)
        log.write_json("result.json", result.to_dict())
        log.close()
        return result

    # ==================================================================
    # core loop
    # ==================================================================
    def _execute(self, ctx: "_RunCtx") -> ReplayResult:
        art, log = ctx.art, ctx.log

        if art.status != "approved":
            msg = f"Artifact status is '{art.status}', not 'approved'."
            if self.require_approved:
                return self._fail(ctx, "artifact_not_approved", msg + " Refusing unattended replay.")
            log.event("warning", message=msg + " Running anyway (require_approved=False).")

        # entry point (discovery navigates here before its loop, so replay does too)
        blocked = self._policy_url(ctx, art.target_url)
        if blocked:
            return blocked
        self.page.navigate(art.target_url)

        for step in art.steps:
            ctx.control.require_automation()
            outcome = self._run_step(ctx, step)
            if outcome is not None:       # step ended the run (business outcome / failure / human finish)
                return outcome
            blocked = self._policy_url(ctx, self.page.current_url())
            if blocked:
                return blocked

        return self._verify_checkpoint(ctx)

    def _run_step(self, ctx: "_RunCtx", step: Step) -> Optional[ReplayResult]:
        log, params = ctx.log, ctx.params
        loc = _fill_loc(step.locator, params)
        label = " ".join(x for x in [loc.get("value") if loc else "", (loc or {}).get("button_text") or ""] if x)

        # ---- policy gate BEFORE acting ------------------------------------
        decision = self.policy.check_action(step.action, label)
        if decision.verdict == "block":
            log.event("policy_block", step=step.step_number, action=step.action, label=label, reason=decision.reason)
            return self._fail(ctx, "policy_blocked", f"Step {step.step_number} blocked by policy: {decision.reason}",
                              step=step.step_number, expected=f"{step.action} '{label}'", observed="blocked by policy")
        if decision.verdict == "needs_approval":
            rec = ctx.escalator.escalate(kind="approval", capability=ctx.art.artifact_id,
                                         step_number=step.step_number,
                                         reason=f"Risky action needs approval: {decision.reason}")
            if rec["status"] != "approved":
                return self._fail(ctx, "action_denied", f"Operator denied risky step {step.step_number} ('{label}').",
                                  step=step.step_number)

        log.event("step_start", step=step.step_number, action=step.action, target=label,
                  why=step.description[:160])

        try:
            if step.action == "navigate":
                self.page.navigate(ctx.art.target_url)
            elif step.action == "wait":
                self.page.settle()
            elif step.action == "press_enter":
                self.page.press_enter()
            elif step.action == "type":
                self.page.type_text(loc, fill(step.input_value, params))
            elif step.action == "click":
                self.page.click(loc)
            else:
                raise StepFailed(f"unrecognized action '{step.action}'")
            log.event("step_ok", step=step.step_number, strategy=_last_action(self.page))
            return None
        except Exception as primary_error:
            log.event("step_target_missing", step=step.step_number, error=str(primary_error)[:300])
            return self._handle_step_failure(ctx, step, loc, label, primary_error)

    # ==================================================================
    # the error taxonomy lives here
    # ==================================================================
    def _handle_step_failure(self, ctx, step: Step, loc, label, err) -> Optional[ReplayResult]:
        log, params = ctx.log, ctx.params

        # (a) declared fallback -> substitution (a business outcome, reported)
        if step.fallback is not None:
            fb_loc = _fill_loc(step.fallback.locator, params)
            if fb_loc and fb_loc.get("value"):
                try:
                    self.page.click(fb_loc)
                    ctx.substitutions.append({"step": step.step_number, "requested": loc.get("value"),
                                              "used": fb_loc["value"], "reason": step.fallback.condition})
                    log.event("fallback_used", step=step.step_number, requested=loc.get("value"),
                              used=fb_loc["value"])
                    return None
                except Exception as fb_err:
                    log.event("fallback_failed", step=step.step_number, error=str(fb_err)[:200])

        # (b) a known business-state signature is on screen
        for sig in ctx.art.outcome_signatures:
            if self.page.find(_fill_loc(sig.locator, params), timeout_ms=1500):
                self._capture(ctx, step.step_number)
                return ReplayResult("business_outcome", sig.outcome_code, fill(sig.message, params),
                                    failed_at_step=step.step_number, expected=label,
                                    observed=f"page shows '{sig.locator.value}'")

        # (c) screen is provably healthy but the target doesn't exist -> business outcome
        rule = step.on_missing
        if rule is not None:
            anchor = _fill_loc(rule.page_anchor, params)
            if self.page.find(anchor, timeout_ms=4000):
                self._capture(ctx, step.step_number)
                log.event("business_outcome", step=step.step_number, code=rule.outcome_code,
                          anchor_seen=anchor["value"])
                return ReplayResult("business_outcome", rule.outcome_code, fill(rule.message, params),
                                    failed_at_step=step.step_number, expected=label,
                                    observed=f"page is healthy (anchor '{anchor['value']}' visible) "
                                             f"but '{loc.get('value')}' is not on it")

        # (d) genuinely stuck -> hard failure, but first offer it to a human
        self._capture(ctx, step.step_number)
        msg = f"Step {step.step_number}: could not {step.action} '{label}' and no fallback/outcome rule applied."
        if not self.escalate_enabled:
            return self._fail(ctx, "step_failed", msg, step=step.step_number, expected=f"{step.action} '{label}'",
                              observed=str(err)[:300])

        # A resume that didn't actually fix things shows up as the very next
        # step failing too. Cap the loop instead of paging the human forever.
        if len(ctx.open_escalations) >= self.MAX_ESCALATIONS:
            return self._fail(ctx, "too_many_escalations",
                              msg + f" Already escalated {len(ctx.open_escalations)} times in this run; stopping.",
                              step=step.step_number, expected=f"{step.action} '{label}'", observed=str(err)[:300])
        if ctx.last_resume_step == step.step_number - 1:
            msg += (f" NOTE: automation resumed here right after a human handoff at step {ctx.last_resume_step}, "
                    f"so that step may not have been completed.")

        hint = self._checkpoint_hint(ctx)
        human_label = label.replace(INJECT_PREFIX, "")
        todo = {"click": f"click '{human_label}'", "type": f"type into '{human_label}'"}.get(
            step.action, f"do step {step.step_number}")
        todo += f" (this is step {step.step_number} of {len(ctx.art.steps)}), then come back here."
        rec = ctx.escalator.escalate(kind="stuck", capability=ctx.art.artifact_id, step_number=step.step_number,
                                     reason=msg, expected=f"{step.action} '{human_label}'", checkpoint_hint=hint,
                                     todo=todo)
        ctx.open_escalations.append(rec)
        ctx.human_intervened = True

        if rec.get("handback_mode") == "resume":
            # Human fixed just this step. Automation takes the wheel again and
            # continues with the next recorded step. Later steps + the final
            # checkpoint are what prove the fix actually worked.
            log.event("resumed_after_human", step=step.step_number, next_step=step.step_number + 1)
            ctx.last_resume_step = step.step_number
            return None

        # Human finished the flow. Never trust that blindly: verify ourselves.
        verified = self._verify_checkpoint(ctx, after_human=True)
        if verified.status == "failure":
            verified.message = msg + " Escalated to a human, but the checkpoint was still not met afterwards. " \
                               + verified.message
            verified.failed_at_step = step.step_number
        return verified

    # ==================================================================
    # checkpoint + outputs
    # ==================================================================
    def _verify_checkpoint(self, ctx, after_human: bool = False) -> ReplayResult:
        art, params = ctx.art, ctx.params
        cp_loc = _fill_loc(art.checkpoint.locator, params)
        # after a human handoff the page may still be settling: give it longer
        timeout = 9000 if after_human else 6000
        if not self.page.find(cp_loc, timeout_ms=timeout):
            self._capture(ctx, "checkpoint")
            return self._fail(ctx, "checkpoint_not_met", "All steps ran, but the checkpoint was not found.",
                              expected=f"visible: '{cp_loc['value']}' ({art.checkpoint.description})",
                              observed="checkpoint text not visible")

        # beyond the checkpoint, every expected item must actually be present
        subs = {s["requested"]: s["used"] for s in ctx.substitutions}
        missing = []
        for tmpl in art.checkpoint.must_contain:
            want = fill(tmpl, params)
            want = subs.get(want, want)
            if not self.page.find({"type": "text", "value": want}, timeout_ms=3000):
                missing.append(want)
        if missing:
            self._capture(ctx, "checkpoint")
            return self._fail(ctx, "checkpoint_incomplete",
                              "Checkpoint screen reached, but expected item(s) are missing. An earlier step "
                              "silently failed without raising an error.",
                              expected=f"all of {art.checkpoint.must_contain}", observed=f"missing {missing}")

        outputs = {}
        for out in art.outputs:
            try:
                outputs[out.name] = self.page.extract(out.extractor) if out.extractor else None
            except Exception as e:
                outputs[out.name] = None
                ctx.log.event("output_extract_failed", output=out.name, error=str(e)[:200])

        if after_human or ctx.human_intervened:
            return ReplayResult("success", "completed_with_human_intervention",
                                "Checkpoint verified after a human operator took over and handed control back.",
                                outputs=outputs)
        if ctx.substitutions:
            s = ctx.substitutions[0]
            return ReplayResult("business_outcome", "substituted",
                                f"Completed, but '{s['requested']}' was unavailable and '{s['used']}' was used "
                                f"instead.", outputs=outputs)
        return ReplayResult("success", "completed", "Checkpoint verified and all expected items are present.",
                            outputs=outputs)

    # ==================================================================
    # helpers
    # ==================================================================
    def _resolve_inputs(self, art: Artifact, inputs: dict) -> dict:
        declared = {p.name: p for p in art.inputs}
        unknown = set(inputs) - set(declared)
        if unknown:
            raise ValueError(f"Unknown input(s) {sorted(unknown)}. This capability accepts {sorted(declared)}.")
        params = {}
        for name, p in declared.items():
            val = inputs.get(name, p.default)
            if val is None or val == "":
                if p.required:
                    raise ValueError(f"Missing required input '{name}' ({p.type}): {p.description}")
                params[name] = ""
                continue
            params[name] = _coerce(name, p.type, val)
        return params

    def _policy_url(self, ctx, url) -> Optional[ReplayResult]:
        d = self.policy.check_url(url)
        if d.verdict != "allow":
            ctx.log.event("policy_block", url=url, reason=d.reason)
            return self._fail(ctx, "policy_blocked", f"Navigation blocked by policy: {d.reason}",
                              expected="allowlisted domain", observed=url)
        return None

    def _checkpoint_hint(self, ctx) -> str:
        cp = fill(ctx.art.checkpoint.locator.value, ctx.params)
        items = [fill(x, ctx.params) for x in ctx.art.checkpoint.must_contain]
        return f"'{cp}' is visible" + (f" and it shows {items}" if items else "")

    def _capture(self, ctx, tag) -> None:
        try:
            self.page.screenshot(path=ctx.log.path(f"failure_step_{tag}.png"))
        except Exception:
            pass
        try:
            ctx.log.write_text(f"failure_step_{tag}_tree.txt", self.page.get_accessibility_snapshot())
        except Exception:
            pass

    def _fail(self, ctx, code, message, step=None, expected=None, observed=None) -> ReplayResult:
        return ReplayResult("failure", code, message, failed_at_step=step, expected=expected, observed=observed)


@dataclass
class _RunCtx:
    art: Artifact
    params: dict
    log: RunLog
    control: ControlState
    escalator: Escalator
    recovered: list = field(default_factory=list)
    substitutions: list = field(default_factory=list)
    open_escalations: list = field(default_factory=list)
    human_intervened: bool = False
    last_resume_step: Optional[int] = None


def _fill_loc(loc: Optional[Locator], params: dict) -> Optional[dict]:
    if loc is None:
        return None
    d = {"type": loc.type, "value": fill(loc.value, params)}
    if loc.button_text:
        d["button_text"] = fill(loc.button_text, params)
    return d


def _coerce(name, typ, val):
    if typ == "string":
        return str(val)
    if typ == "number":
        try:
            return float(val)
        except (TypeError, ValueError):
            raise ValueError(f"Input '{name}' must be a number, got {val!r}")
    if typ == "boolean":
        if isinstance(val, bool):
            return val
        if str(val).lower() in ("true", "1", "yes"):
            return True
        if str(val).lower() in ("false", "0", "no"):
            return False
        raise ValueError(f"Input '{name}' must be a boolean, got {val!r}")
    raise ValueError(f"Input '{name}' has unknown type {typ}")


def _mask(art: Artifact, params: dict) -> dict:
    sens = {p.name for p in art.inputs if p.sensitive}
    return {k: ("[REDACTED]" if k in sens else v) for k, v in params.items()}


def _last_action(page) -> str:
    try:
        return page.last_action()
    except Exception:
        return ""


def _inject_failure(art: Artifact, step_number: int) -> Artifact:
    """Simulate a broken control at one step (for the error-path demo):
    the target is renamed so it cannot be found, and its fallback /
    outcome rules are removed so the engine sees a genuine hard failure."""
    art = dataclasses.replace(art, steps=[dataclasses.replace(s) for s in art.steps])
    for s in art.steps:
        if s.step_number == step_number and s.locator is not None:
            s.locator = dataclasses.replace(s.locator, value=INJECT_PREFIX + s.locator.value)
            s.fallback = None
            s.on_missing = None
            return art
    raise ValueError(f"No step {step_number} with a locator to inject a failure into")


def load_artifact(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministically replay a saved capability (no LLM).")
    ap.add_argument("artifact", nargs="?", default="artifacts/taco_bell_order_checkout.json")
    ap.add_argument("--input", "-i", action="append", default=[], metavar="NAME=VALUE",
                    help="capability input, repeatable, e.g. -i side_item='Large Nacho Fries'")
    ap.add_argument("--inject-failure", type=int, metavar="STEP",
                    help="simulate a broken control at STEP to demo hard-failure + human escalation")
    ap.add_argument("--no-escalate", action="store_true", help="fail fast instead of asking a human")
    ap.add_argument("--require-approved", action="store_true", help="refuse to run draft artifacts")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args(argv)

    inputs = {}
    for kv in args.input:
        if "=" not in kv:
            ap.error(f"--input must look like NAME=VALUE, got {kv!r}")
        k, v = kv.split("=", 1)
        inputs[k.strip()] = v.strip()

    from playwright_page import PlaywrightPage   # imported here so tests never need a browser
    page = PlaywrightPage(headless=args.headless)
    engine = ReplayEngine(page, escalate=not args.no_escalate, require_approved=args.require_approved)
    print(f"--- Replaying {args.artifact} with inputs {inputs} ---")
    result = engine.run(load_artifact(args.artifact), inputs, inject_failure_step=args.inject_failure)

    print("\n" + "=" * 64)
    print(f"STATUS       : {result.status}")
    print(f"OUTCOME CODE : {result.outcome_code}")
    print(f"MESSAGE      : {result.message}")
    print(f"OUTPUTS      : {result.outputs}")
    if result.failed_at_step is not None:
        print(f"FAILED AT    : step {result.failed_at_step}")
        print(f"EXPECTED     : {result.expected}")
        print(f"OBSERVED     : {result.observed}")
    print(f"RECOVERED    : {result.recovered}")
    print(f"HUMAN        : {result.human_intervened} {result.interventions}")
    print(f"EVIDENCE     : {result.evidence_dir}")
    print("=" * 64)
    page.close(pause_before_close=not args.headless)
    return 0 if result.status != "failure" else 1


if __name__ == "__main__":
    sys.exit(main())
