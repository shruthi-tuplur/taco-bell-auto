from playwright.sync_api import sync_playwright
import re as _re


class PlaywrightPage:
    def __init__(self, headless=False):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=headless)
        context = self.browser.new_context(
            permissions=["geolocation"],
            geolocation={"latitude": 37.3382, "longitude": -121.8863},
        )
        self.page = context.new_page()
        self.actions_log = []
        self.last_typed_field = None
        self.browser = self.playwright.chromium.launch(
            headless=headless
        )

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

        try:
            agree_button = self.page.get_by_role("button", name="AGREE", exact=False)
            agree_button.first.click(timeout=5000)
            self.actions_log.append("CLICKED cookie banner button: AGREE")
            self.page.wait_for_timeout(1000)
        except Exception as e:
            self.actions_log.append(f"Cookie banner click attempt failed or not found: {e}")

        self.actions_log.append(f"NAVIGATE -> {url}")

    def find(self, locator):
        value = locator["value"]
        try:
            element = self.page.get_by_text(value, exact=False)
            is_visible = element.first.is_visible(timeout=5000)
            self.actions_log.append(f"FIND '{value}' -> {'found' if is_visible else 'NOT FOUND'}")
            return is_visible
        except Exception:
            self.actions_log.append(f"FIND '{value}' -> NOT FOUND")
            return False

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
                match.click()
                self._settle()
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
        for role in ("button", "link"):
            try:
                role_match = self.page.get_by_role(role, name=raw_value, exact=False).first
                if role_match.is_visible(timeout=2000):
                    role_match.click(force=True, timeout=8000)
                    self._settle()
                    self.actions_log.append(f"CLICK (accessible name, {role}) '{raw_value}'")
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
            self.actions_log.append(f"CLICK (text) '{raw_value}'")
            return

        # Attempt 3: positional fallback near the last typed field --
        # only meaningful right after typing into something (e.g. an
        # unlabeled icon button next to a just-filled search box).
        if getattr(self, "last_typed_field", None) is not None:
            nearby_button = self.last_typed_field.locator("xpath=following::button[1]")
            nearby_button.click(force=True, timeout=8000)
            self._settle()
            self.actions_log.append(f"CLICK (positional fallback near last field) for intended '{raw_value}'")
            return

        raise Exception(f"No visible button/link match found for '{raw_value}'")

    def _settle(self):
        try:
            self.page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        self.page.wait_for_timeout(300)

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
        self.browser.close()
        self.playwright.stop()