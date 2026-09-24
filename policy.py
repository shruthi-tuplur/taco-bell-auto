"""
Safety & policy guardrails. Enforced in code, in ONE place, for BOTH the
LLM-driven discovery loop and the deterministic replay engine.

Three layers:
  1. Allowlist: which domains the automation may be on, and which action
     types it may perform. Anything else is blocked.
  2. Risk classes for actions:
       - "blocked":  irreversible / out-of-scope (pay, place order, submit
                     payment). Never performed by automation, full stop.
       - "approval": risky but sometimes legitimate (remove, delete, sign in).
                     Pauses and asks a human operator to approve or deny.
       - "safe":     everything else on the allowlist.
  3. Redaction: nothing sensitive is written to artifacts, logs, or
     escalation records (emails, phone numbers, card/account-like numbers,
     and any input the artifact marks sensitive=True).

The policy lives in policy.json so it is configurable per deployment
(per tenant, in the real system) without code changes.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Iterable, Literal, Optional
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_POLICY_PATH = os.path.join(HERE, "policy.json")

Verdict = Literal["allow", "needs_approval", "block"]


@dataclass
class PolicyDecision:
    verdict: Verdict
    reason: str


class PolicyViolation(Exception):
    """Raised when an action is blocked outright by policy."""

    def __init__(self, decision: PolicyDecision):
        super().__init__(decision.reason)
        self.decision = decision


@dataclass
class Policy:
    allowed_domains: list[str]
    allowed_actions: list[str]
    blocked_labels: list[str] = field(default_factory=list)
    approval_labels: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- load
    @staticmethod
    def load(path: str = DEFAULT_POLICY_PATH) -> "Policy":
        with open(path) as f:
            d = json.load(f)
        return Policy(
            allowed_domains=d["allowed_domains"],
            allowed_actions=d["allowed_actions"],
            blocked_labels=d.get("blocked_labels", []),
            approval_labels=d.get("approval_labels", []),
        )

    # -------------------------------------------------------------- checks
    def check_url(self, url: str) -> PolicyDecision:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return PolicyDecision("block", f"URL '{url}' has no host")
        for allowed in self.allowed_domains:
            allowed = allowed.lower()
            if host == allowed or host.endswith("." + allowed):
                return PolicyDecision("allow", f"domain '{host}' is allowlisted")
        return PolicyDecision("block", f"domain '{host}' is not on the allowlist {self.allowed_domains}")

    def check_action(self, action: str, label: str = "") -> PolicyDecision:
        if action not in self.allowed_actions:
            return PolicyDecision("block", f"action type '{action}' is not allowlisted")
        norm = _norm(label)
        for pat in self.blocked_labels:
            if _label_matches(norm, pat):
                return PolicyDecision("block", f"'{label}' matches blocked (irreversible) action '{pat}'")
        for pat in self.approval_labels:
            if _label_matches(norm, pat):
                return PolicyDecision("needs_approval", f"'{label}' matches risky action '{pat}'")
        return PolicyDecision("allow", "safe action")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _label_matches(norm_label: str, pattern: str) -> bool:
    # whole-word match so "Pay" blocks "Pay Now" but not "Paypal info page"
    return re.search(r"\b" + re.escape(pattern.lower()) + r"\b", norm_label) is not None


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_CARDLIKE = re.compile(r"\b(?:\d[ -]?){12,19}\b")            # card / account numbers
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)")
_BEARER = re.compile(r"(?i)\b(bearer|token|api[_-]?key|password|secret)\b\s*[:=]\s*\S+")
_ANTHROPIC_KEY = re.compile(r"sk-ant-[A-Za-z0-9_-]+")


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Scrub sensitive values from any string before it touches disk."""
    if not isinstance(text, str):
        return text
    for secret in extra_secrets:
        if secret:
            text = text.replace(str(secret), "[REDACTED:input]")
    text = _ANTHROPIC_KEY.sub("[REDACTED:api_key]", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    text = _EMAIL.sub("[REDACTED:email]", text)
    text = _SSN.sub("[REDACTED:ssn]", text)
    text = _CARDLIKE.sub("[REDACTED:number]", text)
    text = _PHONE.sub("[REDACTED:phone]", text)
    return text


def redact_obj(obj, extra_secrets: Iterable[str] = ()):
    """Recursively redact every string inside dicts/lists."""
    extra_secrets = list(extra_secrets)
    if isinstance(obj, str):
        return redact(obj, extra_secrets)
    if isinstance(obj, dict):
        return {k: redact_obj(v, extra_secrets) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, extra_secrets) for v in obj]
    return obj


if __name__ == "__main__":
    # Tiny self-demo: `python policy.py`
    p = Policy.load()
    demos = [
        ("url", "https://www.tacobell.com/food/specialties"),
        ("url", "https://evil.example.com/phish"),
        ("click", "Add to Order"),
        ("click", "Place Order"),
        ("click", "Remove item"),
        ("drag", "anything"),
    ]
    for kind, val in demos:
        d = p.check_url(val) if kind == "url" else p.check_action(kind, val)
        print(f"{kind:6} {val!r:45} -> {d.verdict:15} ({d.reason})")
    print()
    print(redact("call me at (408) 555-1234, card 4111 1111 1111 1111, jane@bank.com, api_key=abc123"))
