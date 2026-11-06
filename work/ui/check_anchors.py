import sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright
import threading
from http.server import ThreadingHTTPServer

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT / "work" / "ui"))
from server import StudioRequestHandler

server = ThreadingHTTPServer(("127.0.0.1", 8099), StudioRequestHandler)
t = threading.Thread(target=server.serve_forever, daemon=True)
t.start()
time.sleep(0.5)

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe", headless=True)
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    
    logs = []
    page.on("console", lambda m: logs.append(f"[{m.type}] {m.text}"))
    page.on("requestfailed", lambda req: print(f"REQUEST FAILED: {req.url} -> {req.failure}"))
    page.on("response", lambda res: print(f"RESPONSE {res.status}: {res.url}") if res.status >= 400 else None)
    
    page.goto("http://127.0.0.1:8099/", wait_until="domcontentloaded")
    time.sleep(1.0)
    
    # Click Anchors Tab
    page.click("button[data-tab='anchors']")
    time.sleep(1.5)
    
    cards = page.locator(".anchor-card")
    count = cards.count()
    print(f"\n==========================================")
    print(f"Total anchor cards found: {count}")
    print(f"==========================================")
    
    for i in range(count):
        card = cards.nth(i)
        card.scroll_into_view_if_needed()
        time.sleep(0.3)
        aid = card.locator(".anchor-id").inner_text()
        img = card.locator("img")
        missing = card.locator(".missing-placeholder")
        
        has_img = img.count() > 0
        img_loaded = False
        img_src = ""
        if has_img:
            img_src = img.get_attribute("src") or ""
            img_loaded = page.evaluate("el => el.complete && el.naturalWidth > 0", img.element_handle())
        
        has_missing = missing.count() > 0
        missing_text = missing.inner_text().replace("\n", " ") if has_missing else ""
        
        status = "LOADED [OK]" if img_loaded else ("MISSING BADGE [FAIL]" if has_missing else "NOT LOADED / BROKEN")
        print(f"Card #{i+1:02d} ({aid}): {status}")
        print(f"   has_img={has_img}, loaded={img_loaded}, src={img_src}")
        if has_missing:
            print(f"   badge={missing_text}")
    
    artifact_dir = Path(r"C:\Users\bugbl\.gemini\antigravity\brain\11e6839d-e38f-40d5-8ee5-0f5c88dbb4a5")
    page.screenshot(path=str(artifact_dir / "anchors_scroll_check.png"))
    print(f"\nScreenshot saved to {artifact_dir / 'anchors_scroll_check.png'}")
    
    browser.close()

server.shutdown()

