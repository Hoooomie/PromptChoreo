"""PixVerse 登录辅助脚本。

打开持久化 Chromium 到 PixVerse，手动登录后按回车关闭，
登录态保存到独立的 user_data_dir（不与 Happy Oyster 混用）。

用法:
    python scripts/login_pixverse.py
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

USER_DATA_DIR = str(Path.home() / ".workbuddy" / "browser_data_pixverse")
LOGIN_URL = "https://world.pixverse.video/generate/"


def main() -> None:
    with sync_playwright() as p:
        launch_options = {
            "user_data_dir": USER_DATA_DIR,
            "headless": False,
            "args": [
                # PixVerse World 已迁移到 pixverse.video，但登录服务仍可能
                # 使用 pixverse.ai。新 Chrome profile 默认的第三方 Cookie /
                # 存储隔离会让跨站认证层变成空白，因此登录专用窗口显式放行。
                "--disable-features=TrackingProtection3pcd,ThirdPartyStoragePartitioning",
                "--disable-popup-blocking",
            ],
        }
        try:
            # 使用系统 Chrome。PixVerse 新登录页在 Playwright 自带的
            # Chromium 中偶尔只显示空白页，而系统 Chrome 可以正常完成认证。
            context = p.chromium.launch_persistent_context(
                channel="chrome",
                **launch_options,
            )
        except Exception as exc:
            print(f"系统 Chrome 启动失败，回退到 Playwright Chromium：{exc}")
            context = p.chromium.launch_persistent_context(**launch_options)

        # 登录按钮可能新开认证窗口；确保它不会藏在生成页后面。
        context.on("page", lambda popup: popup.bring_to_front())
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        print("=" * 50)
        print("浏览器已打开 PixVerse World。请从当前生成页点击 Log in 登录。")
        print(f"当前入口: {page.url}")
        print("不要打开 app.pixverse.ai/login；它是另一套产品界面。")
        print(f"登录态会保存到: {USER_DATA_DIR}")
        print("登录完成后，回到这里按回车关闭浏览器即可。")
        print("=" * 50)
        input("登录完成后按回车关闭浏览器...")
        context.close()
        print("浏览器已关闭，登录态已保存。")


if __name__ == "__main__":
    main()
