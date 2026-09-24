"""
Wraps the LLM API call. Isolated in its own file so it's obvious, at a
glance, that ONLY discovery.py touches this -- replay.py never imports it.
"""

import os
import re
import json
from anthropic import Anthropic

try:  # optional: read ANTHROPIC_API_KEY from a local .env file (never committed)
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """You are controlling a web browser to complete an online food order.

RULES:
- Respond with EXACTLY ONE JSON object per turn, nothing else -- no explanation, no markdown formatting, no reasoning text before or after the JSON.
- For click/navigate: {"action": "click"|"navigate", "target": {"type": "text", "value": "<visible text of the element to interact with>"}, "reasoning": "<one short sentence>"}
- For typing into a field: {"action": "type", "target": {"type": "text", "value": "<visible placeholder or label of the INPUT FIELD>"}, "text": "<the actual text to type into it>", "reasoning": "<one short sentence>"}
- If you believe the goal has been fully achieved, respond instead with:
  {"action": "done", "reasoning": "<why you believe the goal is complete>"}
- If an item you need appears unavailable, look for a reasonable substitute before giving up.
- Never attempt to enter payment information or complete a real purchase.
- Only take ONE action per response. You will be shown the updated page after each action.
- When a location/city/store search is needed, type the location given in the GOAL.
- Icon-only buttons (like a search/magnifying-glass icon) usually have no visible text, but DO have an accessible name in the page tree (e.g. "Search"). For these, use {"action": "click", "target": {"type": "role", "value": "<accessible name from the tree>"}}.
- After clicking to submit a location search, a list of matching store results usually appears. Click the first one.
- ALWAYS use the SHORTEST distinctive word or phrase as the locator value, never a full sentence or paragraph — e.g. use "Drive-Thru" not "Drive-Thru. Open til...", use "Now" not "Now. Wait time: 5-8 mins...". If a button's visible text is long, pick out just the first 1-3 words that make it unique.
- If the page shows a loading indicator (e.g. "Finding Stores...", a spinner, "Loading"), do NOT try to click it — it is not interactive. Instead respond with {"action": "wait", "reasoning": "<why>"} and you will be shown the page again after a short pause.
- Some pages have multiple buttons with the same generic label (like "Customize" or "Add to Order") — one per product. When this is the case, use {"action": "click", "target": {"type": "role", "value": "<exact product name>", "button_text": "Customize"}} to specify BOTH the product's accessible name and which button on that product you want.
"""


def decide_next_action(goal: str, accessibility_tree: str, steps_so_far: list[str]) -> dict:
    history_text = "\n".join(steps_so_far) if steps_so_far else "(no steps taken yet)"

    user_message = f"""GOAL: {goal}

STEPS TAKEN SO FAR:
{history_text}

CURRENT PAGE (accessibility tree):
{accessibility_tree}

What is the next single action?"""

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw_text = response.content[0].text.strip()

    # Look for a fenced ```json ... ``` block ANYWHERE in the response
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
    if fence_match:
        raw_text = fence_match.group(1)
    else:
        # No fence -- fall back to grabbing the first { ... last } as JSON,
        # which handles cases where the model adds prose before/after with no fence at all.
        start, end = raw_text.find("{"), raw_text.rfind("}")
        if start != -1 and end != -1 and end > start:
            raw_text = raw_text[start:end + 1]

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return {"action": "error", "reasoning": f"Could not parse LLM response: {raw_text}"}