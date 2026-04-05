import json
import os

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from tools import fetch_webpage_text, screenshot_webpage

# ***** Config *****
nvidia_api_key = os.getenv("NVIDIA_API_KEY")
nvidia_invoke_url = os.getenv("NVIDIA_INVOKE_URL")

# ***** Load agent brain & memory *****
def load_context() -> str:
    with open("agents.md", "r") as f:
        return f.read()

def load_memory() -> str:
    try:
        with open("memory.md", "r") as f:
            return f.read()
    except FileNotFoundError:
        return "No memory yet."

def update_memory(entry: str):
    with open("memory.md", "a") as f:
        f.write(f"\n- {entry}")


# ***** THINK: call phi-3.5-vision-instruct *****
def think(image_b64: str, text_content: str, goal: str) -> str:
    """Sends screenshot + text to the vision model and streams the response."""
    if not nvidia_api_key or not nvidia_invoke_url:
        raise RuntimeError(
            "NVIDIA_API_KEY and INVOKE_URL error"
        )

    agent_context = load_context()
    memory        = load_memory()

    prompt = f"""
{agent_context}

Memory of past sessions:
{memory}

---
Your current goal: {goal}

You have been given a screenshot of the webpage (image below) AND
the extracted text. Use BOTH to reason about the page.

Look for: layout, headings, images, buttons, key message, structure.

Extracted text (first 3000 chars):
{text_content[:3000]}
"""

    headers = {
        "Authorization": f"Bearer {nvidia_api_key}",
        "Accept": "text/event-stream"
    }

    payload = {
        "model": "microsoft/phi-3.5-vision-instruct",
        "messages": [
            {
                "role": "user",
                "content": f'{prompt}\n<img src="data:image/png;base64,{image_b64}" />'
            }
        ],
        "max_tokens": 1024,
        "temperature": 0.20,
        "top_p": 0.70,
        "stream": True
    }

    response = requests.post(nvidia_invoke_url, headers=headers, json=payload)

    # Stream response live to terminal
    result = ""
    for line in response.iter_lines():
        if line:
            decoded = line.decode("utf-8")
            if decoded.startswith("data: ") and decoded != "data: [DONE]":
                try:
                    chunk = json.loads(decoded[6:])
                    delta = chunk["choices"][0]["delta"].get("content", "")
                    print(delta, end="", flush=True)
                    result += delta
                except json.JSONDecodeError:
                    pass
    print()  # newline after stream ends
    return result


# ***** Main observe → think → act loop *****
def run_agent(url: str, goal: str):
    print(f"\n🔍 OBSERVE: Taking screenshot of {url}...")

    # OBSERVE
    screenshot = screenshot_webpage(url)
    if not screenshot["success"]:
        print(f"Screenshot failed: {screenshot['error']}")
        return

    text = fetch_webpage_text(url)
    title = text.get("title", "Unknown")
    print(f"Captured: {title}")

    # THINK
    print(f"\n THINK: Reasoning about the page...\n")
    result = think(
        image_b64=screenshot["image_b64"],
        text_content=text.get("content", ""),
        goal=goal
    )

    # ACT
    print(f"\n ACT: Memory updated.")
    update_memory(f"Analysed '{title}' | Goal: {goal}")

    return result


# ***** Entry point *****
if __name__ == "__main__":
    url  = input("Enter a URL: ").strip()
    goal = input("What do you want to know? (Enter for default summary): ").strip()
    if not goal:
        goal = "Summarise this page — cover layout, key sections, and main message."
    run_agent(url, goal)