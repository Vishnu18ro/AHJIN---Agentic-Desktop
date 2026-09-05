"""BrowserTool — Live browser navigation, observation, and interaction layer via Playwright."""

import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, cast

import structlog

from ahjin.core.errors import AhjinError, ErrorCategory
from ahjin.tools.base import BaseTool, ToolInvocationRequest, ToolInvocationResult

async_playwright: Any = None
try:
    from playwright.async_api import (  # pyright: ignore[reportMissingImports]
        async_playwright as _ap,  # pyright: ignore[reportMissingImports,reportUnknownVariableType]
    )

    async_playwright = cast(Any, _ap)
except ImportError:
    async_playwright = None

logger = structlog.get_logger()

_MAX_OBSERVATION_TEXT_LEN = 2500
_MAX_INTERACTIVE_ELEMENTS = 25
_ACTION_BUDGET_PER_TASK = 10

_SIDE_EFFECT_KEYWORDS = frozenset({
    "send_message",
    "post_comment",
    "submit_payment",
    "purchase",
    "delete_account",
    "send_email",
    "submit_form",
})

_AUTHENTICATION_INDICATORS = (
    "scan qr code",
    "log in to whatsapp",
    "sign in to continue",
    "enter your password",
    "2-step verification",
    "use whatsapp on your computer",
)

_VERIFICATION_INDICATORS = (
    "sorry/index",
    "unusual traffic",
    "verify you are human",
    "enter the characters you see",
    "captcha",
    "bot verification",
)


def _verify_and_focus_os_window(browser_obj: Any) -> Dict[str, Any]:
    """Inspects Windows HWND for browser process, verifies visibility, and brings window to top."""
    info: Dict[str, Any] = {
        "hwnd": None,
        "pid": getattr(getattr(browser_obj, "process", None), "pid", None),
        "visible": False,
        "minimized": False,
        "title": "",
    }
    if sys.platform != "win32":
        info["visible"] = True
        return info

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]

        b_pid = info["pid"]
        candidates: list[dict[str, Any]] = []

        def enum_cb(hwnd: int, lparam: int) -> bool:
            w_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(w_pid))
            if b_pid is None or w_pid.value == b_pid:
                is_vis = bool(user32.IsWindowVisible(hwnd))
                length = user32.GetWindowTextW(hwnd, None, 0)
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buff, length + 1)
                candidates.append({
                    "hwnd": hwnd,
                    "pid": w_pid.value,
                    "visible": is_vis,
                    "minimized": bool(user32.IsIconic(hwnd)),
                    "title": buff.value,
                })
            return True

        user32.EnumWindows(WNDENUMPROC(enum_cb), 0)

        for cand in candidates:
            if cand["visible"] or b_pid is not None:
                info["hwnd"] = cand["hwnd"]
                info["pid"] = cand["pid"]
                info["visible"] = True
                info["minimized"] = cand["minimized"]
                info["title"] = cand["title"]

                user32.ShowWindow(cand["hwnd"], 9)  # SW_RESTORE
                user32.ShowWindow(cand["hwnd"], 5)  # SW_SHOW
                user32.SetForegroundWindow(cand["hwnd"])
                break
    except Exception as exc:
        logger.debug("Win32 OS window verification note", error=str(exc))
        info["visible"] = True

    return info


class BrowserSessionManager:
    """Manages an active Playwright browser session across multi-step tasks."""

    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._action_count: int = 0

    @property
    def is_active(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    @property
    def action_count(self) -> int:
        return self._action_count

    def increment_action_count(self) -> None:
        self._action_count += 1

    def reset_action_count(self) -> None:
        self._action_count = 0

    async def get_or_create_page(self, headless: bool = False) -> Any:
        if self._page is not None and not getattr(self._page, "is_closed", lambda: True)():
            try:
                await self._page.bring_to_front()
            except Exception:
                pass
            logger.info(
                "[Browser] Window visible — session remains active",
                session_id=hex(id(self)),
            )
            return self._page

        if async_playwright is None:
            raise RuntimeError(
                "PLAYWRIGHT_NOT_INSTALLED: Playwright package is not installed "
                "in current Python environment."
            )

        try:
            if self._playwright is None:
                self._playwright = await async_playwright().start()

            pw = self._playwright
            if self._browser is None or not getattr(self._browser, "is_connected", lambda: False)():
                self._browser = await pw.chromium.launch(
                    headless=headless,
                    args=["--start-maximized", "--no-sandbox", "--disable-setuid-sandbox"],
                )
                pid = getattr(getattr(self._browser, "process", None), "pid", "unknown")
                exec_path = getattr(self._browser, "_executable_path", "chromium")
                logger.info(
                    "[Browser] Launching visible browser",
                    pid=pid,
                    executable=exec_path,
                    visible=not headless,
                    headless=headless,
                    session_id=hex(id(self)),
                )
            else:
                pid = getattr(getattr(self._browser, "process", None), "pid", "unknown")
                logger.info(
                    "[Browser] Window visible — session remains active",
                    pid=pid,
                    session_id=hex(id(self)),
                )

            browser_obj = self._browser
            if self._context is None:
                self._context = await browser_obj.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                )

            context_obj = self._context
            self._page = await context_obj.new_page()
            try:
                await self._page.bring_to_front()
            except Exception:
                pass
            os_win = _verify_and_focus_os_window(self._browser)
            logger.info(
                "[Browser] OS-level Window state verified",
                hwnd=os_win.get("hwnd"),
                pid=os_win.get("pid"),
                visible=os_win.get("visible"),
                title=os_win.get("title"),
            )
            self._action_count = 0
            return self._page
        except Exception as exc:
            err_str = str(exc)
            logger.error("Failed to launch Playwright browser session", error=err_str)
            has_missing_exec = (
                "Executable doesn't exist" in err_str
                or ("chromium" in err_str.lower() and "install" in err_str.lower())
            )
            if has_missing_exec:
                raise RuntimeError(
                    f"BROWSER_BINARY_MISSING: Chromium binary missing — {exc}"
                ) from exc
            elif "BROWSER_LAUNCH_FAILED" in err_str or "launch" in err_str.lower():
                raise RuntimeError(f"BROWSER_LAUNCH_FAILED: {exc}") from exc
            else:
                raise RuntimeError(f"BROWSER_RUNTIME_ERROR: {exc}") from exc

    async def close(self) -> None:
        try:
            if self._page and not self._page.is_closed():
                await self._page.close()
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as exc:
            logger.warning("Error closing browser session", error=str(exc))
        finally:
            logger.info("Browser closed", session_id=hex(id(self)))
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None
            self._action_count = 0


# Global browser session instance
global_browser_session = BrowserSessionManager()


class BrowserTool(BaseTool):
    """Tool for controlling a live Playwright browser session."""

    def __init__(self, session_manager: BrowserSessionManager | None = None) -> None:
        self.session = session_manager or global_browser_session

    @property
    def tool_name(self) -> str:
        return "browser"

    async def execute(self, request: ToolInvocationRequest) -> ToolInvocationResult:
        t0 = time.monotonic()
        action = str(request.parameters.get("action", "")).strip().lower()

        # Infer action if omitted but specific parameters are provided
        if not action:
            if "url" in request.parameters:
                action = "navigate"
            elif "text" in request.parameters and "selector" in request.parameters:
                action = "type"
            elif "selector" in request.parameters:
                action = "click"
            else:
                action = "observe"

        # Normalize action aliases
        if action in ("open", "browser_open", "launch"):
            action = "open"
        elif action in ("navigate", "browser_navigate", "goto", "open_url"):
            action = "navigate"
        elif action in ("observe", "browser_observe", "read", "inspect"):
            action = "observe"
        elif action in ("click", "browser_click"):
            action = "click"
        elif action in ("type", "browser_type", "fill", "input"):
            action = "type"
        elif action in ("press", "browser_press", "submit"):
            action = "press"
        elif action in ("scroll", "browser_scroll"):
            action = "scroll"
        elif action in ("screenshot", "browser_screenshot", "capture"):
            action = "screenshot"
        elif action in ("close", "browser_close", "quit", "exit"):
            action = "close"

        # Check action budget
        if action != "close" and self.session.action_count >= _ACTION_BUDGET_PER_TASK:
            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=(
                    "[BROWSER OBSERVATION]\n"
                    "Status: ACTION_BUDGET_EXCEEDED\n"
                    f"Message: Browser action budget limit ({_ACTION_BUDGET_PER_TASK}) reached.\n"
                    "[/BROWSER OBSERVATION]"
                ),
                error=AhjinError(
                    code="ACTION_BUDGET_EXCEEDED",
                    message="Browser action step limit reached.",
                    category=ErrorCategory.TOOL,
                ),
                latency_ms=latency_ms,
            )

        # Check confirmation requirement for side-effect operations
        is_side_effect = request.parameters.get("is_side_effect") is True or any(
            kw in action for kw in _SIDE_EFFECT_KEYWORDS
        )
        if is_side_effect and not request.parameters.get("confirm"):
            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=(
                    "[BROWSER OBSERVATION]\n"
                    "Status: CONFIRMATION_REQUIRED\n"
                    "Message: This browser action has external side effects. "
                    "Explicit user confirmation is required before proceeding.\n"
                    "[/BROWSER OBSERVATION]"
                ),
                error=AhjinError(
                    code="CONFIRMATION_REQUIRED",
                    message="User confirmation required for browser side-effect action.",
                    category=ErrorCategory.TOOL,
                ),
                latency_ms=latency_ms,
            )

        try:
            if action == "close":
                await self.session.close()
                latency_ms = (time.monotonic() - t0) * 1000.0
                return ToolInvocationResult(
                    invocation_id=request.invocation_id,
                    success=True,
                    output=(
                        "[BROWSER OBSERVATION]\n"
                        "Status: CLOSED\n"
                        "Message: Browser session closed successfully.\n"
                        "[/BROWSER OBSERVATION]"
                    ),
                    latency_ms=latency_ms,
                )

            # Get or launch page
            headless = request.parameters.get("headless", False) is True
            raw_page = await self.session.get_or_create_page(headless=headless)
            page: Any = raw_page
            self.session.increment_action_count()

            delay_ms_param = request.parameters.get(
                "delay_ms",
                int(os.environ.get("BROWSER_ACTION_DELAY_MS", "500")),
            )
            action_delay_ms = int(delay_ms_param) if str(delay_ms_param).isdigit() else 500

            if action == "open":
                url = str(request.parameters.get("url", "https://www.google.com")).strip()
                logger.info("[Browser] Opening URL", url=url)
                await page.goto(url, wait_until="commit", timeout=30000)
                if action_delay_ms > 0:
                    await page.wait_for_timeout(action_delay_ms)
                output = await self._generate_observation(page)

            elif action == "navigate":
                url = str(request.parameters.get("url", "")).strip()
                if not url:
                    raise ValueError("No URL provided for navigation.")
                if not url.startswith(("http://", "https://")):
                    url = f"https://{url}"
                logger.info("[Browser] Navigating to URL", url=url)
                await page.goto(url, wait_until="commit", timeout=30000)
                if action_delay_ms > 0:
                    await page.wait_for_timeout(action_delay_ms)
                output = await self._generate_observation(page)

            elif action == "observe":
                logger.info("[Browser] Observing page")
                output = await self._generate_observation(page)

            elif action == "click":
                selector = str(request.parameters.get("selector", "")).strip()
                description = str(request.parameters.get("description", "")).strip()
                logger.info(
                    "[Browser] Clicking element",
                    selector=selector,
                    description=description,
                )

                clicked = False
                if selector:
                    try:
                        await page.click(selector, timeout=5000)
                        clicked = True
                    except Exception:
                        clicked = False

                if not clicked and description:
                    # Fallback text click
                    try:
                        await page.get_by_text(description, exact=False).click(timeout=5000)
                        clicked = True
                    except Exception:
                        clicked = False

                if not clicked:
                    latency_ms = (time.monotonic() - t0) * 1000.0
                    return ToolInvocationResult(
                        invocation_id=request.invocation_id,
                        success=False,
                        output=(
                            "[BROWSER OBSERVATION]\n"
                            f"Status: ELEMENT_NOT_FOUND\n"
                            f"Message: Could not find clickable element for selector '{selector}' "
                            f"or description '{description}'.\n"
                            "[/BROWSER OBSERVATION]"
                        ),
                        error=AhjinError(
                            code="ELEMENT_NOT_FOUND",
                            message="Target element not found.",
                            category=ErrorCategory.TOOL,
                        ),
                        latency_ms=latency_ms,
                    )
                if action_delay_ms > 0:
                    await page.wait_for_timeout(action_delay_ms)
                output = await self._generate_observation(page)

            elif action == "type":
                selector = str(request.parameters.get("selector", "")).strip()
                text = str(request.parameters.get("text", ""))
                logger.info("[Browser] Typing text", selector=selector, text=text)

                typed = False
                if selector:
                    try:
                        await page.fill(selector, text, timeout=5000)
                        typed = True
                    except Exception:
                        typed = False

                if not typed:
                    # Try common input selectors if selector was generic or failed
                    for fallback_sel in [
                        "input[type='text']",
                        "textarea",
                        "input[name='q']",
                        "textarea[name='q']",
                        "input",
                    ]:
                        try:
                            await page.fill(fallback_sel, text, timeout=3000)
                            typed = True
                            break
                        except Exception:
                            continue

                if not typed:
                    latency_ms = (time.monotonic() - t0) * 1000.0
                    return ToolInvocationResult(
                        invocation_id=request.invocation_id,
                        success=False,
                        output=(
                            "[BROWSER OBSERVATION]\n"
                            "Status: ELEMENT_NOT_FOUND\n"
                            f"Message: Could not find input field for selector '{selector}'.\n"
                            "[/BROWSER OBSERVATION]"
                        ),
                        error=AhjinError(
                            code="ELEMENT_NOT_FOUND",
                            message="Input field not found.",
                            category=ErrorCategory.TOOL,
                        ),
                        latency_ms=latency_ms,
                    )

                if action_delay_ms > 0:
                    await page.wait_for_timeout(action_delay_ms)

                if request.parameters.get("press_enter") is True:
                    logger.info("[Browser] Pressing Enter key")
                    await page.keyboard.press("Enter")
                    if action_delay_ms > 0:
                        await page.wait_for_timeout(action_delay_ms)

                output = await self._generate_observation(page)

            elif action == "press":
                key = str(request.parameters.get("key", "Enter")).strip()
                logger.info("[Browser] Pressing key", key=key)
                await page.keyboard.press(key)
                if action_delay_ms > 0:
                    await page.wait_for_timeout(action_delay_ms)
                output = await self._generate_observation(page)

            elif action == "scroll":
                direction = str(request.parameters.get("direction", "down")).strip().lower()
                amount_param = request.parameters.get("amount", 800)
                amount = (
                    int(amount_param)
                    if isinstance(amount_param, (int, str)) and str(amount_param).isdigit()
                    else 800
                )
                logger.info("[Browser] Scrolling page", direction=direction, amount=amount)

                delta_y = amount if direction == "down" else -amount
                await page.evaluate(f"window.scrollBy(0, {delta_y})")
                if action_delay_ms > 0:
                    await page.wait_for_timeout(action_delay_ms)
                output = await self._generate_observation(page)

            elif action == "screenshot":
                target_dir = Path(os.getcwd()) / ".scratch_screenshots"
                target_dir.mkdir(exist_ok=True, parents=True)
                file_name = f"screenshot_{uuid.uuid4().hex[:8]}.png"
                screenshot_path = target_dir / file_name

                await page.screenshot(path=str(screenshot_path), full_page=False)

                latency_ms = (time.monotonic() - t0) * 1000.0
                output = (
                    "[BROWSER OBSERVATION]\n"
                    f"URL: {page.url}\n"
                    f"Title: {await page.title()}\n"
                    "Status: SCREENSHOT_CAPTURED\n"
                    f"Screenshot Path: {file_name}\n"
                    "[/BROWSER OBSERVATION]"
                )

            else:
                latency_ms = (time.monotonic() - t0) * 1000.0
                output_str = (
                    f"[BROWSER OBSERVATION]\n"
                    f"Status: UNKNOWN_ACTION '{action}'\n"
                    f"[/BROWSER OBSERVATION]"
                )
                return ToolInvocationResult(
                    invocation_id=request.invocation_id,
                    success=False,
                    output=output_str,
                    error=AhjinError(
                        code="UNKNOWN_ACTION",
                        message=f"Browser action '{action}' is not supported.",
                        category=ErrorCategory.VALIDATION,
                    ),
                    latency_ms=latency_ms,
                )

            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=True,
                output=output,
                latency_ms=latency_ms,
            )

        except Exception as exc:
            logger.warning("Browser action execution failed", action=action, error=str(exc))
            latency_ms = (time.monotonic() - t0) * 1000.0
            err_msg = str(exc)
            err_code = "ACTION_FAILED"
            if "Timeout" in err_msg or "timeout" in err_msg:
                err_code = "TIMEOUT"

            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=(
                    "[BROWSER OBSERVATION]\n"
                    f"Status: {err_code}\n"
                    f"Message: {err_msg}\n"
                    "[/BROWSER OBSERVATION]"
                ),
                error=AhjinError(
                    code=err_code,
                    message=err_msg,
                    category=ErrorCategory.TOOL,
                ),
                latency_ms=latency_ms,
            )

    async def _generate_observation(self, page_input: Any) -> str:
        page: Any = page_input
        url = str(page.url)
        title = str(await page.title())

        # Extract visible page text
        text_content = ""
        try:
            raw_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            if isinstance(raw_text, str):
                lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
                text_content = "\n".join(lines)[:_MAX_OBSERVATION_TEXT_LEN]
        except Exception:
            text_content = "Unable to extract text."

        # Check authentication indicators
        lower_text = text_content.lower()
        if any(indicator in lower_text for indicator in _AUTHENTICATION_INDICATORS):
            return (
                "[BROWSER OBSERVATION]\n"
                f"URL: {url}\n"
                f"Title: {title}\n"
                "Status: AUTHENTICATION_REQUIRED\n"
                "Message: The webpage requires user login or authentication "
                "(e.g. scanning QR code or credentials).\n"
                "Please complete authentication manually in the open browser window.\n"
                "[/BROWSER OBSERVATION]"
            )

        # Check security verification / CAPTCHA indicators
        if any(v_ind in url.lower() or v_ind in lower_text for v_ind in _VERIFICATION_INDICATORS):
            return (
                "[BROWSER OBSERVATION]\n"
                f"URL: {url}\n"
                f"Title: {title}\n"
                "Status: VERIFICATION_REQUIRED\n"
                "Message: The webpage presented a CAPTCHA or security verification challenge.\n"
                "The browser window remains active and visible on your desktop for manual "
                "completion.\n"
                "[/BROWSER OBSERVATION]"
            )

        # Extract interactive elements
        interactive_elements: list[str] = []
        try:
            # Inputs / Textareas
            raw_inputs: Any = await page.query_selector_all("input:not([type='hidden']), textarea")
            if isinstance(raw_inputs, list):
                for inp in cast(list[Any], raw_inputs[:10]):
                    tag = str(await inp.evaluate("el => el.tagName.toLowerCase()"))
                    name = str(await inp.get_attribute("name") or "")
                    placeholder = str(await inp.get_attribute("placeholder") or "")
                    inp_type = str(await inp.get_attribute("type") or "text")
                    val = str(await inp.input_value()) if tag == "input" else ""
                    desc = (
                        f"- Input ({tag}): name='{name}', type='{inp_type}', "
                        f"placeholder='{placeholder}'"
                    )
                    if val:
                        desc += f", value='{val}'"
                    interactive_elements.append(desc)

            # Buttons
            btn_sel = "button, input[type='button'], input[type='submit']"
            raw_buttons: Any = await page.query_selector_all(btn_sel)
            if isinstance(raw_buttons, list):
                for btn in cast(list[Any], raw_buttons[:10]):
                    btn_text = (
                        str((await btn.inner_text())).strip()
                        or str((await btn.get_attribute("value") or "")).strip()
                    )
                    name = str(await btn.get_attribute("name") or "")
                    interactive_elements.append(f"- Button: text='{btn_text}', name='{name}'")

            # Links
            raw_links: Any = await page.query_selector_all("a[href]")
            if isinstance(raw_links, list):
                for link in cast(list[Any], raw_links[:10]):
                    link_text = str((await link.inner_text())).strip()
                    href = str(await link.get_attribute("href") or "")
                    if link_text and not href.startswith("javascript:"):
                        interactive_elements.append(f"- Link: text='{link_text}', href='{href}'")
        except Exception as exc:
            logger.debug("Failed to extract interactive elements", error=str(exc))

        elements_str = (
            "\n".join(interactive_elements[:_MAX_INTERACTIVE_ELEMENTS])
            if interactive_elements
            else "None detected"
        )

        return (
            "[BROWSER OBSERVATION]\n"
            f"URL: {url}\n"
            f"Title: {title}\n"
            f"Action Steps Taken: {self.session.action_count}/{_ACTION_BUDGET_PER_TASK}\n\n"
            f"VISIBLE TEXT:\n{text_content}\n\n"
            f"INTERACTIVE ELEMENTS:\n{elements_str}\n"
            "[/BROWSER OBSERVATION]"
        )
