import urllib.request
for u in ["https://den3d-droid.github.io/vecher/firebase-config.js","https://den3d-droid.github.io/vecher/index.html"]:
    with urllib.request.urlopen(u, timeout=30) as r:
        b=r.read().decode(); print("URL",u,r.status,r.headers.get("Cache-Control"),r.headers.get("Last-Modified"))
        print("  has config:", "vecher-a2124" in b, "| has openAcc:", "openAcc" in b, "| len", len(b))
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b=p.chromium.launch(); pg=b.new_page(); logs=[]
    pg.on('console', lambda m: logs.append(m.type+': '+m.text)); pg.on('pageerror', lambda e: logs.append('PAGEERR '+str(e)))
    pg.goto("https://den3d-droid.github.io/vecher/"); pg.wait_for_timeout(5000)
    print("CFG", pg.evaluate("!!window.FIREBASE_CONFIG"), "BTN visible", pg.is_visible('#openAcc'), pg.inner_text('#openAcc'))
    pg.click('#openAcc'); pg.wait_for_timeout(500); print("SETTINGS", pg.inner_text('#mBody')[:200].replace('\n',' | '))
    print("TOAST", pg.inner_text('#toast')); print("LOGS", logs[:10])
