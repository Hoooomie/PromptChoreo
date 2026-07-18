"""探索 Happy Oyster Directing Mode — 分阶段截图。

阶段 1 (90s): 登录 → 选 Directing → 选 Peaceful → 停在输入框界面
阶段 2 (90s): 输入初始 prompt → 开始生成 → 等加载 100% → 停在生成中界面

每个阶段结束自动截图 + dump 可交互元素。
"""

import asyncio
import json
import os

from playwright.async_api import async_playwright

URL = "https://www.happyoyster.cn/"
USER_DATA_DIR = "D:/PromptChoreo/.browser_data"
OUTPUT_DIR = "D:/PromptChoreo/.exploration"


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


async def wait_with_countdown(seconds, label):
    for remaining in range(seconds, 0, -30):
        print(f"  [{label}] ...剩余 {remaining}s")
        await asyncio.sleep(min(30, remaining))


async def screenshot_and_dump(page, name):
    path_png = f"{OUTPUT_DIR}/{name}.png"
    path_json = f"{OUTPUT_DIR}/{name}_elements.json"
    path_html = f"{OUTPUT_DIR}/{name}.html"

    await page.screenshot(path=path_png, full_page=True)
    elements = await dump_interactive_elements(page)
    visible = [e for e in elements if e["visible"]]

    with open(path_json, "w", encoding="utf-8") as f:
        json.dump(elements, f, ensure_ascii=False, indent=2)
    html = await page.content()
    with open(path_html, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n  截图: {path_png}")
    print(f"  元素: {path_json} (可见 {len(visible)}/{len(elements)})")
    print(f"  URL:  {page.url}")
    print(f"  --- 可见元素 ---")
    for e in visible:
        label = e["text"] or e["placeholder"] or e["id"] or e["cls"][:50] or "(no label)"
        print(f"    [{e['tag']}] {label[:60]}")
        print(f"         {e['selector'][:130]}")
    print()


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

        print(f"[导航] {URL} ...")
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)

        print(f"\n{'='*60}")
        print(f"  阶段 1 (90s):")
        print(f"  1. 登录 Happy Oyster")
        print(f"  2. 选择「实时导演」(Directing) 模式")
        print(f"  3. 选择「Peaceful」子模式")
        print(f"  4. 停在能看到 prompt 输入框的界面")
        print(f"{'='*60}")
        await wait_with_countdown(90, "阶段1")

        print(f"\n[截图] 阶段 1: 模式选择界面")
        await screenshot_and_dump(page, "phase1_mode_select")

        print(f"{'='*60}")
        print(f"  阶段 2 (90s):")
        print(f"  1. 在输入框输入一段初始 prompt")
        print(f"  2. 提交，开始生成")
        print(f"  3. 等待加载到 100%")
        print(f"  4. 停在生成中的界面（能看到 Pause 按钮和输入框）")
        print(f"{'='*60}")
        await wait_with_countdown(90, "阶段2")

        print(f"\n[截图] 阶段 2: 生成中界面")
        await screenshot_and_dump(page, "phase2_generating")

        print(f"[完成] 浏览器 10s 后关闭")
        await asyncio.sleep(10)
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
