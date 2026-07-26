"""PixVerse 自动爬取下载视频。

用法:
    python scripts/download_pv_videos.py              # 下载全部未下载过的
    python scripts/download_pv_videos.py --all        # 下载全部
    python scripts/download_pv_videos.py --date 2026.07.19  # 下载指定日期
"""
import argparse, asyncio, json, os, re
from pathlib import Path

from playwright.async_api import async_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
USER_DATA = str(Path.home() / ".workbuddy" / "browser_data_pixverse")
GENERATE_URL = "https://world.pixverse.video/generate/"
DOWNLOADED_FILE = str(PROJECT_ROOT / ".downloaded_pv.json")
DOWNLOAD_DIR = str(PROJECT_ROOT / "outputs" / "downloads" / "pixverse")

def load_downloaded():
    if os.path.exists(DOWNLOADED_FILE):
        with open(DOWNLOADED_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_downloaded(dl):
    with open(DOWNLOADED_FILE, "w", encoding="utf-8") as f:
        json.dump(list(dl), f, ensure_ascii=False)

async def main(download_all=False, target_date=None):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    downloaded = set() if download_all else load_downloaded()

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA, headless=False,
            viewport={"width": 1920, "height": 1080},
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # 1. 导航到生成页 → 点 Mine
        await page.goto(GENERATE_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        for txt in ["Mine", "我的"]:
            try:
                btn = page.locator(f"text={txt}").first
                if await btn.is_visible(timeout=3000): await btn.click(); break
            except Exception: pass
        await asyncio.sleep(5)

        # 确保在 Worlds tab
        for txt in ["Worlds"]:
            try:
                tab = page.locator(f"text={txt}").first
                if await tab.is_visible(timeout=2000): await tab.click()
            except Exception: pass
        await asyncio.sleep(2)

        # 2. 滚动加载
        for _ in range(8):
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)

        # 3. 找视频卡片（缩略图+Edit+Publish 的 div，跳过 Edit/Publish 按钮本身）
        cards = await page.evaluate("""() => {
            var results = [];
            var current_date = '';
            document.querySelectorAll('div').forEach(function(el) {
                var t = el.innerText || '';
                var r = el.getBoundingClientRect();

                // 日期头 (如 "2026.07.19")
                var dm = t.match(/^(\\d{4}\\.\\d{2}\\.\\d{2})$/m);
                if (dm && t.length < 20) {
                    current_date = dm[1];
                    return;
                }

                // 视频卡片：有 "Edit" 和 "Publish" 按钮的 div
                if (!t.includes('Edit') || !t.includes('Publish')) return;
                if (r.width < 100 || r.height < 60) return;

                // 视频名（通常是第一行或"No Title"）
                var lines = t.split('\\n').filter(function(l) { return l.trim(); });
                var name = '';
                for (var i = 0; i < lines.length; i++) {
                    var l = lines[i].trim();
                    if (l === 'Edit' || l === 'Publish') continue;
                    if (l.match(/^\\d|^Jul |^Jun |^Aug /)) continue;
                    if (l.length > 3) { name = l; break; }
                }
                if (!name) name = 'untitled';

                results.push({
                    name: name,
                    date: current_date,
                    x: Math.round(r.x), y: Math.round(r.y),
                    text: t.slice(0, 100)
                });
            });
            return results;
        }""")

        if target_date:
            cards = [c for c in cards if c["date"] == target_date]

        print(f"找到 {len(cards)} 个视频" + (f" ({target_date})" if target_date else ""))

        for i, card in enumerate(cards):
            key = f"{card['date']}_{card['name']}"
            if key in downloaded:
                print(f"  [{i+1}/{len(cards)}] {card['date']} {card['name']} - skip"); continue

            print(f"\n[{i+1}/{len(cards)}] {card['date']} {card['name']}")

            # 滚到卡片
            await page.evaluate(f"() => window.scrollTo(0, {card['y'] - 300})")
            await asyncio.sleep(0.5)
            # 点卡片上半部分（缩略图区域，远在 Edit/Publish 之上）
            await page.mouse.click(card["x"] + 65, card["y"] - 150)
            await asyncio.sleep(5)
            print(f"  URL after click: {page.url}")
            await asyncio.sleep(5)

            # 找 Download（可能在弹窗或同页面展开）
            try:
                for txt in ["Download", "下载"]:
                    btn = page.locator(f"button:has-text('{txt}')").first
                    if await btn.is_visible(timeout=3000):
                        async with page.expect_download(timeout=120000) as dl_info:
                            await btn.click()
                        dl = await dl_info.value
                        fname = f"{card['date']}_{card['name'].replace(' ', '_')}.mp4"
                        await dl.save_as(os.path.join(DOWNLOAD_DIR, fname))
                        print(f"  [OK] {fname}")
                        downloaded.add(key)
                        break
                    btn2 = page.locator(f"text={txt}").first
                    if await btn2.is_visible(timeout=3000):
                        async with page.expect_download(timeout=120000) as dl_info:
                            await btn2.click()
                        dl = await dl_info.value
                        fname = f"{card['date']}_{card['name'].replace(' ', '_')}.mp4"
                        await dl.save_as(os.path.join(DOWNLOAD_DIR, fname))
                        print(f"  [OK] {fname}")
                        downloaded.add(key)
                        break
                else:
                    print(f"  [FAIL] 下载按钮未找到 (URL: {page.url})")
            except Exception as e:
                print(f"  [FAIL] {e}")

            # 返回 Mine
            await page.goto(GENERATE_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            for txt in ["Mine"]:
                try:
                    btn = page.locator(f"text={txt}").first
                    if await btn.is_visible(timeout=3000): await btn.click(); break
                except Exception: pass
            await asyncio.sleep(5)

        save_downloaded(downloaded)
        print(f"\nDONE. {len(downloaded)} 个")
        await context.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="下载全部")
    parser.add_argument("--date", type=str, default=None, help="指定日期 如 2026.07.19")
    args = parser.parse_args()
    asyncio.run(main(download_all=args.all, target_date=args.date))
