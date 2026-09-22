"""
Artifact schema — the typed, versioned structure that represents a recorded,
replayable capability (e.g. "order a Crunchwrap Supreme and Nacho Fries,
reach checkout").

This is the "recipe format." The discovery run WRITES one of these.
The replay engine READS one of these. Neither of them improvise the shape —
this file is the single source of truth for what a valid artifact looks like.
"""

from dataclasses import dataclass, field
from typing import Literal, Optional
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Locator: HOW we find an element on the page.
# We only support "by visible text / accessible name" for now — this is the
# robust strategy we chose (see decision log): it survives layout changes,
# unlike CSS position or pixel coordinates.
# ---------------------------------------------------------------------------
@dataclass
class Locator:
    type: Literal["text", "role"]   # "text" = match by visible text/label
                                      # "role" = match by accessibility role (button, link, etc)
    value: str                       # e.g. "Nacho Fries" or "Add to Cart"


# ---------------------------------------------------------------------------
# Fallback: what to do if the primary locator can't be found.
# This is how "item unavailable" becomes a handled case instead of a crash.
# ---------------------------------------------------------------------------
@dataclass
class Fallback:
    condition: str                   # human-readable reason this fallback exists,
                                      # e.g. "item_unavailable"
    action: Literal["click", "type", "navigate"]
    locator: Locator


# ---------------------------------------------------------------------------
# Step: ONE action in the recorded flow.
# This is the core unit of the artifact — a list of these, in order, IS the
# artifact's "steps" field.
# ---------------------------------------------------------------------------
@dataclass
class Step:
    step_number: int
    action: Literal["click", "type", "navigate"]
    locator: Optional[Locator]       # None for "navigate" steps that don't click anything
    description: str                 # human-readable explanation, for a reviewer
    input_value: Optional[str] = None    # e.g. text to type, if action == "type"
    fallback: Optional[Fallback] = None  # optional — only steps that need one have one


# ---------------------------------------------------------------------------
# Checkpoint: how we know the flow actually succeeded, not just "ran without
# crashing." This is checked at the END of a replay.
# ---------------------------------------------------------------------------
@dataclass
class Checkpoint:
    locator: Locator                 # something that should be visible/present
                                      # if the flow actually reached the right state
    description: str                 # e.g. "Checkout review screen is showing order total"


# ---------------------------------------------------------------------------
# InputParam / OutputField: the artifact's "function signature."
# This is what makes it a reusable CAPABILITY an agent can call, not just a
# one-off recording of exactly what happened this one time.
# ---------------------------------------------------------------------------
@dataclass
class InputParam:
    name: str                        # e.g. "crunchwrap_modifications"
    type: Literal["string", "number", "boolean"]
    description: str
    required: bool = True


@dataclass
class OutputField:
    name: str                        # e.g. "order_total"
    type: Literal["string", "number", "boolean"]
    description: str


# ---------------------------------------------------------------------------
# The artifact itself — everything above, assembled + versioned.
# ---------------------------------------------------------------------------
@dataclass
class Artifact:
    artifact_id: str                 # e.g. "taco_bell_order_checkout_v1"
    version: str                     # e.g. "1.0.0" — bump this if the recorded flow changes
    goal_description: str            # the natural-language goal this artifact fulfills
    target_url: str                  # entry point this artifact was recorded against
    created_at: str                  # ISO timestamp of when discovery produced this
    inputs: list[InputParam]
    outputs: list[OutputField]
    steps: list[Step]
    checkpoint: Checkpoint

    def to_dict(self) -> dict:
        """Convert to a plain dict for JSON serialization."""
        import dataclasses
        return dataclasses.asdict(self)


def now_iso() -> str:
    """Helper so discovery.py doesn't need to import datetime itself."""
    return datetime.now(timezone.utc).isoformat()
