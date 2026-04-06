import base64
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

try:
    from playwright_stealth import stealth_sync
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False


# ***** Shared headers that mimic a real browser *****
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-PK,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.google.com/",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def fetch_webpage_text(url: str) -> dict:
    """Scrapes visible text content from a webpage using requests + BeautifulSoup."""
    try:
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)

        response = session.get(url, timeout=15, allow_redirects=True)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "noscript"]):
            tag.decompose()

        title = soup.title.string.strip() if soup.title else "No title"
        body_text = soup.get_text(separator="\n", strip=True)

        return {
            "success": True,
            "title": title,
            "content": body_text[:5000],
        }
    except Exception as e:
        return {"success": False, "error": str(e), "title": "Error", "content": ""}


def screenshot_webpage(url: str, path: str = "screenshot.jpg") -> dict:
    """
    Takes a viewport screenshot using a stealth Playwright browser.
    Returns JPEG (quality 70) as a base64-encoded string for the vision API.
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )

            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=BROWSER_HEADERS["User-Agent"],
                locale="en-PK",
                timezone_id="Asia/Karachi",
                extra_http_headers={
                    "Accept-Language": "en-PK,en;q=0.9",
                    "Referer": "https://www.google.com/",
                },
            )

            page = context.new_page()

            # Patch: remove the webdriver flag that WAFs detect
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            # Apply full stealth patch if available
            if STEALTH_AVAILABLE:
                stealth_sync(page)

            page.goto(url, wait_until="networkidle", timeout=30_000)

            # Brief pause — lets JS-rendered content settle
            page.wait_for_timeout(1500)

            page.screenshot(
                path=path,
                full_page=False,
                type="jpeg",
                quality=70,
            )
            browser.close()

        with open(path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode()

        # NVIDIA vision API limit
        if len(image_b64) >= 180_000:
            return {
                "success": False,
                "error": "Screenshot too large for vision model. Try a simpler page.",
            }

        return {"success": True, "image_b64": image_b64, "path": path}

    except Exception as e:
        return {"success": False, "error": str(e)}
