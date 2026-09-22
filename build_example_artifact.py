"""
Builds ONE example artifact by hand, using the schema we just defined,
filled in with the real Crunchwrap Supreme + Nacho Fries order.

This isn't the discovery run yet (that's the LLM-driven version, next).
This is here so you can SEE what a filled-out artifact actually looks like
as real JSON, before we make an LLM generate one live.
"""

import json
from schema import Artifact, Step, Locator, Fallback, Checkpoint, InputParam, OutputField, now_iso

artifact = Artifact(
    artifact_id="taco_bell_order_checkout_v1",
    version="1.0.0",
    goal_description=(
        "Order one Crunchwrap Supreme with modifications and one Nacho Fries, "
        "then reach the checkout review screen (never complete purchase)."
    ),
    target_url="https://www.tacobell.com/order",
    created_at=now_iso(),

    inputs=[
        InputParam(
            name="crunchwrap_modifications",
            type="string",
            description="Comma-separated modifications, e.g. 'remove sour cream, add cheese'",
            required=True,
        ),
    ],

    outputs=[
        OutputField(
            name="order_total",
            type="string",
            description="The subtotal shown on the checkout review screen",
        ),
        OutputField(
            name="substitution_used",
            type="boolean",
            description="True if a fallback substitution was triggered during this run",
        ),
    ],

            steps=[
        Step(
            step_number=1,
            action="navigate",
            locator=None,
            description="Go to the Taco Bell ordering site",
        ),
        Step(
            step_number=2,
            action="click",
            locator=Locator(type="text", value="Specialties"),
            description="Navigate to the Specialties category",
        ),
        Step(
            step_number=3,
            action="click",
            locator=Locator(type="text", value="CUSTOMIZE"),
            description="Open customization for the Black Bean Crunchwrap Supreme",
        ),
        Step(
            step_number=4,
            action="click",
            locator=Locator(type="text", value="Fiesta Strips and Seasoned Rice"),
            description="Apply modification: Fiesta Strips and Seasoned Rice",
        ),
        Step(
            step_number=5,
            action="click",
            locator=Locator(type="text", value="Add to Order"),
            description="Add the customized Crunchwrap Supreme to the order",
        ),
        Step(
            step_number=6,
            action="click",
            locator=Locator(type="text", value="Nacho Fries"),
            description="Order Nacho Fries, or substitute Cheesy Roll Up if unavailable",
            fallback=Fallback(
                condition="item_unavailable",
                action="click",
                locator=Locator(type="text", value="Cheesy Roll Up"),
            ),
        ),
        Step(
            step_number=7,
            action="click",
            locator=Locator(type="text", value="Add to Cart"),
            description="Add Nacho Fries (or its substitute) to cart",
        ),
        Step(
            step_number=8,
            action="click",
            locator=Locator(type="text", value="View Cart"),
            description="Navigate to the cart",
        ),
        Step(
            step_number=9,
            action="click",
            locator=Locator(type="text", value="Checkout"),
            description="Proceed to checkout review screen",
        ),
    ],

    checkpoint=Checkpoint(
        locator=Locator(type="text", value="Order Review"),
        description="The checkout review screen is showing, confirming both items were added",
    ),
)

if __name__ == "__main__":
    output_path = "../example_artifact.json"
    with open(output_path, "w") as f:
        json.dump(artifact.to_dict(), f, indent=2)
    print(f"Artifact saved to {output_path}")
    print()
    print(json.dumps(artifact.to_dict(), indent=2))
