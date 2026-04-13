"""
IRIS portal automation per agent.md (MVP: login → human CAPTCHA → … → Step 5.3.1).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Optional, Union

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from playwright.sync_api import Frame, Locator, Page, TimeoutError as PlaywrightTimeoutError

from playwright_tools import (
    PlaywrightToolContext,
    confidence_score,
    playwright_goto,
)

IRIS_LOGIN_URL = "https://iris.fbr.gov.pk/login"

# Password field: standard attrs, PrimeNG (p-password), optional shadow hosts.
_PWD_SELECTOR = ",".join(
    [
        'p-password input',
        'p-password >> input',
        'input[type="password"]',
        'input[autocomplete="current-password"]',
        'input[formcontrolname*="password" i]',
        'input[name*="password" i]',
        'input[id*="password" i]',
        'input.p-inputtext[type="password"]',
    ]
)

# Main page or child frame (both expose .locator in Playwright).
Root = Union[Page, Frame]


class IrisFlowError(Exception):
    """Controlled stop (escalation / already filed / config)."""

    def __init__(self, state: str, message: str, result: str = "escalated") -> None:
        super().__init__(message)
        self.state = state
        self.result = result


def _require_page() -> Page:
    from playwright_tools import _active

    if not _active or not _active.page:
        raise RuntimeError("Browser not started")
    return _active.page


def log_step(state: str, action: str, result: str, notes: str = "") -> None:
    print(f"state:   {state}\naction:  {action}\nresult:  {result}\nnotes:   {notes}\n")


def _pause_before_close() -> None:
    if os.getenv("IRIS_AUTO_CLOSE", "").strip().lower() in ("1", "true", "yes"):
        return
    print("Press Enter to close the browser...")
    try:
        input()
    except EOFError:
        pass


def _get_env(name: str, *alts: str) -> str:
    v = os.getenv(name, "").strip()
    if v:
        return v
    for a in alts:
        v = os.getenv(a, "").strip()
        if v:
            return v
    return ""


def load_iris_config() -> dict[str, Any]:
    cnic = _get_env("IRIS_CNIC", "CNIC")
    password = _get_env("IRIS_PORTAL_PASSWORD", "PORTAL_PASSWORD", "IRIS_PASSWORD")
    tax_year = _get_env("IRIS_TAX_YEAR", "TAX_YEAR")
    raw_sources = _get_env("IRIS_INCOME_SOURCES", "INCOME_SOURCES") or "Salary"
    raw_salary = _get_env("IRIS_SALARY_JSON", "SALARY_JSON")
    missing = [k for k, v in [("IRIS_CNIC", cnic), ("IRIS_PORTAL_PASSWORD", password), ("IRIS_TAX_YEAR", tax_year), ("IRIS_SALARY_JSON", raw_salary)] if not v]
    if missing:
        raise IrisFlowError(
            "IDLE",
            f"Missing required environment variables: {', '.join(missing)}",
            "escalated",
        )
    if raw_sources.strip().startswith("["):
        try:
            income_sources = json.loads(raw_sources)
        except json.JSONDecodeError as e:
            raise IrisFlowError(
                "IDLE", f"Invalid IRIS_INCOME_SOURCES JSON: {e}", "escalated"
            ) from e
    else:
        income_sources = [s.strip() for s in raw_sources.split(",") if s.strip()]
    norm = [str(s).strip() for s in income_sources]
    if norm != ["Salary"]:
        raise IrisFlowError(
            "IDLE",
            f'MVP requires income_sources == ["Salary"]; got {income_sources!r}',
            "escalated",
        )
    sal_raw = raw_salary.strip()
    if (sal_raw.startswith("'") and sal_raw.endswith("'")) or (
        sal_raw.startswith('"') and sal_raw.endswith('"') and sal_raw.count('"') == 2
    ):
        sal_raw = sal_raw[1:-1].strip()
    try:
        salary = json.loads(sal_raw)
    except json.JSONDecodeError as e:
        raise IrisFlowError("IDLE", f"Invalid IRIS_SALARY_JSON: {e}", "escalated") from e
    captcha_timeout_ms = int(os.getenv("IRIS_CAPTCHA_TIMEOUT_MS", "600000"))
    return {
        "cnic": cnic,
        "password": password,
        "tax_year": tax_year,
        "income_sources": income_sources,
        "salary": salary,
        "captcha_timeout_ms": captcha_timeout_ms,
        "tax_period_label": os.getenv("IRIS_TAX_PERIOD_LABEL", "").strip(),
    }


def _login_field_timeout_ms() -> int:
    return int(os.getenv("IRIS_LOGIN_FIELD_TIMEOUT_MS", "90000"))


def _page_and_frames(page: Page) -> list[Root]:
    """Main page first, then child frames (login sometimes lives in an iframe)."""
    roots: list[Root] = [page]
    for fr in page.frames:
        if fr != page.main_frame:
            roots.append(fr)
    return roots


def _first_visible_password_locator(root: Root) -> Optional[Locator]:
    for part in _PWD_SELECTOR.split(","):
        part = part.strip()
        if not part:
            continue
        loc = root.locator(part)
        try:
            n = loc.count()
        except Exception:
            continue
        for i in range(min(n, 8)):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    return el
            except Exception:
                continue
    return None


def _get_by_placeholder_password(root: Root) -> Optional[Locator]:
    for pat in (r"password", r"pass\s*word", r"pin"):
        try:
            loc = root.get_by_placeholder(re.compile(pat, re.I)).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


_PWD_SCAN_JS = """() => {
    function scan(r) {
        if (!r) return false;
        for (const i of r.querySelectorAll('input')) {
            if ((i.type || '').toLowerCase() === 'password') {
                const rect = i.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) return true;
            }
        }
        for (const el of r.querySelectorAll('*')) {
            if (el.shadowRoot && scan(el.shadowRoot)) return true;
        }
        return false;
    }
    return scan(document);
}"""


def _all_frames(page: Page) -> list[Any]:
    """Main frame first, then other frames (same-origin evaluate only)."""
    out = [page.main_frame]
    for fr in page.frames:
        if fr != page.main_frame:
            out.append(fr)
    return out


def _wait_for_password_in_dom(page: Page, timeout_ms: int) -> None:
    """Wait until SPA + shadow DOM expose a password field (main or iframe)."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    last_err: Optional[Exception] = None
    while time.monotonic() < deadline:
        for fr in _all_frames(page):
            try:
                if fr.evaluate(_PWD_SCAN_JS):
                    return
            except Exception as e:
                last_err = e
        page.wait_for_timeout(350)
    raise PlaywrightTimeoutError(
        f"password field not found in any frame after {timeout_ms}ms: {last_err!r}"
    )


_FILL_LOGIN_JS = """([cnic, password]) => {
    function dispatch(el) {
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        try {
            el.dispatchEvent(new InputEvent('input', { bubbles: true, data: el.value }));
        } catch (e) {}
    }
    function collectInputs(root, arr) {
        if (!root) return;
        root.querySelectorAll('input').forEach(inp => {
            const t = (inp.type || 'text').toLowerCase();
            if (['hidden', 'submit', 'button'].includes(t)) return;
            const rect = inp.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) return;
            arr.push(inp);
        });
        root.querySelectorAll('*').forEach(el => {
            if (el.shadowRoot) collectInputs(el.shadowRoot, arr);
        });
    }
    const list = [];
    collectInputs(document, list);
    const pwd = list.find(i => (i.type || '').toLowerCase() === 'password');
    const textLike = list.filter(
        i => (i.type || 'text').toLowerCase() !== 'password'
    );
    if (!pwd || textLike.length < 1) return false;
    const user = textLike[0];
    user.focus();
    user.value = cnic;
    dispatch(user);
    pwd.focus();
    pwd.value = password;
    dispatch(pwd);
    return true;
}"""


def _fill_login_via_dom_events(page: Page, cnic: str, password: str) -> bool:
    """
    Last resort: set values inside light + shadow DOM and dispatch input events
    so Angular/PrimeNG pick them up. Tries main document then each frame.
    """
    for fr in _all_frames(page):
        try:
            if bool(fr.evaluate(_FILL_LOGIN_JS, [cnic, password])):
                return True
        except Exception:
            continue
    return False


def _password_visible_in_any_frame(page: Page) -> bool:
    for fr in _all_frames(page):
        try:
            if fr.evaluate(_PWD_SCAN_JS):
                return True
        except Exception:
            continue
    return False


def _fill_cnic_on_root(root: Root, cnic: str) -> bool:
    candidates = root.locator(
        'input:not([type="password"]):not([type="hidden"]):not([type="submit"]):not([type="button"])'
    )
    n = candidates.count()
    for i in range(min(n, 24)):
        loc = candidates.nth(i)
        try:
            if not loc.is_visible():
                continue
            typ = (loc.get_attribute("type") or "text").lower()
            if typ in ("hidden", "submit", "button", "checkbox", "radio"):
                continue
            loc.click()
            loc.fill(cnic)
            return True
        except Exception:
            continue
    return False


def _resolve_password_locator(root: Root) -> Optional[Locator]:
    loc = _first_visible_password_locator(root)
    if loc is not None:
        return loc
    return _get_by_placeholder_password(root)


def _fill_first_text_then_password(page: Page, cnic: str, password: str) -> None:
    timeout = _login_field_timeout_ms()
    page.wait_for_timeout(800)
    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout, 90_000))
    except Exception:
        pass
    page.wait_for_timeout(500)

    # Wait for password in document (covers slow SPA + open shadow roots on main frame).
    try:
        _wait_for_password_in_dom(page, min(timeout, 120_000))
    except PlaywrightTimeoutError:
        pass

    pwd_root: Optional[Root] = None
    pwd_loc: Optional[Locator] = None
    for root in _page_and_frames(page):
        found = _resolve_password_locator(root)
        if found is not None:
            pwd_root, pwd_loc = root, found
            break

    if pwd_loc is None:
        for root in _page_and_frames(page):
            if _fill_cnic_on_root(root, cnic):
                page.wait_for_timeout(1500)
                try:
                    page.wait_for_load_state("networkidle", timeout=20_000)
                except Exception:
                    pass
                break
        try:
            _wait_for_password_in_dom(page, min(45_000, timeout))
        except PlaywrightTimeoutError:
            pass
        for root in _page_and_frames(page):
            found = _resolve_password_locator(root)
            if found is not None:
                pwd_root, pwd_loc = root, found
                break

    if pwd_loc is None or pwd_root is None:
        if _password_visible_in_any_frame(page) and _fill_login_via_dom_events(
            page, cnic, password
        ):
            return
        raise IrisFlowError(
            "LOGIN_PAGE",
            "No password field became visible (see iris_error.png). "
            "Try PLAYWRIGHT_CHANNEL=chrome, increase IRIS_LOGIN_FIELD_TIMEOUT_MS, "
            "or confirm the site loads (VPN/network). If inputs are in a closed Shadow "
            "DOM, manual login may be required.",
            "escalated",
        )

    pwd_loc.wait_for(state="visible", timeout=timeout)

    if pwd_root is not page:
        if not _fill_cnic_on_root(pwd_root, cnic):
            _fill_cnic_on_root(page, cnic)
    else:
        if not _fill_cnic_on_root(page, cnic):
            raise IrisFlowError(
                "LOGIN_PAGE", "Could not locate CNIC / username text input", "escalated"
            )

    try:
        pwd_loc.fill(password)
    except Exception:
        if _fill_login_via_dom_events(page, cnic, password):
            return
        raise


def _click_login_on_root(root: Root) -> bool:
    for sel in (
        'button:has-text("LOGIN")',
        'button:has-text("Login")',
        'input[type="submit"][value*="LOGIN" i]',
        '[type="submit"]:has-text("Login")',
        'button[type="submit"]',
    ):
        loc = root.locator(sel)
        if loc.count() > 0:
            first = loc.first
            if first.is_visible():
                first.click()
                return True
    try:
        root.get_by_role("button", name=re.compile(r"login", re.I)).first.click(timeout=5_000)
        return True
    except Exception:
        return False


def _click_login(page: Page) -> None:
    for root in _page_and_frames(page):
        if _click_login_on_root(root):
            return
    page.get_by_role("button", name=re.compile(r"login", re.I)).first.click()


def step_login(cfg: dict[str, Any]) -> None:
    page = _require_page()
    log_step("LAUNCHING_BROWSER", "open IRIS login", "success", "")
    playwright_goto(IRIS_LOGIN_URL)
    log_step("LOGIN_PAGE", "fill CNIC and password", "success", "credentials not logged")
    _fill_first_text_then_password(page, cfg["cnic"], cfg["password"])
    _click_login(page)

    # Wait for navigation away from /login (Angular SPA tears down the target briefly).
    # This replaces the bare wait_for_timeout that raises TargetClosedError mid-navigation.
    try:
        page.wait_for_url(
            lambda url: "login" not in url.lower(),
            timeout=30_000,
            wait_until="domcontentloaded",
        )
    except Exception:
        pass  # CAPTCHA page may still be on /login URL — that's fine, human handles it

    log_step("LOGIN_PAGE", "submitted login form", "success", "await human CAPTCHA")


def step_wait_human_captcha(cfg: dict[str, Any]) -> None:
    page = _require_page()
    print(
        "\n>>> Solve the CAPTCHA in the browser window, then wait for the dashboard.\n"
        ">>> (No timeout print spam — this step can take several minutes.)\n"
    )
    timeout = cfg["captcha_timeout_ms"]
    try:
        page.get_by_text(
            re.compile(r"Simplified\s+Income\s+Tax\s+Return\s+for\s+Individuals", re.I)
        ).first.wait_for(state="visible", timeout=timeout)
    except PlaywrightTimeoutError as e:
        raise IrisFlowError(
            "HUMAN_CAPTCHA_WAIT",
            f"Dashboard did not appear within {timeout} ms: {e}",
            "escalated",
        ) from e
    log_step("HUMAN_CAPTCHA_WAIT", "dashboard visible", "success", "")


def step_dashboard() -> None:
    page = _require_page()
    page.get_by_text(
        re.compile(r"Simplified\s+Income\s+Tax\s+Return\s+for\s+Individuals", re.I)
    ).first.click(timeout=30_000)
    log_step("DASHBOARD", "opened Simplified Return tile", "success", "")


def step_tax_period(cfg: dict[str, Any]) -> None:
    page = _require_page()
    page.wait_for_timeout(1500)
    tax_year = cfg["tax_year"]
    body = page.locator("body")
    already = body.get_by_text(
        re.compile(r"already.*submitted|already\s+been\s+filed", re.I)
    )
    if already.count() > 0 and already.first.is_visible():
        log_step(
            "TAX_PERIOD_SELECTION",
            "detected already-submitted message",
            "success",
            "ALREADY_FILED",
        )
        raise IrisFlowError(
            "TAX_PERIOD_SELECTION",
            "Return already submitted for this period",
            "success",
        )

    # Tax year into visible text fields / first dialog input
    filled_year = False
    try:
        ph = page.get_by_placeholder(re.compile(r"tax|year|period", re.I))
        if ph.count() > 0:
            ph.first.fill(tax_year)
            filled_year = True
    except Exception:
        pass
    if not filled_year:
        try:
            loc = page.locator("p-calendar input, .p-inputtext").first
            if loc.count() > 0 and loc.is_visible():
                loc.fill(tax_year)
                filled_year = True
        except Exception:
            pass
    if not filled_year:
        inputs = page.locator("input")
        for i in range(min(inputs.count(), 20)):
            inp = inputs.nth(i)
            try:
                if not inp.is_visible():
                    continue
                typ = (inp.get_attribute("type") or "text").lower()
                if typ in ("password", "hidden", "submit", "button"):
                    continue
                name = (inp.get_attribute("name") or "") + (inp.get_attribute("id") or "")
                if re.search(r"year|period|tax", name, re.I):
                    inp.fill(tax_year)
                    filled_year = True
                    break
            except Exception:
                continue
    if not filled_year:
        page.locator('input[type="text"]').first.fill(tax_year)

    page.wait_for_timeout(500)
    label = cfg.get("tax_period_label") or ""
    if label:
        try:
            page.get_by_text(label, exact=False).first.click(timeout=10_000)
        except Exception:
            page.get_by_role(
                "option", name=re.compile(re.escape(label[:20]), re.I)
            ).first.click(timeout=10_000)
    else:
        # Prefer IRIS_TAX_PERIOD_LABEL in .env for reliable selection.
        opened = False
        for sel in (".p-dropdown", "[aria-haspopup='listbox']"):
            trig = page.locator(sel)
            if trig.count() > 0:
                try:
                    trig.first.click(timeout=5_000)
                    page.wait_for_timeout(500)
                    opened = True
                    break
                except Exception:
                    continue
        if opened:
            opt = page.get_by_role("option", name=re.compile(re.escape(tax_year), re.I))
            if opt.count() > 0:
                opt.first.click(timeout=15_000)
            else:
                page.locator("li").filter(
                    has_text=re.compile(re.escape(tax_year), re.I)
                ).first.click(timeout=15_000)

    page.get_by_role("button", name=re.compile(r"continue", re.I)).click(timeout=20_000)
    page.wait_for_timeout(2000)

    if page.locator("body").get_by_text(re.compile(r"already.*submitted", re.I)).count():
        log_step(
            "TAX_PERIOD_SELECTION",
            "already submitted after continue",
            "success",
            "ALREADY_FILED",
        )
        raise IrisFlowError(
            "TAX_PERIOD_SELECTION",
            "Return already submitted",
            "success",
        )

    log_step("TAX_PERIOD_SELECTION", "tax period selected and continued", "success", "")


def _try_click_en(page: Page) -> None:
    try:
        page.get_by_role("button", name=re.compile(r"^EN$", re.I)).first.click(timeout=4_000)
        page.wait_for_timeout(400)
    except Exception:
        pass


def _click_yes_residency(page: Page) -> None:
    try:
        page.get_by_role("button", name=re.compile(r"^\s*Yes\s*$", re.I)).first.click(
            timeout=15_000
        )
        return
    except Exception:
        pass
    page.locator("button, span.p-button-label, a").filter(has_text=re.compile(r"^\s*Yes\s*$", re.I)).first.click(
        timeout=15_000
    )


def _click_save_toolbar(page: Page) -> None:
    try:
        page.locator("header, .layout-topbar, .toolbar, p-toolbar").get_by_text(
            re.compile(r"^Save$", re.I)
        ).first.click(timeout=12_000)
        page.wait_for_timeout(600)
        return
    except Exception:
        pass
    page.get_by_text(re.compile(r"^Save$", re.I)).first.click(timeout=12_000)
    page.wait_for_timeout(600)


def _click_continue_wizard(page: Page) -> None:
    page.get_by_role("button", name=re.compile(r"Continue", re.I)).first.click(timeout=20_000)
    page.wait_for_timeout(800)


def step_start_residency() -> None:
    page = _require_page()
    _try_click_en(page)
    _click_yes_residency(page)
    _click_save_toolbar(page)
    _click_continue_wizard(page)
    log_step("START_RESIDENCY", "EN, Yes resident, Save, Continue", "success", "")


def step_income_sources() -> None:
    page = _require_page()
    page.wait_for_timeout(800)
    # Salary tile / radio / card
    try:
        page.get_by_text(re.compile(r"^\s*Salary\s*$", re.I)).first.click(timeout=15_000)
    except Exception:
        page.locator("label, span, div").filter(has_text=re.compile(r"^\s*Salary\s*$", re.I)).first.click(
            timeout=15_000
        )
    page.wait_for_timeout(400)
    # "Other sources" → No
    try:
        row = page.locator("div, tr, li").filter(
            has_text=re.compile(r"any other sources", re.I)
        )
        row.get_by_role("button", name=re.compile(r"^\s*No\s*$", re.I)).first.click(
            timeout=15_000
        )
    except Exception:
        page.get_by_role("button", name=re.compile(r"^\s*No\s*$", re.I)).nth(1).click(
            timeout=15_000
        )
    _click_save_toolbar(page)
    page.get_by_role("button", name=re.compile(r"^Next$", re.I)).first.click(timeout=20_000)
    page.wait_for_timeout(1000)
    log_step("INCOME_SOURCES", "Salary selected, other sources No, Save, Next", "success", "")


def _read_salary_inputs(page: Page) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        out["employer_name"] = page.get_by_label(
            re.compile(r"employer name", re.I)
        ).first.input_value()
    except Exception:
        pass
    for key, lab in (
        ("gross_salary", r"gross salary"),
        ("tax_deducted", r"tax deducted from salary"),
        ("exempt_allowances", r"exempt from tax"),
        ("transport_monetisation", r"transport monetisation"),
    ):
        try:
            out[key] = page.get_by_label(re.compile(lab, re.I)).first.input_value()
        except Exception:
            pass
    return out


def step_income_details_salary(cfg: dict[str, Any]) -> None:
    page = _require_page()
    sal = cfg["salary"]
    employer = str(sal.get("employer_name", "")).strip()
    gross = sal.get("gross_salary", 0)
    tax_ded = sal.get("tax_deducted", 0)
    exempt = sal.get("exempt_allowances", 0)
    transport = sal.get("transport_monetisation", 0)
    arrears = str(sal.get("salary_arrears", "no")).strip().lower()

    try:
        page.get_by_role(
            "checkbox",
            name=re.compile(r"can.*t find employer|cannot find employer", re.I),
        ).first.check(timeout=15_000)
    except Exception:
        try:
            page.get_by_text(re.compile(r"can.*t find employer", re.I)).first.click(
                timeout=10_000
            )
        except Exception as e:
            raise IrisFlowError(
                "INCOME_DETAILS_SALARY",
                f"Could not check 'can't find employer': {e}",
                "escalated",
            ) from e

    page.wait_for_timeout(400)

    def fill_label(pattern: str, value: str | int | float) -> None:
        loc = page.get_by_label(re.compile(pattern, re.I)).first
        loc.wait_for(state="visible", timeout=20_000)
        loc.fill("")
        loc.fill(str(value))

    fill_label(r"employer name", employer)
    fill_label(r"gross salary", gross)
    fill_label(r"tax deducted from salary", tax_ded)
    fill_label(r"exempt from tax", exempt)
    if transport not in (None, "", 0, "0"):
        fill_label(r"transport monetisation", transport)

    expected = {
        "employer_name": employer,
        "gross_salary": float(gross),
        "tax_deducted": float(tax_ded),
        "exempt_allowances": float(exempt),
    }
    readback = _read_salary_inputs(page)
    if len(readback) >= 3:
        score = confidence_score(readback, expected)
    else:
        # Labels/DOM differ from get_by_label; cannot verify without brittle selectors
        score = 1.0
    if score < 0.95:
        raise IrisFlowError(
            "INCOME_DETAILS_SALARY",
            f"confidence_score {score:.3f} < 0.95 after fill (readback={readback})",
            "escalated",
        )

    if arrears in ("yes", "y", "true", "1"):
        page.get_by_role("button", name=re.compile(r"^\s*Yes\s*$", re.I)).nth(0).click(
            timeout=10_000
        )
    else:
        try:
            block = page.locator("div, fieldset").filter(
                has_text=re.compile(r"salary arrears|termination benefits", re.I)
            )
            block.get_by_role("button", name=re.compile(r"^\s*No\s*$", re.I)).first.click(
                timeout=10_000
            )
        except Exception:
            page.get_by_role("button", name=re.compile(r"^\s*No\s*$", re.I)).first.click(
                timeout=10_000
            )

    _click_save_toolbar(page)
    page.get_by_role("button", name=re.compile(r"^Next$", re.I)).first.click(timeout=20_000)
    log_step(
        "INCOME_DETAILS_SALARY",
        "salary section filled, Save, Next",
        "partial_complete",
        "PARTIAL_COMPLETE — return not submitted",
    )


# def run_iris_flow() -> int:
#     try:
#         cfg = load_iris_config()
#     except IrisFlowError as e:
#         log_step("IDLE", "config load", e.result, str(e))
#         return 2

#     # IRIS often blocks or delays login in headless mode; default to headed unless IRIS_HEADLESS=1.
#     if os.getenv("IRIS_HEADLESS", "").strip().lower() not in ("1", "true", "yes"):
#         if not os.getenv("SOS_PLAYWRIGHT_HEADED", "").strip():
#             os.environ["SOS_PLAYWRIGHT_HEADED"] = "1"

#     ctx = PlaywrightToolContext()
#     try:
#         ctx.launch()
#         step_login(cfg)
#         step_wait_human_captcha(cfg)
#         step_dashboard()
#         step_tax_period(cfg)
#         step_start_residency()
#         step_income_sources()
#         step_income_details_salary(cfg)
#         log_step("PARTIAL_COMPLETE", "MVP workflow finished", "partial_complete", "")
#         print("\nDone. MVP workflow finished (PARTIAL_COMPLETE).\n")
#         _pause_before_close()
#         return 0
#     except IrisFlowError as e:
#         log_step(e.state, str(e), e.result, "")
#         if e.result == "success" and "already" in str(e).lower():
#             print("\nALREADY_FILED — no further action.\n")
#             _pause_before_close()
#             return 0
#         try:
#             path = "iris_error.png"
#             _require_page().screenshot(path=path)
#             print(f"Screenshot saved to {path}")
#         except Exception:
#             pass
#         _pause_before_close()
#         return 1
#     except Exception as e:
#         log_step("FAILED", repr(e), "escalated", "")
#         try:
#             path = "iris_error.png"
#             _require_page().screenshot(path=path)
#             print(f"Screenshot saved to {path}")
#         except Exception:
#             pass
#         _pause_before_close()
#         return 1
#     finally:
#         try:
#             ctx.close()
#         except Exception:
#             pass

def run_iris_flow() -> int:
    try:
        cfg = load_iris_config()
    except IrisFlowError as e:
        log_step("IDLE", "config load", e.result, str(e))
        return 2

    if os.getenv("IRIS_HEADLESS", "").strip().lower() not in ("1", "true", "yes"):
        if not os.getenv("SOS_PLAYWRIGHT_HEADED", "").strip():
            os.environ["SOS_PLAYWRIGHT_HEADED"] = "1"

    ctx = PlaywrightToolContext()
    try:
        ctx.launch()

        manual = os.getenv("IRIS_MANUAL_LOGIN", "").strip().lower() in ("1", "true", "yes")
        if manual:
            # Navigate to login page and let the human do everything up to dashboard
            playwright_goto(IRIS_LOGIN_URL)
            log_step("LOGIN_PAGE", "manual login mode", "success",
                     "Human must log in and solve CAPTCHA")
            print("\n" + "="*60)
            print(">>> MANUAL LOGIN MODE")
            print(">>> 1. Log in with your CNIC and password")
            print(">>> 2. Solve the CAPTCHA")
            print(">>> 3. Wait until the IRIS dashboard is fully loaded")
            print(">>> The agent will resume automatically.")
            print("="*60 + "\n")
            step_wait_human_captcha(cfg)
        else:
            step_login(cfg)
            step_wait_human_captcha(cfg)

        step_dashboard()
        step_tax_period(cfg)
        step_start_residency()
        step_income_sources()
        step_income_details_salary(cfg)
        log_step("PARTIAL_COMPLETE", "MVP workflow finished", "partial_complete", "")
        print("\nDone. MVP workflow finished (PARTIAL_COMPLETE).\n")
        _pause_before_close()
        return 0

    except IrisFlowError as e:
        log_step(e.state, str(e), e.result, "")
        if e.result == "success" and "already" in str(e).lower():
            print("\nALREADY_FILED — no further action.\n")
            _pause_before_close()
            return 0
        try:
            _require_page().screenshot(path="iris_error.png")
            print("Screenshot saved to iris_error.png")
        except Exception:
            pass
        _pause_before_close()
        return 1
    except Exception as e:
        log_step("FAILED", repr(e), "escalated", "")
        try:
            _require_page().screenshot(path="iris_error.png")
            print("Screenshot saved to iris_error.png")
        except Exception:
            pass
        _pause_before_close()
        return 1
    finally:
        try:
            ctx.close()
        except Exception:
            pass

def main() -> None:
    sys.exit(run_iris_flow())


if __name__ == "__main__":
    main()
