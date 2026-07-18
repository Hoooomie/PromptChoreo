"""探索 Happy Oyster — 登录后状态。

打开浏览器，等待用户手动登录和导航到 Directing Mode，然后截图 dump。
首次运行会复用 .browser_data 目录保存登录态。
"""

import asyncio
import json
import os

from playwright.async_api import async_playwright

URL = "https://www.happyoyster.cn/"
USER_DATA_DIR = "D:/PromptChoreo/.browser_data"
OUTPUT_DIR = "D:/PromptChoreo/.exploration"
WAIT_SECONDS = 120  # 给用户登录和导航的时间


async def dump_interactive_elements(page):
    return await page.evaluate("""() => {
        const sels = 'input, textarea, button, [contenteditable], [role="button"], [role="textbox"], a[href]';
        const els = document.querySelectorAll(sels);
        return Array.from(els).map((el, i) => {
            const r = el.getBoundingClientRect();
            let path = [];
            let node = el;
            while (node && node.nodeType === 1 && path.length < 6) {
                let part = node.tagName.toLowerCase();
                if (node.id) { path.unshift('#' + node.id); break; }
                if (node.className && typeof node.className === 'string') {
                    const cls = node.className.trim().split(/\\s+/).slice(0, 2).join('.');
                    if (cls) part += '.' + cls;
                }
                const parent = node.parentElement;
                if (parent) {
                    const sibs = Array.from(parent.children).filter(c => c.tagName === node.tagName);
                    if (sibs.length > 1) part += `:nth-of-type(${sibs.indexOf(node) + 1})`;
                }
                path.unshift(part);
                node = parent;
            }
            return {
                idx: i, tag: el.tagName.toLowerCase(),
                type: el.type || '', role: el.getAttribute('role') || '',
                id: el.id || '',
                cls: (typeof el.className === 'string' ? el.className : '').slice(0, 80),
                name: el.name || '', placeholder: el.placeholder || '',
                text: (el.textContent || '').trim().slice(0, 80),
                href: el.href || '',
                visible: r.width > 0 && r.height > 0,
                x: Math.round(r.x), y: Math.round(r.y),
                w: Math.round(r.width), h: Math.round(r.height),
                selector: path.join(' > ')
            };
        });
    }""")


async def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(USER_DATA_DIR, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
            viewport={"width": 1366, "height": 900},
        )
        page = context.pages[0] if context.pages else await context.new_page()

        print(f"[1/5] 导航到 {URL} ...")
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)

        print(f"[2/5] 当前 URL: {page.url}")
        await page.screenshot(path=f"{OUTPUT_DIR}/step1_before_login.png", full_page=True)

        print(f"\n{'='*60}")
        print(f"  请在浏览器中手动操作:")
        print(f"  1. 登录 Happy Oyster 账号")
        print(f"  2. 找到并进入 Directing Mode (实时导演) 界面")
        print(f"  3. 等待 {WAIT_SECONDS} 秒，脚本会自动截图")
        print(f"{'='*60}\n")

        for remaining in range(WAIT_SECONDS, 0, -30):
            print(f"  ...剩余 {remaining}s (可继续操作浏览器)")
            await asyncio.sleep(min(30, remaining))

        print(f"\n[3/5] 截图登录后状态 ...")
        current_url = page.url
        print(f"      当前 URL: {current_url}")
        await page.screenshot(path=f"{OUTPUT_DIR}/step2_after_login.png", full_page=True)

        print(f"[4/5] 提取可交互元素 ...")
        elements = await dump_interactive_elements(page)
        visible = [e for e in elements if e["visible"]]

        print(f"\n=== 当前页可见元素 ({len(visible)}) ===")
        for e in visible[:60]:  # 只显示前 60 个
            label = e["text"] or e["placeholder"] or e["id"] or e["cls"][:40] or "(no label)"
            print(f"  [{e['tag']}] {label}")
            print(f"       selector: {e['selector'][:120]}")

        with open(f"{OUTPUT_DIR}/step2_elements.json", "w", encoding="utf-8") as f:
            json.dump(elements, f, ensure_ascii=False, indent=2)

        html = await page.content()
        with open(f"{OUTPUT_DIR}/step2_after_login.html", "w", encoding="utf-8") as f:
            f.write(html)

        print(f"\n[5/5] 完成")
        print(f"      截图: {OUTPUT_DIR}/step2_after_login.png")
        print(f"      元素: {OUTPUT_DIR}/step2_elements.json")
        print(f"      URL:  {current_url}")
        print(f"      可见: {len(visible)} / 总计: {len(elements)}")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
