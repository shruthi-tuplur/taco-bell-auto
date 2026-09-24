"""
Artifact schema: the typed, versioned structure that represents a recorded,
replayable capability (e.g. "order a customized Black Bean Crunchwrap Supreme
plus a side, and reach the cart review screen").

This is the "recipe format." The discovery run WRITES one of these.
The replay engine READS one of these. Neither of them improvise the shape:
this file is the single source of truth for what a valid artifact looks like.

Design notes (see REPORT.md, "Artifact schema"):
  - SCHEMA_VERSION versions the *format*. Artifact.version versions the
    *capability* (bump it when the recorded flow changes).
  - Steps may contain {{param}} placeholders in locator values and
    input_value. Replay fills them from the caller's typed inputs, which is
    what turns one recording into a reusable capability.
  - Anything the replay engine needs to know about a specific app lives in
    the artifact (checkpoint, verification, outcome rules), never hardcoded
    in replay.py. That keeps the engine generic.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

SCHEMA_VERSION = "2.0"

ActionType = Literal["click", "type", "navigate", "wait", "press_enter"]
ParamType = Literal["string", "number", "boolean"]

PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


# ---------------------------------------------------------------------------
# Locator: HOW we find an element on the surface.
#   type="text"  -> match by visible text
#   type="role"  -> match by accessible name (what a screen reader announces)
# Accessible names are the primary strategy because they survive CSS/layout
# changes and exist on legacy web apps AND native desktop apps (UIA / AX),
# unlike CSS selectors or pixel coordinates.
#
# button_text scopes a generic control ("Add to Order") to the product card
# named by `value`, for pages with many identical buttons.
# ---------------------------------------------------------------------------
@dataclass
class Locator:
    type: Literal["text", "role"]
    value: str
    button_text: Optional[str] = None


# ---------------------------------------------------------------------------
# Fallback: an alternate target to try if the primary one is missing.
# `condition` is the human-readable reason it exists. Using a fallback makes
# the run a business_outcome ("substitution"), never a silent success.
# ---------------------------------------------------------------------------
@dataclass
class Fallback:
    locator: Locator
    condition: str = "primary_target_missing"
    action: Literal["click"] = "click"


# ---------------------------------------------------------------------------
# MissingTargetRule: how to classify "the target isn't there".
# This is the core of separating business outcomes from hard failures.
#   - If `page_anchor` IS visible, we are provably on the right screen and it
#     is healthy, so a missing target means the THING doesn't exist
#     (e.g. item not on this store's menu). -> business_outcome
#   - If `page_anchor` is NOT visible, the page itself is wrong/broken.
#     -> hard failure (escalate to a human)
# ---------------------------------------------------------------------------
@dataclass
class MissingTargetRule:
    outcome_code: str                # e.g. "item_unavailable"
    page_anchor: Locator             # proof the screen is healthy
    message: str                     # may contain {{params}}


# ---------------------------------------------------------------------------
# OutcomeSignature: a known page state that means "legitimate business
# answer", checked when a step fails, e.g. text "No stores found".
# ---------------------------------------------------------------------------
@dataclass
class OutcomeSignature:
    outcome_code: str
    locator: Locator
    message: str


@dataclass
class Step:
    step_number: int
    action: ActionType
    locator: Optional[Locator]       # None for wait / navigate / press_enter
    description: str                 # why this step exists (from the LLM's reasoning)
    input_value: Optional[str] = None     # text to type; may be "{{param}}"
    fallback: Optional[Fallback] = None
    on_missing: Optional[MissingTargetRule] = None


@dataclass
class Checkpoint:
    locator: Locator                 # must be visible at the end
    description: str
    must_contain: list[str] = field(default_factory=list)
    # Extra texts that must ALSO be visible (may use {{params}}). Catches
    # "the cart page loaded but an item silently never got added".


@dataclass
class InputParam:
    name: str
    type: ParamType
    description: str
    required: bool = True
    default: Optional[Any] = None
    sensitive: bool = False          # if True, value is masked in all logs


@dataclass
class OutputField:
    name: str
    type: ParamType
    description: str
    extractor: str = ""              # name of a registered extractor, e.g. "subtotal"


@dataclass
class Provenance:
    discovered_by: str               # e.g. "llm:claude-sonnet-4-5"
    discovery_run_id: str
    reviewed_by: Optional[str] = None
    notes: str = ""


@dataclass
class Artifact:
    artifact_id: str
    version: str
    goal_description: str
    target_url: str
    created_at: str
    inputs: list[InputParam]
    outputs: list[OutputField]
    steps: list[Step]
    checkpoint: Checkpoint
    schema_version: str = SCHEMA_VERSION
    status: Literal["draft", "approved"] = "draft"
    app: dict = field(default_factory=dict)   # {"vendor": ..., "product": ..., "surface": "web"}
    outcome_signatures: list[OutcomeSignature] = field(default_factory=list)
    provenance: Optional[Provenance] = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Artifact":
        return artifact_from_dict(d)


# ---------------------------------------------------------------------------
# Deserialization + validation. Replay never trusts raw JSON: it loads it
# into these types first so a malformed artifact fails loudly up front,
# not halfway through a live session.
# ---------------------------------------------------------------------------
class ArtifactValidationError(ValueError):
    pass


def _loc(d: Optional[dict]) -> Optional[Locator]:
    if d is None:
        return None
    return Locator(type=d.get("type", "text"), value=d["value"], button_text=d.get("button_text"))


def artifact_from_dict(d: dict) -> Artifact:
    try:
        steps = []
        for s in d["steps"]:
            fb = s.get("fallback")
            om = s.get("on_missing")
            steps.append(Step(
                step_number=s["step_number"],
                action=s["action"],
                locator=_loc(s.get("locator")),
                description=s.get("description", ""),
                input_value=s.get("input_value"),
                fallback=Fallback(locator=_loc(fb["locator"]),
                                  condition=fb.get("condition", "primary_target_missing"))
                if fb else None,
                on_missing=MissingTargetRule(outcome_code=om["outcome_code"],
                                             page_anchor=_loc(om["page_anchor"]),
                                             message=om.get("message", ""))
                if om else None,
            ))
        cp = d["checkpoint"]
        prov = d.get("provenance")
        art = Artifact(
            artifact_id=d["artifact_id"],
            version=d["version"],
            goal_description=d["goal_description"],
            target_url=d["target_url"],
            created_at=d["created_at"],
            inputs=[InputParam(**i) for i in d.get("inputs", [])],
            outputs=[OutputField(**o) for o in d.get("outputs", [])],
            steps=steps,
            checkpoint=Checkpoint(locator=_loc(cp["locator"]), description=cp.get("description", ""),
                                  must_contain=cp.get("must_contain", [])),
            schema_version=d.get("schema_version", "1.0"),
            status=d.get("status", "draft"),
            app=d.get("app", {}),
            outcome_signatures=[OutcomeSignature(outcome_code=o["outcome_code"], locator=_loc(o["locator"]),
                                                 message=o.get("message", ""))
                                for o in d.get("outcome_signatures", [])],
            provenance=Provenance(**prov) if prov else None,
        )
    except (KeyError, TypeError) as e:
        raise ArtifactValidationError(f"Malformed artifact: {e!r}") from e

    validate_artifact(art)
    return art


def validate_artifact(art: Artifact) -> None:
    if art.schema_version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        raise ArtifactValidationError(
            f"Artifact schema_version {art.schema_version} is not compatible with engine {SCHEMA_VERSION}")
    declared = {p.name for p in art.inputs}
    for step in art.steps:
        if step.action in ("click",) and step.locator is None:
            raise ArtifactValidationError(f"Step {step.step_number}: click needs a locator")
        if step.action == "type" and (step.locator is None or step.input_value is None):
            raise ArtifactValidationError(
                f"Step {step.step_number}: type needs a locator AND an input_value (what to type)")
        for text in _texts_in_step(step):
            for name in PLACEHOLDER_RE.findall(text):
                if name not in declared:
                    raise ArtifactValidationError(
                        f"Step {step.step_number} uses {{{{{name}}}}} but no input named '{name}' is declared")


def _texts_in_step(step: Step) -> list[str]:
    out = []
    if step.locator:
        out += [step.locator.value, step.locator.button_text or ""]
    if step.input_value:
        out.append(step.input_value)
    if step.fallback:
        out.append(step.fallback.locator.value)
    if step.on_missing:
        out.append(step.on_missing.message)
    return out


def fill(template: Optional[str], params: dict) -> Optional[str]:
    """Replace {{name}} placeholders with caller-supplied values."""
    if template is None:
        return None
    return PLACEHOLDER_RE.sub(lambda m: str(params[m.group(1)]), template)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
