"""探索 Happy Oyster — 生成中界面。

只截一张图：用户输入初始 prompt → 点 ↑ 提交 → 等加载 100% → 生成中界面。
"""

import asyncio
import json
import os

from playwright.async_api import async_playwright

URL = "https://www.happyoyster.cn/create/directing"
USER_DATA_DIR = "D:/PromptChoreo/.browser_data"
OUTPUT_DIR = "D:/PromptChoreo/.exploration"
WAIT_SECONDS = 240


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
                cls: (typeof el.className === 'string' ? el.className : '').slice(0, 100),
                name: el.name || '', placeholder: el.placeholder || '',
                text: (el.textContent || '').trim().slice(0, 100),
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

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
            viewport={"width": 1366, "height": 900},
        )
        page = context.pages[0] if context.pages else await context.new_page()

        print(f"[导航] {URL} ...")
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)

        print(f"\n{'='*60}")
        print(f"  请完成以下操作 ({WAIT_SECONDS}s):")
        print(f"  1. 在输入框输入一段初始 prompt")
        print(f"  2. 点 ↑ 按钮提交，开始生成")
        print(f"  3. 等待加载到 100%，时长从 00:00 开始")
        print(f"  4. 停在生成中界面（能看到 Pause 和指令输入框）")
        print(f"{'='*60}")

        for remaining in range(WAIT_SECONDS, 0, -30):
            print(f"  ...剩余 {remaining}s")
            await asyncio.sleep(30)

        print(f"\n[截图] 生成中界面")
        await page.screenshot(path=f"{OUTPUT_DIR}/generating_live.png", full_page=True)
        elements = await dump_interactive_elements(page)
        visible = [e for e in elements if e["visible"]]

        with open(f"{OUTPUT_DIR}/generating_live_elements.json", "w", encoding="utf-8") as f:
            json.dump(elements, f, ensure_ascii=False, indent=2)
        html = await page.content()
        with open(f"{OUTPUT_DIR}/generating_live.html", "w", encoding="utf-8") as f:
            f.write(html)

        print(f"\n  URL: {page.url}")
        print(f"  可见元素 ({len(visible)}/{len(elements)}):")
        for e in visible:
            label = e["text"] or e["placeholder"] or e["id"] or e["cls"][:50] or "(no label)"
            print(f"    [{e['tag']}] {label[:60]}")
            print(f"         {e['selector'][:130]}")

        print(f"\n  截图: {OUTPUT_DIR}/generating_live.png")
        print(f"  元素: {OUTPUT_DIR}/generating_live_elements.json")

        await asyncio.sleep(5)
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
