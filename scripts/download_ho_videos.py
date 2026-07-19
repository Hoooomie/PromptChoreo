"""HappyOyster 自动爬取下载 Directing 视频。

用法:
    python scripts/download_ho_videos.py          # 只下载未下载过的
    python scripts/download_ho_videos.py --all    # 下载全部
"""
import argparse, asyncio, json, os
from playwright.async_api import async_playwright

USER_DATA = "C:/Users/19515/.workbuddy/browser_data"
DOWNLOADED_FILE = "D:/PromptChoreo/.downloaded_ho.json"
DOWNLOAD_DIR = "D:/PromptChoreo/outputs/downloads/happyoyster"

def load_downloaded():
    if os.path.exists(DOWNLOADED_FILE):
        with open(DOWNLOADED_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_downloaded(dl):
    with open(DOWNLOADED_FILE, "w", encoding="utf-8") as f:
        json.dump(list(dl), f, ensure_ascii=False)

async def main(download_all=False):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    downloaded = set() if download_all else load_downloaded()

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA, headless=False,
            viewport={"width": 1920, "height": 1080},
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto("https://www.happyoyster.cn/create/directing", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)
        for txt in ["Accept All", "接受全部"]:
            try:
                btn = page.locator(f"button:has-text('{txt}')").first
                if await btn.is_visible(timeout=2000): await btn.click()
            except Exception: pass

        await page.goto("https://www.happyoyster.cn/profile", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)

        for _ in range(3):
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)

        cards = await page.evaluate("""() => {
            var seen = new Set(), results = [];
            document.querySelectorAll('div').forEach(function(el) {
                var t = el.innerText || '';
                if (!t.includes('实') || !t.includes('导演') || !t.includes('视频')) return;
                var img = el.querySelector('img, video, canvas');
                if (!img) return;
                var ir = img.getBoundingClientRect();
                if (ir.width < 200 || ir.height < 100) return;
                var r = el.getBoundingClientRect();
                if (r.width < 200 || r.height < 100) return;
                var lines = t.split('\\n');
                var name = (lines[2] || lines[1] || '').trim();
                if (!name || name.includes('视频') || name.includes('导演') || seen.has(name)) return;
                seen.add(name);
                results.push({name: name, x: Math.round(r.x), y: Math.round(r.y)});
            });
            return results;
        }""")
        print(f"找到 {len(cards)} 个视频: {[c['name'] for c in cards]}")

        for i, card in enumerate(cards):
            name = card["name"]
            if name in downloaded:
                print(f"  [{i+1}/{len(cards)}] {name} - skip"); continue

            print(f"\n[{i+1}/{len(cards)}] {name}")
            await page.evaluate("""(pos) => {
                var divs = document.querySelectorAll('div');
                for (var d of divs) {
                    var r = d.getBoundingClientRect();
                    if (Math.abs(r.x - pos.x) < 5 && Math.abs(r.y - pos.y) < 5 && r.width > 200) {
                        var img = d.querySelector('img, video, canvas');
                        if (img) { img.click(); return; }
                        d.click(); return;
                    }
                }
            }""", {"x": card["x"], "y": card["y"]})
            await asyncio.sleep(5)

            try:
                dl_btn = page.locator("button[aria-label='下载']").first
                await dl_btn.wait_for(state="visible", timeout=10000)
                async with page.expect_download(timeout=120000) as dl_info:
                    await dl_btn.click()
                await asyncio.sleep(2)
                for txt in ["用户指令", "包含指令", "Prompt"]:
                    try:
                        opt = page.locator(f"text={txt}").first
                        if await opt.is_visible(timeout=2000): await opt.click()
                    except Exception: pass
                dl = await dl_info.value
                fname = f"{name}.mp4"
                await dl.save_as(os.path.join(DOWNLOAD_DIR, fname))
                print(f"  [OK] {fname}")
                downloaded.add(name)
            except Exception as e:
                print(f"  [FAIL] {e}")

            await page.goto("https://www.happyoyster.cn/profile", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

        save_downloaded(downloaded)
        print(f"\nDONE. {len(downloaded)} 个")
        await context.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="下载全部视频")
    args = parser.parse_args()
    asyncio.run(main(download_all=args.all))
