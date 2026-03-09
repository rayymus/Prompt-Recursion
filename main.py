#!/usr/bin/env python3
import asyncio
import os
import shutil
import tempfile
import json
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Page,
)

# --- CONFIGURATION ---
CHROME_EXEC = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_USER_DATA_DIR = os.path.expanduser("~/Library/Application Support/Google/Chrome")
SOURCE_PROFILE_DIR_NAME = "Default"  

ORDER = ["ChatGPT", "Claude", "Perplexity", "Gemini"]
EVALUATION_BOT = "ChatGPT"
RECURSION_LOOPS = 1
EXTRACTION_TIMEOUT = 120


BOT_CONFIGS = {
    "ChatGPT": {"url": "https://chatgpt.com/", "input": "#prompt-textarea", "assistant": "div[data-message-author-role='assistant']"},
    "Claude": {
        "url": "https://claude.ai/", 
        "input": "div[contenteditable='true']", 
        "assistant": ".font-claude-response"
    },
    "Perplexity": {"url": "https://www.perplexity.ai/", "input": "textarea, div[contenteditable='true']", "assistant": ".prose"},
    "Gemini": {"url": "https://gemini.google.com/", "input": "div[role='textbox']", "assistant": "message-content, div[aria-label='Response']"},
}

# --- HELPER: SURGICAL PROFILE COPY ---
def create_minimal_profile_copy(src_root: Path, src_profile_name: str, dst_root: Path):
    src_profile = src_root / src_profile_name
    dst_profile = dst_root / "Default"
    dst_profile.mkdir(parents=True, exist_ok=True)

    must_copy = [
        "Cookies", "Network", "Login Data", "Preferences", 
        "Secure Preferences", "Web Data", "Local Storage", 
        "Session Storage", "Sessions", "IndexedDB", "Favicons"
    ]
    ignore_patterns = shutil.ignore_patterns("*.tmp", "Cache*", "Code Cache*", "Service Worker*")

    for item in must_copy:
        src_item = src_profile / item
        dst_item = dst_profile / item
        try:
            if src_item.is_dir():
                shutil.copytree(src_item, dst_item, symlinks=True, dirs_exist_ok=True, ignore=ignore_patterns)
            elif src_item.exists():
                shutil.copy2(src_item, dst_item)
        except Exception: pass

    for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket", "Lockfile"]:
        try: (dst_profile / lock).unlink(missing_ok=True)
        except Exception: pass

    try:
        prefs_path = dst_profile / "Preferences"
        if prefs_path.exists():
            with open(prefs_path, "r", encoding="utf-8") as f:
                prefs = json.load(f)
            prefs.setdefault("profile", {})
            prefs["profile"]["exit_type"] = "Normal"
            prefs["profile"]["exited_cleanly"] = True
            for key in ["signin", "sync", "account_info", "invalidation", "google"]:
                prefs.pop(key, None)
            with open(prefs_path, "w", encoding="utf-8") as f:
                json.dump(prefs, f)
    except Exception: pass

# --- ADAPTER ---
class ChatbotAdapter:
    def __init__(self, name, config):
        self.name = name
        self.url = config['url']
        self.selector = config['input']
        self.assistant_selector = config['assistant']
        self.page: Page = None

    async def extract_response(self) -> str:
        try:
            # Try/Except block prevents the Perplexity navigation crash
            elems = await self.page.query_selector_all(self.assistant_selector)
            if elems:
                text = await elems[-1].inner_text()
                if text and text.strip(): 
                    return text.strip()
        except Exception:
            pass
        return ""

    async def submit_and_get_response(self, prompt: str, *, submit_only: bool=False) -> Optional[str]:
        await self.page.bring_to_front()
        await asyncio.sleep(0.5)
        
        initial_response = await self.extract_response()

        try:
            input_el = await self.page.wait_for_selector(self.selector, state="visible", timeout=15000)
            await input_el.scroll_into_view_if_needed()
            await input_el.click(force=True)
            
            # CLEAR AND FILL 
            tag = await input_el.evaluate("el => el.tagName.toLowerCase()")
            if tag in ("textarea", "input"):
                await input_el.fill("")
                # Type the last character manually to trigger React 'onChange' events
                if len(prompt) > 1:
                    await input_el.fill(prompt[:-1])
                    await self.page.keyboard.type(prompt[-1])
                else:
                    await self.page.keyboard.type(prompt)
            else:
                await input_el.focus()
                await self.page.keyboard.press("Meta+A")
                await self.page.keyboard.press("Backspace")
                await asyncio.sleep(0.2)
                if len(prompt) > 1:
                    await self.page.keyboard.insert_text(prompt[:-1])
                    await self.page.keyboard.type(prompt[-1])
                else:
                    await self.page.keyboard.type(prompt)
        except Exception as e:
            print(f"[{self.name}] Input error: {e}", flush=True)
            return None
        
        # Force a small pause for the UI to realize text was entered
        await asyncio.sleep(0.8)

        # Standardized Send Button Attempt
        send_selectors = [
            'button[aria-label*="Send" i]', 
            'button[aria-label*="Submit" i]',
            'button[data-testid*="send" i]',
            'div[role="button"][aria-label*="Send" i]',
            'button:has-text("Send")',
            'button[type="submit"]'
        ]
        
        sent = False
        for s in send_selectors:
            try:
                btn = await self.page.query_selector(s)
                # Check if button is actually enabled
                if btn and await btn.is_enabled():
                    await btn.click(force=True)
                    sent = True
                    break
            except:
                continue
                
        if not sent: 
            print(f"[{self.name}] Button failed. Using Enter fallback.", flush=True)
            await self.page.keyboard.press("Enter")

        if submit_only: return
            
        # EXTRACTION LOGIC
        await asyncio.sleep(2.0)
        last_text = ""
        stable_count = 0
        
        for _ in range(EXTRACTION_TIMEOUT): 
            await asyncio.sleep(1)
            cur_text = await self.extract_response()
            
            # Ensure we aren't just reading the same old message or an empty string
            if not cur_text or cur_text == initial_response:
                continue
                
            if cur_text == last_text:
                stable_count += 1
                if stable_count >= 4: # Wait for 4 seconds of no change
                    return cur_text
            else:
                stable_count = 0
                
            last_text = cur_text
            
        return last_text if last_text and last_text != initial_response else None

# --- BROWSER MANAGER ---
class BrowserManager:
    async def start(self):
        print("[BrowserManager] Starting Playwright...", flush=True)
        self.pw_manager = async_playwright()
        self.pw = await self.pw_manager.start()
        
        loop = asyncio.get_running_loop()

        for attempt in range(3):
            print(f"[BrowserManager] Copying Chrome profile (Attempt {attempt+1})...", flush=True)
            self.tmp = Path(tempfile.mkdtemp())
            
            await loop.run_in_executor(None, create_minimal_profile_copy, Path(CHROME_USER_DATA_DIR), SOURCE_PROFILE_DIR_NAME, self.tmp)
            
            chrome_args = [
                "--no-first-run", "--no-default-browser-check", "--disable-session-crashed-bubble",
                "--disable-infobars", "--test-type", "--ignore-certificate-errors", "--disable-blink-features=AutomationControlled"
            ]
            
            try:
                self.context = await self.pw.chromium.launch_persistent_context(
                    str(self.tmp), executable_path=CHROME_EXEC, headless=False,
                    ignore_https_errors=True, args=chrome_args, viewport=None,
                    ignore_default_args=["--use-mock-keychain", "--password-store=basic", "--enable-automation"]
                )
                break
            except Exception as e:
                shutil.rmtree(self.tmp, ignore_errors=True)
                if attempt == 2: raise
                await asyncio.sleep(2)
        
        print(f"[BrowserManager] Starting Bot Adapters. Order: {ORDER}", flush=True)
        self.bots = {}
        for name in ORDER:
            bot = ChatbotAdapter(name, BOT_CONFIGS[name])
            bot.page = await self.context.new_page()
            try:
                await bot.page.goto(bot.url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass # Timeout on load doesn't mean page is broken
            self.bots[name] = bot
            
        if ORDER: await self.bots[ORDER[0]].page.bring_to_front()

    async def stop(self):
        if hasattr(self, 'context'): 
            try: await self.context.close()
            except: pass
        if hasattr(self, 'pw_manager'): 
            try: await self.pw_manager.__aexit__()
            except: pass
        if hasattr(self, 'tmp'): shutil.rmtree(self.tmp, ignore_errors=True)

# --- MAIN LOOP ---
async def main():
    manager = BrowserManager()
    try:
        await manager.start()
        print("\n[main] Ready. Sessions loaded automatically.", flush=True)
        
        while True:
            user_input = input("\nEnter Prompt (or QUIT): ").strip()
            if user_input.upper() == "QUIT": break
            if not user_input: continue
            
            original_prompt = user_input
            current_content = original_prompt
            last_valid_response = ""
            
            for _ in range(RECURSION_LOOPS):
                for name in ORDER:
                    print(f"\n[{name}] Generating...", flush=True)
                    try:
                        resp = await manager.bots[name].submit_and_get_response(current_content)
                        if resp:
                            resp_str = str(resp)
                            last_valid_response = resp_str
                            print(f"[{name}] Received {len(resp_str)} chars. Snippet: {resp_str[:50]}...", flush=True)
                            
                            current_content = f"Original Prompt: {original_prompt}\n\nPlease review and improve this response, return only your improved response:\n\n{resp_str}"
                        else:
                            print(f"[{name}] No response received. Continuing with previous prompt.", flush=True)
                    except Exception as e:
                        print(f"[{name}] Unexpected error, skipping to next bot: {e}", flush=True)
                    
                    print(f"[{name}] Pausing for 2 seconds before switching tabs...", flush=True)
                    await asyncio.sleep(2.0)
            
            if last_valid_response:
                print("\n[Evaluation] Submitting final response to Evaluation Bot...", flush=True)
                evaluation_input = f"This is a response to the original prompt '{original_prompt}'. Please review and improve it. Your response should be just your improved response and nothing else. The response: \n{last_valid_response}"
                await manager.bots[EVALUATION_BOT].submit_and_get_response(evaluation_input, submit_only=True)
            else:
                print("\n[Evaluation] All bots failed. No response to evaluate.", flush=True)
            
    finally:
        await manager.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user. Exiting gracefully...", flush=True)
    except Exception as e:
        if "Connection closed" not in str(e): raise

# standard-markdown grid-cols-1 grid [&_>_*]:min-w-0 gap-3 standard-markdown
# font-claude-response-body break-words whitespace-normal leading-[1.7]