"""
Playwright session and tools aligned with agent.md (IRIS automation).
"""
from __future__ import annotations

import base64
import json
import os
import re
from typing import Any, Optional

import requests
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

try:
    from playwright_stealth import stealth_sync

    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _chromium_launch_kwargs() -> dict:
    headed = os.environ.get("SOS_PLAYWRIGHT_HEADED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    raw_slow = os.environ.get("SOS_PLAYWRIGHT_SLOW_MO_MS", "").strip()
    if raw_slow:
        slow_mo = max(0, int(raw_slow))
    else:
        slow_mo = 300 if headed else 0
    kwargs: dict = {"headless": not headed}
    if slow_mo > 0:
        kwargs["slow_mo"] = slow_mo
    return kwargs


_active: Optional["PlaywrightToolContext"] = None


class PlaywrightToolContext:
    """Single-browser session for IRIS flows."""

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    def launch(self) -> Page:
        global _active
        self._playwright = sync_playwright().start()
        launch_kw = _chromium_launch_kwargs()
        ch = os.environ.get("PLAYWRIGHT_CHANNEL", "").strip()
        if ch:
            # e.g. PLAYWRIGHT_CHANNEL=chrome uses installed Google Chrome (skip bundled Chromium download)
            launch_kw["channel"] = ch
        self._browser = self._playwright.chromium.launch(
            **launch_kw,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                # "--disable-gpu",
            ],
        )
        self._context = self._browser.new_context(
            viewport={"width": 1365, "height": 900},
            user_agent=BROWSER_HEADERS["User-Agent"],
            locale="en-PK",
            timezone_id="Asia/Karachi",
            extra_http_headers={
                "Accept-Language": "en-PK,en;q=0.9",
            },
        )
        self.page = self._context.new_page()
        self.page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        if STEALTH_AVAILABLE:
            stealth_sync(self.page)
        _active = self
        return self.page

    def close(self) -> None:
        global _active
        try:
            if self._context:
                self._context.close()
        finally:
            try:
                if self._browser:
                    self._browser.close()
            finally:
                if self._playwright:
                    self._playwright.stop()
                self.page = None
                self._context = None
                self._browser = None
                self._playwright = None
                if _active is self:
                    _active = None


def _require_page() -> Page:
    if _active is None or _active.page is None:
        raise RuntimeError("No Playwright session; call PlaywrightToolContext.launch() first.")
    return _active.page


# def playwright_goto(url: str, timeout: int = 90_000) -> None:
#     page = _require_page()
#     page.goto(url, wait_until="domcontentloaded", timeout=timeout)
#     page.wait_for_timeout(800)

def playwright_goto(url: str, timeout: int = 90_000) -> None:
    page = _require_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    except Exception:
        # networkidle may never fire on Angular SPAs; domcontentloaded is enough
        pass
    page.wait_for_timeout(1500)
    # Warn if page is blank
    body_text = page.evaluate("() => document.body?.innerText?.trim() || ''")
    if not body_text:
        print(f"[warn] playwright_goto: page body is empty after loading {url}")
        print(f"[warn] current url: {page.url!r} | title: {page.title()!r}")

def playwright_click(selector: str, timeout: int = 30_000) -> None:
    page = _require_page()
    page.locator(selector).first.click(timeout=timeout)


def playwright_fill(selector: str, value: str, timeout: int = 30_000) -> None:
    page = _require_page()
    loc = page.locator(selector).first
    loc.wait_for(state="visible", timeout=timeout)
    loc.fill("", timeout=timeout)
    loc.fill(str(value), timeout=timeout)


def playwright_check(selector: str, timeout: int = 30_000) -> None:
    page = _require_page()
    page.locator(selector).first.check(timeout=timeout)


def playwright_screenshot() -> str:
    """Return PNG base64 of the viewport."""
    page = _require_page()
    raw = page.screenshot(type="png")
    return base64.b64encode(raw).decode("ascii")


def playwright_wait(selector: str, timeout: int = 30_000) -> None:
    page = _require_page()
    page.locator(selector).first.wait_for(state="visible", timeout=timeout)


def capsolver_solve(image_b64: str) -> str:
    raise NotImplementedError(
        "capsolver_solve is reserved for a future phase; complete CAPTCHA manually."
    )


def vision_identify(screenshot_b64: str, question: str) -> dict[str, Any]:
    """
    Optional NVIDIA vision fallback (same stack as agent.py).
    Returns {success, text or error}.
    """
    api_key = os.getenv("NVIDIA_API_KEY")
    url = os.getenv("NVIDIA_INVOKE_URL")
    if not api_key or not url:
        return {
            "success": False,
            "error": "NVIDIA_API_KEY / NVIDIA_INVOKE_URL not set; cannot run vision_identify.",
        }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    model = os.getenv("NVIDIA_MODEL", "meta/llama-3.2-90b-vision-instruct").strip()
    payload = {
        "model": model or "meta/llama-3.2-90b-vision-instruct",
        "messages": [
            {
                "role": "user",
                "content": (
                    f"{question}\nAnswer concisely.\n\n"
                    f'<img src="data:image/png;base64,{screenshot_b64}" />'
                ),
            }
        ],
        "max_tokens": 512,
        "temperature": 0.2,
        "stream": False,
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=90)
        r.raise_for_status()
        data = r.json()
        text = data["choices"][0]["message"]["content"]
        return {"success": True, "text": text}
    except Exception as e:
        return {"success": False, "error": str(e)}


def confidence_score(filled_values: dict[str, Any], expected_values: dict[str, Any]) -> float:
    """Compare filled vs expected; 1.0 = all match."""
    if not expected_values:
        return 1.0
    scores: list[float] = []
    for key, exp in expected_values.items():
        got = filled_values.get(key)
        if got is None:
            scores.append(0.0)
            continue
        if isinstance(exp, (int, float)) and isinstance(got, (int, float)):
            scores.append(1.0 if abs(float(got) - float(exp)) < 0.015 else 0.0)
        elif isinstance(exp, (int, float)) or isinstance(got, (int, float)):
            try:
                scores.append(
                    1.0 if abs(float(got) - float(exp)) < 0.015 else 0.0
                )
            except (TypeError, ValueError):
                scores.append(0.0)
        else:
            a = str(got).strip().lower()
            b = str(exp).strip().lower()
            scores.append(1.0 if a == b else (0.85 if b in a or a in b else 0.0))
    return sum(scores) / len(scores) if scores else 1.0
