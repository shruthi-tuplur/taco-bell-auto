"""
Surface adapter for web apps, built on Playwright.

This is the "how we perceive and act on a surface" side of the seam.
Discovery and replay only ever call the small interface below
(navigate / find / click / type_text / press_enter / wait / settle /
get_accessibility_snapshot / screenshot / current_url / extract /
start_human_capture / stop_human_capture). A legacy-web or desktop adapter
would implement the same methods (see REPORT.md, "Heterogeneity").
"""

import time
from playwright.sync_api import sync_playwright
import re as _re

# Injected into every page. While a human is in control it reports what they
# click and which fields they edit. It NEVER reports typed values.
_HUMAN_CAPTURE_JS = """
(() => {
  if (window.__humanCaptureInstalled) return;
  window.__humanCaptureInstalled = true;
  const label = (el) => {
    const t = el.closest('button, a, [role], input, select, textarea, label') || el;
    return (t.getAttribute('aria-label') || t.getAttribute('placeholder') ||
            (t.innerText || '').trim().slice(0, 60) || t.tagName).replace(/\\s+/g, ' ');
  };
  // pointerdown (not click) so the event is sent BEFORE a link starts navigating away
  document.addEventListener('pointerdown', (e) => {
    try { window.__humanAction({kind: 'click', target: label(e.target), url: location.href}); } catch (_) {}
  }, true);
  document.addEventListener('change', (e) => {
    try { window.__humanAction({kind: 'edit_field', target: label(e.target), url: location.href}); } catch (_) {}
  }, true);
})();
"""


class PlaywrightPage:
    def __init__(self, headless=False, geolocation=None):
        self.playwright = sync_playwright().start()
        # Launch exactly ONE browser. (v1 launched a second one here that
        # was never used and never closed.)
        self.browser = self.playwright.chromium.launch(headless=headless)
        geo = geolocation or {"latitude": 37.3382, "longitude": -121.8863}
        self.context = self.browser.new_context(permissions=["geolocation"], geolocation=geo)
        self._capturing = False
        self._human_actions = []
        self.context.expose_binding("__humanAction", self._on_human_action)
        self.context.add_init_script(_HUMAN_CAPTURE_JS)
        self.page = self.context.new_page()
        # Navigations are recorded from the Python side too, so a human's
        # page change is captured even if the in-page event is lost mid-unload.
        self.page.on("framenavigated", self._on_navigated)
        self.actions_log = []
        self.recovered_events = []      # interstitials dismissed, retries that saved a step, etc.
        self.last_typed_field = None

    # ------------------------------------------------------------------
    # human action capture (used during escalation handoff)
    # ------------------------------------------------------------------
    def _on_human_action(self, source, payload):
        if self._capturing:
            payload = dict(payload)
            payload["at"] = time.strftime("%H:%M:%S")
            self._human_actions.append(payload)

    def _on_navigated(self, frame):
        if self._capturing and frame == self.page.main_frame:
            self._human_actions.append({"kind": "navigated", "url": frame.url, "at": time.strftime("%H:%M:%S")})

    def start_human_capture(self):
        self._human_actions = []
        self._capturing = True
        try:
            self.page.evaluate(_HUMAN_CAPTURE_JS)   # current document too, not just future ones
        except Exception:
            pass

    def stop_human_capture(self):
        self._capturing = False
        return list(self._human_actions)

    def current_url(self):
        return self.page.url

    def last_action(self):
        return self.actions_log[-1] if self.actions_log else ""

    def _pick_best_candidate(self, candidate_names, target_value):
        """
        Given a list of accessible-name strings that all substring-matched 
        target_value, pick the index of the BEST match:
        1. Prefer an exact case-insensitive match
        2. Otherwise prefer the SHORTEST candidate (closest length to target,
        since a decoy like "Diablo Ranch Steak Nacho Fries" is way longer
        than the plain "Large Nacho Fries" we actually want)
        """
        target_norm = target_value.strip().lower()

        # first pass: exact match wins, no contest
        for i, name in enumerate(candidate_names):
            if name.strip().lower() == target_norm:
                return i

        # second pass: no exact match, so pick whichever candidate is
        # closest in length to the target (shortest "extra stuff" wins)
        best_i = 0
        best_diff = None
        for i, name in enumerate(candidate_names):
            diff = abs(len(name.strip()) - len(target_value.strip()))
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_i = i
        return best_i

    def _find_scoped_button(self, product_name, button_text):
        """
        Two card patterns exist on this site:
        1. "Specialty" items: the button's OWN accessible name IS the
           full product name, and its visible text is the action
           ("Add to Order" / "Customize"). e.g. Black Bean Crunchwrap
           Supreme.
        2. "Value/combo" items: the button's accessible name is just the
           generic action text ("Add to Order") with no product name at
           all -- it's just positioned right after the product's heading
           in DOM order. e.g. Nacho Fries, Large Nacho Fries.
        Try pattern 1 first, then fall back to pattern 2.
        """
        # Pattern 1: button's accessible name IS the product name
        try:
            named = self.page.get_by_role("button", name=product_name, exact=False)
            count = named.count()
            for i in range(count):
                candidate = named.nth(i)
                if button_text.lower() in candidate.inner_text().lower():
                    return candidate
        except Exception:
            pass

        # Pattern 2: generic button with no product name in its own
        # accessible name, positioned right after the product's
        # heading/link in DOM order
        clean_name = (
            product_name.replace("®", "").replace("™", "").replace("©", "").strip()
        )
        try:
            product_el = self.page.get_by_text(clean_name, exact=False).first
            following_button = product_el.locator(
                f"xpath=following::button[normalize-space(.)='{button_text}'][1]"
            )
            if following_button.is_visible(timeout=400):
                return following_button
        except Exception:
            pass

        return None

    def navigate(self, url):
        self.page.goto(url)
        self.page.wait_for_load_state("networkidle")

        # Known interstitial: cookie consent banner. Dismissing it is a
        # "recoverable condition" and is reported as such in the result.
        try:
            agree_button = self.page.get_by_role("button", name="AGREE", exact=False)
            agree_button.first.click(timeout=5000)
            self.actions_log.append("CLICKED cookie banner button: AGREE")
            self.recovered_events.append({"kind": "dismissed_interstitial", "detail": "cookie consent banner"})
            self.page.wait_for_timeout(1000)
        except Exception as e:
            self.actions_log.append(f"Cookie banner click attempt failed or not found: {e}")

        self.actions_log.append(f"NAVIGATE -> {url}")

    def find(self, locator, timeout_ms=5000):
        """True if an element with this text becomes visible within timeout.

        v1 used `is_visible(timeout=...)`, but Playwright ignores that
        timeout (is_visible never waits), so every find() was an instant
        single check. That was the real cause of the post-handoff
        checkpoint flake. This version actually polls.
        """
        value = locator["value"] if isinstance(locator, dict) else locator.value
        deadline = time.time() + timeout_ms / 1000
        while True:
            try:
                matches = self.page.get_by_text(value, exact=False)
                for i in range(min(matches.count(), 25)):
                    if matches.nth(i).is_visible():
                        self.actions_log.append(f"FIND '{value}' -> found")
                        return True
            except Exception:
                pass
            if time.time() >= deadline:
                self.actions_log.append(f"FIND '{value}' -> NOT FOUND")
                return False
            self.page.wait_for_timeout(250)

    def click(self, locator):
        raw_value = locator["value"]
        button_text = locator.get("button_text")
        if not raw_value:
            raise Exception("Cannot click: locator value is empty (likely an unlabeled icon button)")

        def normalize(s, ignore_case=False):
            s = (
                s.replace("\u2019", "'").replace("\u2018", "'")
                 .replace("\u201c", '"').replace("\u201d", '"')
            )
            s = _re.sub(r"\s+", " ", s).strip()
            return s.lower() if ignore_case else s

        if button_text:
            match = self._find_scoped_button(raw_value, button_text)

            retries = 0
            while match is None and retries < 5:
                self.page.wait_for_timeout(400)
                match = self._find_scoped_button(raw_value, button_text)
                retries += 1

            if match is not None:
                if retries:
                    self.recovered_events.append({"kind": "retried_slow_render",
                                                  "detail": f"'{raw_value}' appeared after {retries} retries"})
                match.click()
                self._settle()
                self.last_typed_field = None
                self.actions_log.append(f"CLICK (scoped button_text match) '{raw_value}' -> '{button_text}'")
                return True

            raise Exception(
                f"Could not find a button with text '{button_text}' for "
                f"product '{raw_value}' via either accessible-name or "
                f"DOM-proximity matching, even after retrying."
            )

        # Attempt 1: match by ACCESSIBLE NAME (same string shown in the
        # accessibility tree the LLM reasons from) -- this is more reliable
        # than visible text for buttons whose rendered text differs from
        # their full aria-label (e.g. "Add Ons, group of 11 options, ...").
        # Exact name first, THEN substring. (Substring-only matching picked
        # "Start Your Order" when the step asked for "Your Order", because
        # it came first in the DOM. Found by the local smoke test.)
        for exact in (True, False):
            for role in ("button", "link"):
                try:
                    matches = self.page.get_by_role(role, name=raw_value, exact=exact)
                    for i in range(min(matches.count(), 10)):
                        role_match = matches.nth(i)
                        if role_match.is_visible():
                            role_match.click(force=True, timeout=8000)
                            self._settle()
                            self.last_typed_field = None
                            kind = "exact accessible name" if exact else "accessible name contains"
                            self.actions_log.append(f"CLICK ({kind}, {role}) '{raw_value}'")
                            return
                except Exception:
                    continue

        # Attempt 2: manual scan of visible TEXT (handles quote/whitespace
        # mismatches, and cases where accessible name matching misses).
        candidates = self.page.locator("button, a")
        count = candidates.count()
        short_raw = " ".join(raw_value.split()[:3])

        match = None
        for ignore_case in (False, True):
            for target_raw in (raw_value, short_raw):
                target = normalize(target_raw, ignore_case)
                for i in range(count):
                    el = candidates.nth(i)
                    try:
                        text = normalize(el.inner_text(timeout=1000), ignore_case)
                    except Exception:
                        continue
                    if target in text and el.is_visible():
                        match = el
                        break
                if match:
                    break
            if match:
                break

        if match is not None:
            match.click(force=True, timeout=8000)
            self._settle()
            self.last_typed_field = None
            self.actions_log.append(f"CLICK (text) '{raw_value}'")
            return

        # Attempt 3: positional fallback near the last typed field. Only
        # allowed on the click IMMEDIATELY after typing (last_typed_field is
        # cleared by every successful click), e.g. an unlabeled icon button
        # next to a just-filled search box. Without that rule this path
        # silently clicked the wrong button later in a flow.
        if getattr(self, "last_typed_field", None) is not None:
            nearby_button = self.last_typed_field.locator("xpath=following::button[1]")
            nearby_button.click(force=True, timeout=8000)
            self._settle()
            self.last_typed_field = None
            self.actions_log.append(f"CLICK (LOW CONFIDENCE positional fallback next to last typed field) "
                                    f"for intended '{raw_value}'")
            return

        raise Exception(f"No visible button/link match found for '{raw_value}'")

    def _settle(self):
        try:
            self.page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        self.page.wait_for_timeout(300)

    def settle(self, max_seconds=8):
        """Condition-based wait used for recorded 'wait' steps: wait until
        the network goes idle AND common loading indicators disappear,
        capped at max_seconds. Replaces v1's fixed 2 second sleep."""
        deadline = time.time() + max_seconds
        try:
            self.page.wait_for_load_state("networkidle", timeout=max_seconds * 1000)
        except Exception:
            pass
        loading = ("Finding Stores", "Loading")
        while time.time() < deadline:
            busy = False
            for word in loading:
                try:
                    if self.page.get_by_text(word, exact=False).first.is_visible():
                        busy = True
                except Exception:
                    pass
            if not busy:
                break
            self.page.wait_for_timeout(300)
        self.page.wait_for_timeout(500)
        self.actions_log.append("SETTLE (network idle, no loading indicator)")

    def extract(self, extractor):
        """Named output extractors an artifact can reference."""
        if extractor == "subtotal":
            return self.get_order_total()
        if extractor == "current_url":
            return self.page.url
        raise ValueError(f"Unknown extractor '{extractor}'")

    def get_order_total(self):
        try:
            full_text = self.page.inner_text("body")
            match = _re.search(r"Subtotal\s*\$?\s*([\d,]+\.\d{2})", full_text, _re.IGNORECASE)
            if match:
                return f"${match.group(1)}"
        except Exception:
            pass
        try:
            return self.page.get_by_text("$", exact=False).first.inner_text()
        except Exception:
            return "unknown"

    def screenshot(self, path="failure_screenshot.png"):
        self.page.screenshot(path=path)
        self.actions_log.append(f"SCREENSHOT saved -> {path}")

    def get_accessibility_snapshot(self) -> str:
        try:
            snapshot = self.page.locator("body").aria_snapshot()
            lines = snapshot.split("\n")

            # Drop the repetitive footer (contentinfo) section -- it's
            # identical on every page and just adds noise/length.
            footer_start = next((i for i, l in enumerate(lines) if l.strip().startswith("- contentinfo:")), None)
            if footer_start is not None:
                lines = lines[:footer_start]

            return "\n".join(lines[:500])
        except Exception as e:
            return f"(could not get accessibility snapshot: {e})"

    def wait(self, seconds=2):
        self.page.wait_for_timeout(seconds * 1000)
        self.actions_log.append(f"WAIT {seconds}s")

    def type_text(self, locator, text):
        value = locator["value"]
        candidates = self.page.get_by_placeholder(value, exact=False)
        count = candidates.count()

        target = None
        for i in range(count):
            candidate = candidates.nth(i)
            if candidate.is_visible():
                target = candidate
                break

        if target is None:
            raise Exception(f"No VISIBLE element found with placeholder '{value}' (found {count} total, all hidden)")

        target.fill(text)
        self.last_typed_field = target
        self.actions_log.append(f"TYPE '{text}' into field '{value}'")

    def press_enter(self):
        self.page.keyboard.press("Enter")
        self.actions_log.append("PRESSED Enter key")

    def close(self, pause_before_close=False):
        if pause_before_close:
            input("Press ENTER in the terminal to close the browser...")
        try:
            self.context.close()
        except Exception:
            pass
        self.browser.close()
        self.playwright.stop()