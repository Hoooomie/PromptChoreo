"""探索 Happy Oyster 网页 DOM 结构。

自动打开 happyoysterai.net，截图并 dump 所有可交互元素的 CSS selector。
首次运行会创建浏览器用户数据目录（用于保存登录态）。
"""

import asyncio
import json
import os

from playwright.async_api import async_playwright

URL = "https://www.happyoyster.cn/"
USER_DATA_DIR = "D:/PromptChoreo/.browser_data"
OUTPUT_DIR = "D:/PromptChoreo/.exploration"


async def dump_interactive_elements(page):
    """提取页面中所有可交互元素的信息。"""
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
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else await context.new_page()

        print(f"[1/4] 导航到 {URL} ...")
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        print("[2/4] 等待页面加载 (8s) ...")
        await asyncio.sleep(8)

        print("[3/4] 截图 ...")
        await page.screenshot(
            path=f"{OUTPUT_DIR}/happyoyster_landing.png",
            full_page=True,
        )

        print("[4/4] 提取可交互元素 ...")
        elements = await dump_interactive_elements(page)

        visible = [e for e in elements if e["visible"]]
        print(f"\n=== 可见可交互元素 ({len(visible)}) ===")
        for e in visible:
            label = e["text"] or e["placeholder"] or e["id"] or e["cls"][:40] or "(no label)"
            print(f"  [{e['tag']}] {label}")
            print(f"       selector: {e['selector']}")

        with open(f"{OUTPUT_DIR}/happyoyster_elements.json", "w", encoding="utf-8") as f:
            json.dump(elements, f, ensure_ascii=False, indent=2)

        html = await page.content()
        with open(f"{OUTPUT_DIR}/happyoyster_landing.html", "w", encoding="utf-8") as f:
            f.write(html)

        print(f"\n截图: {OUTPUT_DIR}/happyoyster_landing.png")
        print(f"元素: {OUTPUT_DIR}/happyoyster_elements.json")
        print(f"HTML: {OUTPUT_DIR}/happyoyster_landing.html")
        print(f"可见: {len(visible)} / 总计: {len(elements)}")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
