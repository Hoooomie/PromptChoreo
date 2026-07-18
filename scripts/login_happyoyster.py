"""Happy Oyster 登录辅助脚本。

只负责打开持久化 Chromium 到 happyoyster.cn 让你手动登录，
登录完成后按回车关闭浏览器，登录态会自动保存到 user_data_dir。

用法:
    python scripts/login_happyoyster.py
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

USER_DATA_DIR = str(Path.home() / ".workbuddy" / "browser_data")
LOGIN_URL = "https://www.happyoyster.cn/"


def main() -> None:
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        print("=" * 50)
        print("浏览器已打开，请在里面手动登录 Happy Oyster。")
        print(f"登录态会保存到: {USER_DATA_DIR}")
        print("登录完成后，回到这里按回车关闭浏览器即可。")
        print("=" * 50)
        input("登录完成后按回车关闭浏览器...")
        context.close()
        print("浏览器已关闭，登录态已保存。")


if __name__ == "__main__":
    main()
