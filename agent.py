import json
import os
import sys

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from tools import fetch_webpage_text, screenshot_webpage


# ***** Config *****
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
NVIDIA_INVOKE_URL = os.getenv("NVIDIA_INVOKE_URL")
# build.nvidia.com default; set NVIDIA_MODEL in .env if NVIDIA rotates catalog ids.
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "google/gemma-3-27b-it").strip()
stream=True

def _check_config():
    """Fail fast if required env vars are missing."""
    if not NVIDIA_API_KEY:
        print("NVIDIA_API_KEY is not set. Add it to your .env file.")
        sys.exit(1)

# ***** Load agent brain & memory *****
def load_context() -> str:
    try:
        with open("agent.md", "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return "You are a helpful web research agent. Analyse pages thoroughly."

# def load_memory() -> str:
#     try:
#         with open("memory.md", "r") as f:
#             return f.read().strip()
#     except FileNotFoundError:
#         return "No memory yet."

# def update_memory(entry: str):
#     with open("memory.md", "a") as f:
#         f.write(f"\n- {entry}")

def load_memory() -> str:
    try:
        with open("memory.md", "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "No memory yet."

def update_memory(entry: str):
    with open("memory.md", "a", encoding="utf-8") as f:
        f.write(f"\n- {entry}")


# ***** THINK: NVIDIA vision chat completions *****
def think(image_b64: str, text_content: str, goal: str) -> str:
    """
    Sends the page screenshot + extracted text to the NVIDIA vision model
    and streams the response token-by-token to the terminal.
    """
    agent_context = load_context()
    memory        = load_memory()

    prompt = f"""{agent_context}

## Memory of past sessions
{memory}

---
## Current goal
{goal}

## Instructions
You have been given:
1. A screenshot of the webpage (image below)
2. The extracted visible text from the same page

Use BOTH sources together to reason about the page.
Focus on: layout, headings, buttons, forms, key messages, and page structure.

## Extracted text (first 3 000 chars)
{text_content[:3000]}
"""

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }

    payload = {
        "model": NVIDIA_MODEL,
        "messages": [
            {
                "role": "user",
                "content": (
                    f'{prompt}\n\n'
                    f'<img src="data:image/jpeg;base64,{image_b64}" />'
                ),
            }
        ],
        "max_tokens": 512,
        "temperature": 0.20,
        "top_p": 0.70,
        "stream": stream,
    }

    try:
        response = requests.post(
            NVIDIA_INVOKE_URL,
            headers=headers,
            json=payload,
            timeout=max(5, int(os.getenv("IRIS_TIMEOUT_MS", "10000")) // 1000),
        )
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Vision API request failed: {e}")
        return ""

    # Stream tokens live to the terminal
    result = ""
    for line in response.iter_lines():
        if not line:
            continue
        decoded = line.decode("utf-8")
        if decoded.startswith("data: ") and decoded != "data: [DONE]":
            try:
                chunk = json.loads(decoded[6:])
                delta = chunk["choices"][0]["delta"].get("content", "")
                print(delta, end="", flush=True)
                result += delta
            except (json.JSONDecodeError, KeyError):
                pass

    print()  # newline after stream ends
    return result

# ***** Main observe → think → act loop *****
def run_agent(url: str, goal: str) -> str:
    _check_config()

    print(f"\n🔍  OBSERVE — capturing {url} ...")

    # 1. OBSERVE
    screenshot = screenshot_webpage(url)
    if not screenshot["success"]:
        print(f"Screenshot failed: {screenshot['error']}")
        return

    text = fetch_webpage_text(url)
    title = text.get("title", "Unknown")
    print(f"Captured: {title}")

    if not text.get("success"):
        print(f"Text fetch warning: {text.get('error', 'unknown error')}")

    # 2. THINK
    print(f"\n THINK — reasoning about the page ...\n")
    result = think(
        image_b64=screenshot["image_b64"],
        text_content=text.get("content", ""),
        goal=goal,
    )

    # 3. ACT
    update_memory(f"Analysed '{title}' at {url} | Goal: {goal}")
    print(f"\n ACT — memory updated.")

    return result


# ***** Entry point *****
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].lower() in ("iris", "--iris"):
        from iris_runner import main as iris_main

        iris_main()
    else:
        _url = input("Enter a URL: ").strip()
        _goal = input("What do you want to know? (Enter for default): ").strip()
        if not _goal:
            _goal = "Summarise this page — cover layout, key sections, and main message."
        run_agent(_url, _goal)
