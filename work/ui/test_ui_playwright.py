"""Automated Playwright UI Test Suite for Music Video Generation Studio.
Launches the UI server on port 7865, connects via headless Chrome,
tests tab navigation, anchor studio image loading, timeline clicks,
captures browser console logs, and saves screenshots to the artifacts folder.
"""

from __future__ import annotations

import os
import sys
import time
import json
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer

# Ensure project imports
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT / "work" / "ui"))
from server import StudioRequestHandler

from playwright.sync_api import sync_playwright

TEST_HOST = "127.0.0.1"
TEST_PORT = 8088
ARTIFACT_DIR = Path(r"C:\Users\bugbl\.gemini\antigravity\brain\11e6839d-e38f-40d5-8ee5-0f5c88dbb4a5")
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

def run_test():
    print(f"[Test] Starting UI server on {TEST_HOST}:{TEST_PORT}...")
    server = ThreadingHTTPServer((TEST_HOST, TEST_PORT), StudioRequestHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.5)

    console_logs = []
    page_errors = []

    print("[Test] Launching Headless Chrome via Playwright...")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME_PATH, headless=True)
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()

        page.on("console", lambda msg: console_logs.append(f"[{msg.type}] {msg.text}"))
        page.on("pageerror", lambda err: page_errors.append(str(err)))

        url = f"http://127.0.0.1:{TEST_PORT}/"
        print(f"[Test] Navigating to {url}...")
        page.goto(url, wait_until="networkidle")

        # Wait for project data to be loaded
        page.wait_for_selector("#waveformCanvas")
        time.sleep(1.0)

        # 1. Capture Timeline Tab Screenshot
        timeline_ss = ARTIFACT_DIR / "test_timeline.png"
        page.screenshot(path=str(timeline_ss))
        print(f"[Test] Captured Timeline screenshot: {timeline_ss}")

        # 2. Test Timeline Ruler Click / Seeking
        print("[Test] Testing click on timeline ruler...")
        canvas = page.locator("#waveformCanvas")
        box = canvas.bounding_box()
        if box:
            # Click at x = 300, y = 12 (top ruler)
            page.mouse.click(box["x"] + 300, box["y"] + 12)
            time.sleep(0.3)
            timecode = page.locator("#timecodeDisplay").inner_text()
            print(f"[Test] Timecode after ruler click: {timecode}")

        # 3. Click the Anchors Tab
        print("[Test] Switching to Anchors tab...")
        page.click("button[data-tab='anchors']")
        page.wait_for_selector("#tab-anchors", state="visible")
        try:
            page.wait_for_function(
                "() => { const imgs = Array.from(document.querySelectorAll('#tab-anchors img')); return imgs.length >= 14 && imgs.every(i => i.complete); }",
                timeout=8000
            )
        except Exception:
            pass
        time.sleep(0.5)

        # Check anchor cards count
        cards = page.locator(".anchor-card")
        card_count = cards.count()
        print(f"[Test] Found {card_count} anchor cards on Anchors tab.")

        # Check each anchor card image status
        anchor_results = []
        for i in range(card_count):
            card = cards.nth(i)
            aid = card.locator(".anchor-id").inner_text()
            img = card.locator("img")
            has_img = img.count() > 0
            is_loaded = False
            img_src = ""
            if has_img:
                img_src = img.get_attribute("src") or ""
                # Evaluate whether naturalWidth > 0
                is_loaded = page.evaluate("el => el.naturalWidth > 0", img.element_handle())
            missing = card.locator(".missing-placeholder")
            has_missing = missing.count() > 0

            anchor_results.append({
                "aid": aid,
                "has_img": has_img,
                "loaded": is_loaded,
                "src": img_src,
                "missing_badge": has_missing
            })
            status = "LOADED ✅" if is_loaded else ("MISSING ❌" if has_missing else "PENDING ⏳")
            print(f"  - Card #{i+1} ({aid}): {status} (src={img_src})")

        # 4. Capture Anchors Tab Screenshot
        anchors_ss = ARTIFACT_DIR / "test_anchors.png"
        page.screenshot(path=str(anchors_ss))
        print(f"[Test] Captured Anchors screenshot: {anchors_ss}")

        # Print any console errors
        if page_errors:
            print("[Test] Page Errors:", page_errors)
        error_logs = [l for l in console_logs if "[error]" in l]
        if error_logs:
            print("[Test] Console Errors:", error_logs)

        browser.close()

    print("[Test] Shutting down server...")
    server.shutdown()
    print("[Test] Test run complete.")

if __name__ == "__main__":
    run_test()
