"""Run explicitly against the local API + worker; creates/deletes a synthetic account."""

import io
import os
import secrets
import uuid
import wave
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto("http://127.0.0.1:8000", wait_until="networkidle")
            expect(page.get_by_role("status")).not_to_have_text("Connecting…")
            expect(page.get_by_text("FAKE / TEST DOUBLE DEMO", exact=True)).to_be_visible()
            page.get_by_label("Email", exact=True).fill(f"browser-{uuid.uuid4().hex}@example.com")
            page.locator("#password").fill(secrets.token_urlsafe(24))
            page.get_by_role("button", name="Create account", exact=True).click()
            expect(page.get_by_role("status")).to_contain_text("Account created")
            page.get_by_role("button", name="Log in", exact=True).click()
            expect(page.locator("#workspace")).to_be_visible()
            page.locator("#consent").check()
            page.get_by_role("button", name="Agree & create practice", exact=True).click()
            expect(page.locator("#editor")).to_be_visible()
            page.get_by_label("Your draft / edited transcript", exact=True).fill("He go home.")
            page.get_by_role("button", name="Save draft", exact=True).click()
            expect(page.get_by_role("status")).to_contain_text("Draft saved")
            page.get_by_role("button", name="Confirm saved revision", exact=True).click()
            expect(page.get_by_role("status")).to_contain_text("confirmed")
            page.get_by_role("button", name="Queue FAKE grammar", exact=True).click()
            expect(page.locator("#feedback")).to_contain_text("He goes home.", timeout=15000)
            expect(page.locator("#feedback")).to_contain_text("FAKE grammar")
            # Create a second practice and exercise the private audio/ASR path.
            page.locator("#mode").select_option("reference_reading")
            page.locator("#reference").fill("She goes home.")
            page.get_by_role("button", name="Agree & create practice", exact=True).click()
            expect(page.locator("#revision")).to_contain_text("reference_reading")
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                audio.writeframes(b"\xe8\x03" * 1600)
            page.locator("#audio").set_input_files(
                {"name": "synthetic.wav", "mimeType": "audio/wav", "buffer": buffer.getvalue()}
            )
            page.get_by_role("button", name="Upload & queue FAKE speech analysis", exact=True).click()
            expect(page.locator("#feedback")).to_contain_text("FAKE pronunciation", timeout=15000)
            expect(page.locator("#transcript")).not_to_have_value("")
            for width in (1280, 390):
                page.set_viewport_size({"width": width, "height": 900})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            output = Path(os.environ.get("LOCALAPPDATA", ".")) / "Temp/opencode/week3-browser.png"
            output.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output), full_page=True)
            page.on("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Delete account", exact=True).click()
            expect(page.locator("#auth")).to_be_visible()
            assert not errors, errors
            print("PASS: browser account, typed grammar, audio/ASR, history, deletion, desktop/mobile overflow; no page errors")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
