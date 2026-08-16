"""HappyOyster international-site adapter for Directing Mode."""

from __future__ import annotations

import asyncio
import json
import sys
import time

from playwright.async_api import Page

from .happy_oyster import HappyOysterAdapter
from ..credentials import get_browser_data_dir


class GoogleAccountIsolationRequired(RuntimeError):
    """Raised when Google silently reuses a previous signed-in account."""


class HappyOysterGlobalAdapter(HappyOysterAdapter):
    """HappyOyster Directing Mode adapter for ``happyoyster.com``."""

    name = "happy_oyster_global"
    user_data_dir = get_browser_data_dir("happy_oyster_global")

    URL_HOME = "https://www.happyoyster.com/home"
    URL_DIRECTING = "https://www.happyoyster.com/create/directing"
    URL = URL_DIRECTING
    SIGN_IN_TEXT = "Sign in to claim free creative credits"
    GOOGLE_MANUAL_CHALLENGE_TIMEOUT_S = 15 * 60
    LOGIN_CONFIRM_TIMEOUT_S = 15 * 60
    # For both Google credential steps, use the exact sequence:
    # wait -> fill -> wait -> click Next.
    CREDENTIAL_FILL_DELAY_S = 2.0
    NAVIGATION_RETRY_TIMEOUT_S = 3 * 60

    SELECTORS = {
        **HappyOysterAdapter.SELECTORS,
        "initial_input": (
            "textarea[placeholder*='Describe your story'], "
            "textarea.absolute.inset-0"
        ),
        "initial_send": "button[aria-label='Send']",
        "initial_send_form": "button[aria-label='Send']",
        "stream_input": (
            "textarea[placeholder*='direct'], "
            "textarea[placeholder*='Direct']"
        ),
        "stream_send": (
            "button.story-send-btn, button[aria-label='Send']"
        ),
        # The live site renders this control through a non-semantic wrapper,
        # so do not require it to be a <button> or <a>. Its complete visible
        # text is the authoritative signed-out marker and remains clickable.
        "login_button": (
            'text="Sign in to claim free creative credits"'
        ),
        "promo_login_button": "button:text-is('Log in now')",
        "cookie_accept": "button:text-is('Accept All')",
        "google_signin_method": (
            "button:text-is('Sign in with Google'), "
            "[role='button']:text-is('Sign in with Google')"
        ),
        # Observed on the signed-in international home page (2026-08-14).
        "user_menu": "button[aria-label='Open user navigation menu']",
        "campaign_close": "button[aria-label='Close campaign dialog']",
        "logout_button": (
            "[role='menuitem']:has-text('Log out'), "
            "[role='menuitem']:has-text('Sign out'), "
            "button:has-text('Log out'), button:has-text('Sign out')"
        ),
        "captcha": (
            "iframe[src*='captcha' i], [class*='captcha' i], "
            "[id*='captcha' i]"
        ),
        "google_email_input": (
            "#identifierId:visible, input[type='email']:visible"
        ),
        "google_email_next": "#identifierNext button, #identifierNext",
        "google_password_input": (
            "input[name='Passwd']:visible, input[type='password']:visible"
        ),
        "google_password_next": "#passwordNext button, #passwordNext",
    }

    async def _first_visible(
        self,
        page: Page,
        selector: str,
        *,
        timeout_ms: int = 0,
    ):
        deadline = time.monotonic() + (timeout_ms / 1000)
        while True:
            try:
                candidates = await page.locator(selector).all()
            except Exception:
                candidates = []
            for candidate in candidates:
                try:
                    if await candidate.is_visible(timeout=500):
                        return candidate
                except Exception:
                    continue
            if timeout_ms <= 0 or time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.2)

    async def _goto_with_retry(
        self,
        page: Page,
        url: str,
        *,
        total_timeout_s: float | None = None,
    ) -> None:
        """Retry transient international-site navigation failures."""
        total_timeout_s = float(
            self.NAVIGATION_RETRY_TIMEOUT_S
            if total_timeout_s is None
            else total_timeout_s
        )
        deadline = time.monotonic() + total_timeout_s
        attempt = 0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            attempt += 1
            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                if attempt > 1:
                    print(
                        f"[NAV] 页面第 {attempt} 次尝试加载成功: {url}",
                        file=sys.stderr,
                    )
                return
            except Exception as exc:
                last_error = exc
                try:
                    page_closed = page.is_closed()
                except (AttributeError, TypeError):
                    page_closed = False
                if page_closed:
                    raise RuntimeError(
                        f"Happy Oyster 页面已关闭，无法加载 {url}"
                    ) from exc
                remaining = max(deadline - time.monotonic(), 0.0)
                if remaining <= 0:
                    break
                delay = min(2.0 + attempt, 10.0, remaining)
                print(
                    f"[NAV] 页面暂时未加载成功（第 {attempt} 次）: "
                    f"{type(exc).__name__}: {exc}；"
                    f"{delay:.0f}s 后重试，最多再等待 {remaining:.0f}s",
                    file=sys.stderr,
                )
                await asyncio.sleep(delay)
        raise RuntimeError(
            f"Happy Oyster 页面在 {total_timeout_s:.0f}s 内多次加载失败: "
            f"{type(last_error).__name__ if last_error else 'unknown'}: "
            f"{last_error}"
        ) from last_error

    async def _dismiss_campaign_dialog(self, page: Page) -> None:
        close = await self._first_visible(
            page, self.SELECTORS["campaign_close"]
        )
        if close is not None:
            try:
                await close.click(timeout=3000)
            except Exception:
                pass

    async def _dismiss_login_promo(self, page: Page) -> None:
        """Close the occasional ``Log in now`` promo without activating it."""
        promo = await self._first_visible(
            page, self.SELECTORS["promo_login_button"], timeout_ms=3000
        )
        if promo is None:
            return
        result = await page.evaluate(
            """() => {
                const buttons = Array.from(document.querySelectorAll('button'));
                const login = buttons.find(
                    el => (el.innerText || '').trim() === 'Log in now'
                        && el.getBoundingClientRect().width > 0
                );
                if (!login) return 'absent';
                let root = login.parentElement;
                for (let depth = 0; root && depth < 7; depth += 1) {
                    const candidates = Array.from(root.querySelectorAll('button'));
                    const close = candidates.find(el => {
                        if (el === login) return false;
                        const text = (el.innerText || '').trim();
                        const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                        const title = (el.getAttribute('title') || '').toLowerCase();
                        return text === '' || aria.includes('close')
                            || title.includes('close');
                    });
                    if (close) {
                        close.click();
                        return 'closed';
                    }
                    root = root.parentElement;
                }
                return 'close-not-found';
            }"""
        )
        if result != "closed":
            raise RuntimeError(
                "检测到 Log in now 提示弹窗，但未找到关闭按钮"
            )
        print("[ACCOUNT] 已关闭 Log in now 提示弹窗", file=sys.stderr)

    async def _accept_cookies(self, page: Page) -> None:
        accept = await self._first_visible(
            page, self.SELECTORS["cookie_accept"], timeout_ms=3000
        )
        if accept is not None:
            await accept.click(timeout=10000)
            print("[ACCOUNT] 已接受 Cookie Usage", file=sys.stderr)

    async def _has_sign_in_marker(self, page: Page) -> bool:
        """Detect the signed-out header from rendered text, independent of tag."""
        control = await self._first_visible(
            page, self.SELECTORS["login_button"]
        )
        if control is not None:
            return True
        try:
            return bool(
                await page.evaluate(
                    r"""needle => {
                        const normalize = value => (value || '')
                            .replace(/\s+/g, ' ').trim();
                        const target = normalize(needle);
                        return Array.from(document.querySelectorAll('body *'))
                            .some(el => {
                                const rect = el.getBoundingClientRect();
                                const style = getComputedStyle(el);
                                return rect.width > 0 && rect.height > 0
                                    && style.visibility !== 'hidden'
                                    && style.display !== 'none'
                                    && normalize(el.innerText).includes(target);
                            });
                    }""",
                    self.SIGN_IN_TEXT,
                )
            )
        except Exception:
            return False

    async def _click_sign_in_marker(self, page: Page) -> None:
        """Click the rendered signed-out control even when its tag is generic."""
        control = await self._first_visible(
            page, self.SELECTORS["login_button"], timeout_ms=3000
        )
        if control is not None:
            await control.click(timeout=10000)
            return
        clicked = await page.evaluate(
            r"""needle => {
                const normalize = value => (value || '')
                    .replace(/\s+/g, ' ').trim();
                const target = normalize(needle);
                const matches = Array.from(document.querySelectorAll('body *'))
                    .filter(el => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && normalize(el.innerText).includes(target);
                    })
                    .sort((left, right) =>
                        left.querySelectorAll('*').length
                        - right.querySelectorAll('*').length
                    );
                if (!matches.length) return false;
                const leaf = matches[0];
                const clickable = leaf.closest(
                    'button, a, [role="button"], [tabindex]'
                ) || leaf;
                clickable.click();
                return true;
            }""",
            self.SIGN_IN_TEXT,
        )
        if not clicked:
            raise RuntimeError(
                f"页面未找到 {self.SIGN_IN_TEXT} 登录入口"
            )

    async def _click_exact_button_text(
        self,
        page: Page,
        text: str,
        *,
        timeout_ms: int = 15000,
    ) -> bool:
        """Click a visible native button by normalized inner text."""
        deadline = time.monotonic() + (timeout_ms / 1000)
        while True:
            try:
                clicked = await page.evaluate(
                    r"""needle => {
                        const normalize = value => (value || '')
                            .replace(/\s+/g, ' ').trim();
                        const target = normalize(needle);
                        const button = Array.from(
                            document.querySelectorAll('button')
                        ).find(el => {
                            const rect = el.getBoundingClientRect();
                            const style = getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0
                                && style.visibility !== 'hidden'
                                && style.display !== 'none'
                                && normalize(el.innerText) === target;
                        });
                        if (!button) return false;
                        button.click();
                        return true;
                    }""",
                    text,
                )
            except Exception:
                clicked = False
            if clicked:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.2)

    async def _prepare_google_email_input(self, page: Page):
        """Handle Google's optional account chooser before email entry."""
        labels = ["使用其他账号", "Use another account"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current_url = getattr(page, "url", "") or ""
            if (
                "accounts.google.com" not in current_url
                and current_url.startswith("https://www.happyoyster.com/")
                and await self.is_logged_in(page)
            ):
                raise GoogleAccountIsolationRequired(
                    "Google 跳过了“使用其他账号/邮箱输入”"
                    "步骤，直接复用了上一个账号"
                )
            email_input = await self._first_visible(
                page, self.SELECTORS["google_email_input"]
            )
            if email_input is not None:
                return email_input
            try:
                clicked = await page.evaluate(
                    r"""labels => {
                        const normalize = value => (value || '')
                            .replace(/\s+/g, ' ').trim();
                        const targets = new Set(labels.map(normalize));
                        const matches = Array.from(
                            document.querySelectorAll('body *')
                        ).filter(el => {
                            const rect = el.getBoundingClientRect();
                            const style = getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0
                                && style.visibility !== 'hidden'
                                && style.display !== 'none'
                                && targets.has(normalize(el.innerText));
                        }).sort((left, right) =>
                            left.querySelectorAll('*').length
                            - right.querySelectorAll('*').length
                        );
                        if (!matches.length) return false;
                        const leaf = matches[0];
                        const clickable = leaf.closest(
                            'button, a, [role="button"], [role="link"], '
                            + '[tabindex]'
                        ) || leaf;
                        clickable.click();
                        return true;
                    }""",
                    labels,
                )
            except Exception:
                clicked = False
            if clicked:
                print(
                    "[ACCOUNT] Google 账号选择页：已点击“使用其他账号”",
                    file=sys.stderr,
                )
                return await self._first_visible(
                    page,
                    self.SELECTORS["google_email_input"],
                    timeout_ms=15000,
                )
            await asyncio.sleep(0.2)
        return None

    @staticmethod
    def _open_auth_pages(page: Page) -> list[Page]:
        """Return the origin page plus any OAuth popup/tab pages."""
        pages = [page]
        context = getattr(page, "context", None)
        try:
            context_pages = list(context.pages) if context is not None else []
        except Exception:
            context_pages = []
        for candidate in context_pages:
            if candidate not in pages:
                pages.append(candidate)
        return pages

    async def _find_google_auth_page(
        self, origin_page: Page, *, timeout_s: float = 30.0
    ) -> Page | None:
        """Find Google OAuth whether it reused the tab or opened a popup."""
        deadline = time.monotonic() + timeout_s
        observed_urls: set[str] = set()
        while time.monotonic() < deadline:
            candidates = self._open_auth_pages(origin_page)
            # Prefer the newest popup/tab, but retain same-tab navigation.
            for candidate in reversed(candidates):
                try:
                    if candidate.is_closed():
                        continue
                except (AttributeError, TypeError):
                    pass
                current_url = getattr(candidate, "url", "") or ""
                if current_url:
                    observed_urls.add(current_url.split("?", 1)[0])
                if "accounts.google.com" in current_url:
                    return candidate

            current_url = getattr(origin_page, "url", "") or ""
            if (
                current_url.startswith("https://www.happyoyster.com/")
                and await self.is_logged_in(origin_page)
            ):
                raise GoogleAccountIsolationRequired(
                    "Google 未显示账号选择或邮箱输入页，"
                    "直接复用了上一个账号"
                )
            await asyncio.sleep(0.2)

        print(
            "[AUTH-DIAG] 点击 Google 登录后已打开页面: "
            + json.dumps(sorted(observed_urls), ensure_ascii=False),
            file=sys.stderr,
        )
        return None

    async def is_logged_in(self, page: Page) -> bool:
        """Recognize both observed variants of the signed-in header."""
        menu = await self._first_visible(page, self.SELECTORS["user_menu"])
        if menu is not None:
            return True
        try:
            return bool(
                await page.evaluate(
                    r"""signInText => {
                        const normalize = value => (value || '')
                            .replace(/\s+/g, ' ').trim();
                        const visible = el => {
                            const rect = el.getBoundingClientRect();
                            const style = getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0
                                && style.visibility !== 'hidden'
                                && style.display !== 'none';
                        };
                        const bodyText = normalize(document.body?.innerText);
                        if (bodyText.includes(normalize(signInText))) {
                            return false;
                        }
                        const hasCreateControl = bodyText.includes(
                            'Create a new world'
                        );
                        const hasCreditBalance = Array.from(
                            document.querySelectorAll('button, [role="button"]')
                        ).filter(visible).some(el =>
                            /^\d[\d,]*$/.test(normalize(el.innerText))
                        );
                        return hasCreateControl && hasCreditBalance;
                    }""",
                    self.SIGN_IN_TEXT,
                )
            )
        except Exception:
            return False

    async def _dump_auth_diagnostics(
        self, page: Page, *, secrets: tuple[str, ...] = ()
    ) -> None:
        """Print login DOM metadata without input values or credentials."""
        try:
            info = await page.evaluate(
                """() => ({
                    url: location.href,
                    inputs: Array.from(document.querySelectorAll('input'))
                        .filter(el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        })
                        .map(el => ({
                            type: el.type || '',
                            name: el.name || '',
                            placeholder: el.placeholder || '',
                            autocomplete: el.autocomplete || '',
                        })),
                    buttons: Array.from(document.querySelectorAll('button'))
                        .filter(el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        })
                        .map(el => ({
                            text: (el.innerText || '').trim().slice(0, 80),
                            ariaLabel: el.getAttribute('aria-label') || '',
                            type: el.type || '',
                        })),
                    frames: Array.from(document.querySelectorAll('iframe'))
                        .map(el => ({
                            title: el.title || '',
                            src: (el.src || '').split('?')[0],
                        })),
                    dialogs: Array.from(document.querySelectorAll(
                        '[role="dialog"], [role="alert"], '
                        + '[class*="error" i]'
                    )).filter(el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    }).slice(0, 20).map(el =>
                        (el.innerText || '').trim().slice(0, 1000)
                    ),
                })"""
            )
            rendered = json.dumps(info, ensure_ascii=False)
            for secret in secrets:
                if secret:
                    rendered = rendered.replace(secret, "[REDACTED]")
            print("[AUTH-DIAG] " + rendered, file=sys.stderr)
        except Exception as exc:
            print(
                f"[AUTH-DIAG] 认证页面诊断失败: {exc}",
                file=sys.stderr,
            )

    async def _complete_google_login(
        self,
        page: Page,
        email: str,
        password: str,
        *,
        auth_page_timeout_s: float | None = None,
    ) -> bool:
        """Advance Google login in either the current tab or an OAuth popup."""
        auth_page = await self._find_google_auth_page(
            page,
            timeout_s=(
                self.GOOGLE_MANUAL_CHALLENGE_TIMEOUT_S
                if auth_page_timeout_s is None
                else auth_page_timeout_s
            ),
        )
        if auth_page is None:
            return False
        location = "当前标签页" if auth_page is page else "OAuth 新标签页/弹窗"
        print(
            f"[ACCOUNT] 检测到 Google 登录页（{location}），填写邮箱",
            file=sys.stderr,
        )
        email_input = await self._prepare_google_email_input(auth_page)
        if email_input is None:
            await self._dump_auth_diagnostics(
                auth_page, secrets=(email, password)
            )
            raise RuntimeError("Google 登录页未找到邮箱输入框")
        email_input = await self._first_visible(
            auth_page,
            self.SELECTORS["google_email_input"],
            timeout_ms=5000,
        )
        if email_input is None:
            raise RuntimeError(
                "Google 邮箱输入框在等待后已失效"
            )
        await asyncio.sleep(self.CREDENTIAL_FILL_DELAY_S)
        await email_input.fill(email)
        await asyncio.sleep(self.CREDENTIAL_FILL_DELAY_S)
        email_next = await self._first_visible(
            auth_page,
            self.SELECTORS["google_email_next"],
            timeout_ms=5000,
        )
        if email_next is None:
            raise RuntimeError("Google 登录页未找到邮箱“下一步”按钮")
        await email_next.click(timeout=10000)

        password_input = await self._first_visible(
            auth_page,
            self.SELECTORS["google_password_input"],
            timeout_ms=10000,
        )
        if password_input is None:
            print(
                "[ACCOUNT] Google 要求人工图片验证；"
                "请在浏览器中输入字母组合并继续，"
                "脚本最多等待 15 分钟",
                file=sys.stderr,
            )
            deadline = (
                time.monotonic()
                + self.GOOGLE_MANUAL_CHALLENGE_TIMEOUT_S
            )
            while time.monotonic() < deadline:
                password_input = await self._first_visible(
                    auth_page, self.SELECTORS["google_password_input"]
                )
                if password_input is not None:
                    print(
                        "[ACCOUNT] 人工验证已通过，继续密码步骤",
                        file=sys.stderr,
                    )
                    break
                if "accounts.google.com" not in (
                    getattr(auth_page, "url", "") or ""
                ) and await self.is_logged_in(page):
                    print(
                        "[ACCOUNT] 人工验证及登录已完成",
                        file=sys.stderr,
                    )
                    return True
                await asyncio.sleep(0.5)
        if password_input is None:
            await self._dump_auth_diagnostics(
                auth_page, secrets=(email, password)
            )
            raise RuntimeError(
                "Google 人工图片验证等待超时，未进入密码步骤"
            )
        print("[ACCOUNT] Google 邮箱已确认，填写密码", file=sys.stderr)
        password_input = await self._first_visible(
            auth_page,
            self.SELECTORS["google_password_input"],
            timeout_ms=5000,
        )
        if password_input is None:
            raise RuntimeError(
                "Google 密码输入框在等待后已失效"
            )
        await asyncio.sleep(self.CREDENTIAL_FILL_DELAY_S)
        await password_input.fill(password)
        await asyncio.sleep(self.CREDENTIAL_FILL_DELAY_S)
        password_next = await self._first_visible(
            auth_page,
            self.SELECTORS["google_password_next"],
            timeout_ms=5000,
        )
        if password_next is None:
            raise RuntimeError("Google 登录页未找到密码“下一步”按钮")
        await password_next.click(timeout=10000)
        return True

    async def login_with_email(
        self,
        page: Page,
        email: str,
        password: str,
        *,
        timeout_s: float | None = None,
    ) -> None:
        """Sign in through the international site's Google flow.

        Credential values are deliberately never included in logs or errors.
        The account email and password come from the one-video account pool.
        """
        await self._goto_with_retry(page, self.URL_HOME)
        await self._dismiss_login_promo(page)
        await self._accept_cookies(page)
        if await self.is_logged_in(page):
            raise RuntimeError(
                "Happy Oyster 登录前仍存在已登录会话；已停止账号切换"
            )

        # Supervised mode: the user owns all Happy Oyster navigation and may
        # click any controls needed to reach Google OAuth. Do not require or
        # click either Happy Oyster sign-in control here. The automation only
        # takes over after a Google account chooser/email field is visible.
        print(
            "[ACCOUNT] 等待人工进入 Google 登录页；"
            "脚本仅负责填写邮箱和密码，不操作 Happy Oyster 登录按钮",
            file=sys.stderr,
        )
        if not await self._complete_google_login(page, email, password):
            await self._dump_auth_diagnostics(
                page, secrets=(email, password)
            )
            raise RuntimeError(
                "等待人工进入 Google 登录页超时；账号未消耗，可重试"
            )

        confirm_timeout_s = float(
            self.LOGIN_CONFIRM_TIMEOUT_S
            if timeout_s is None
            else timeout_s
        )
        deadline = time.monotonic() + confirm_timeout_s
        while time.monotonic() < deadline:
            if await self.is_logged_in(page):
                print(
                    "[ACCOUNT] Happy Oyster 邮箱账号登录成功",
                    file=sys.stderr,
                )
                return
            captcha = await self._first_visible(
                page, self.SELECTORS["captcha"]
            )
            if captcha is not None:
                raise RuntimeError(
                    "Happy Oyster 登录出现 CAPTCHA，需要人工处理；"
                    "当前任务已停止，本次不计入成功视频额度"
                )
            await asyncio.sleep(0.5)
        await self._dump_auth_diagnostics(
            page, secrets=(email, password)
        )
        raise RuntimeError("Happy Oyster 邮箱登录超时或凭据无效")

    async def logout(self, page: Page, *, timeout_s: float = 30.0) -> bool:
        """Log out through the observed account menu; return False if logged out."""
        await self._goto_with_retry(page, self.URL_HOME)
        await self._dismiss_login_promo(page)
        await self._accept_cookies(page)
        deadline = time.monotonic() + timeout_s
        menu = None
        while time.monotonic() < deadline:
            menu = await self._first_visible(
                page, self.SELECTORS["user_menu"]
            )
            if menu is not None:
                break
            if await self._has_sign_in_marker(page):
                return False
            google = await self._first_visible(
                page, self.SELECTORS["google_signin_method"]
            )
            if google is not None:
                return False
            await asyncio.sleep(0.5)
        if menu is None:
            await self._dump_auth_diagnostics(page)
            raise RuntimeError("无法确认 Happy Oyster 当前登录状态")

        await menu.click(timeout=10000)
        logout = await self._first_visible(
            page, self.SELECTORS["logout_button"], timeout_ms=10000
        )
        if logout is None:
            raise RuntimeError("已打开账号菜单，但未找到 Log out/Sign out")
        await logout.click(timeout=10000)

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await self._dismiss_login_promo(page)
            if await self._has_sign_in_marker(page):
                print(
                    "[ACCOUNT] Happy Oyster 账号已退出",
                    file=sys.stderr,
                )
                return True
            await asyncio.sleep(0.5)
        raise RuntimeError("Happy Oyster 退出登录超时")

    async def _submit_initial(self, page: Page) -> None:
        """Submit through the international site's accessible Send button."""
        try:
            button = page.locator(self.SELECTORS["initial_send"])
            await button.wait_for(state="visible", timeout=12000)
            await button.click(timeout=5000)
            print(
                "[DEBUG] Initial submit via international Send button",
                file=sys.stderr,
            )
            return
        except Exception as exc:
            print(
                "[DEBUG] International Send button failed; "
                f"falling back to shared selector: {exc}",
                file=sys.stderr,
            )
        await super()._submit_initial(page)
