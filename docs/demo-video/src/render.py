"""video.html 을 프레임 단위로 캡처한다.

    python render.py stills 3,8,14        지정한 초의 화면을 stills/ 에 저장
    python render.py frames <몫> <나눔>   프레임 번호 % 나눔 == 몫 인 것만 frames/ 에 저장
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
FPS = 30
URL = (HERE / "video.html").as_uri()

SEEK = "t => new Promise(r => { window.__seek(t); requestAnimationFrame(() => requestAnimationFrame(r)); })"

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    page = browser.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
    page.goto(URL)
    page.wait_for_function("window.__ready === true")
    total = page.evaluate("window.__total")
    if sys.argv[1] == "stills":
        out = HERE / "stills"
        out.mkdir(exist_ok=True)
        print(page.evaluate("window.__measure"))
        for t in sys.argv[2].split(","):
            page.evaluate(SEEK, float(t))
            page.screenshot(path=str(out / f"t{float(t):06.2f}.jpg"), type="jpeg", quality=88)
    else:
        part, parts = int(sys.argv[2]), int(sys.argv[3])
        out = HERE / "frames"
        out.mkdir(exist_ok=True)
        for n in range(int(total * FPS)):
            if n % parts != part:
                continue
            page.evaluate(SEEK, n / FPS)
            page.screenshot(path=str(out / f"{n:05d}.jpg"), type="jpeg", quality=96)
        print("done", part)
    browser.close()
