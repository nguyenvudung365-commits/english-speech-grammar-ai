"""Optional live-browser smoke. Run against this local demo only; creates/deletes its account.

Requires migrated PostgreSQL and running API. Launches worker --once in a separate process.
All input is synthetic, and all language outputs remain FAKE.
"""
import os
import secrets
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright


def main():
    base = os.environ.get("BROWSER_BASE_URL", "http://127.0.0.1:8000")
    email = f"browser-{uuid4().hex}@example.com"
    password = secrets.token_urlsafe(24)
    options = {"headless": True}
    if os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
        options["executable_path"] = os.environ["PLAYWRIGHT_CHROMIUM_EXECUTABLE"]
    Path(".claude").mkdir(exist_ok=True)
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(**options)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base)
        page.wait_for_load_state("networkidle")
        expect(page.get_by_text("FAKE / TEST DOUBLE DEMO", exact=True)).to_be_visible()
        page.get_by_label("Email", exact=True).fill(email)
        page.get_by_label("Password", exact=False).fill(password)
        page.get_by_role("button", name="Create account", exact=True).click()
        expect(page.locator("#status")).to_contain_text("Account created")
        page.get_by_role("button", name="Log in", exact=True).click()
        expect(page.locator("#workspace")).to_be_visible()
        page.locator("#consent").check()
        page.get_by_role("button", name="Agree & create practice").click()
        expect(page.locator("#editor")).to_be_visible()
        page.get_by_label("Your draft / edited transcript").fill("He go home.")
        expect(page.get_by_role("button", name="Queue FAKE grammar", exact=True)).to_be_disabled()
        page.get_by_role("button", name="Save draft", exact=True).click()
        expect(page.locator("#status")).to_contain_text("Draft saved")
        page.get_by_role("button", name="Confirm saved revision", exact=True).click()
        expect(page.get_by_role("button", name="Queue FAKE grammar", exact=True)).to_be_enabled()
        page.get_by_role("button", name="Queue FAKE grammar", exact=True).click()
        expect(page.locator("#job-list")).to_contain_text("queued")
        subprocess.run([sys.executable, "-m", "src.worker.runner", "--once"], check=True)
        page.get_by_role("button", name="Refresh report", exact=True).click()
        expect(page.locator("#feedback")).to_contain_text("He goes home.")
        expect(page.locator("#job-list")).to_contain_text("succeeded")
        page.screenshot(path=".claude/browser-desktop.png", full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=".claude/browser-mobile.png", full_page=True)
        # User content is rendered as text, not executable HTML.
        page.get_by_label("Your draft / edited transcript").fill('<img src=x onerror="alert(1)">')
        page.get_by_role("button", name="Save draft", exact=True).click()
        expect(page.locator("#revision")).to_contain_text("revision 2")
        expect(page.get_by_role("button", name="Queue FAKE grammar", exact=True)).to_be_disabled()
        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="Delete this practice", exact=True).click()
        expect(page.locator("#status")).to_contain_text("Practice deleted")
        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="Delete account", exact=True).click()
        expect(page.locator("#status")).to_contain_text("Account deleted")
        expect(page.locator("#auth")).to_be_visible()
        assert errors == [], errors
        browser.close()
    print("PASS: live browser register/login/consent/typed draft/confirmation/queued job/separate worker/fake result/stale invalidation/delete; desktop + 390px screenshots; zero page errors")


if __name__ == "__main__":
    main()
