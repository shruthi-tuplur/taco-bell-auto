from replay import ReplayEngine, load_artifact
from playwright_page import PlaywrightPage

artifact = load_artifact("../example_artifact.json")

page = PlaywrightPage(headless=False)
engine = ReplayEngine(page)
result = engine.run(artifact, inputs={"crunchwrap_modifications": "remove sour cream"})

if result.status == "failure":
    page.screenshot(path="../evidence/failure_screenshot.png")
    print("Screenshot saved to evidence/failure_screenshot.png")

print(f"RESULT: {result.status}")
print(f"MESSAGE: {result.message}")
print(f"OUTPUTS: {result.outputs}")

page.close(pause_before_close=False)