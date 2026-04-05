import requests
import base64
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


def fetch_webpage_text(url: str) -> dict:
    """Scrapes visible text content from a webpage."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()

        title = soup.title.string if soup.title else "No title"
        body_text = soup.get_text(separator="\n", strip=True)

        return {
            "success": True,
            "title": title,
            "content": body_text[:5000]
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def screenshot_webpage(url: str, path: str = "screenshot.png") -> dict:
    """Takes a screenshot of a webpage and returns it as base64."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(url, wait_until="networkidle")
            page.screenshot(path=path, full_page=False)
            browser.close()

        with open(path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode()

        if len(image_b64) >= 180_000:
            return {"success": False, "error": "Screenshot too large. Try a simpler page."}

        return {"success": True, "image_b64": image_b64, "path": path}

    except Exception as e:
        return {"success": False, "error": str(e)}