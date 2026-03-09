"""
prompt_maxxing

Spawns 4 chatbots in the background. Accepts unlimited prompts until the user types 'QUIT'.

Manually log in, especially to Claude, on all chatbots beforehand on Chrome. Close all chrome instances before running.
"""

import asyncio
import os
import shutil
import subprocess
import tempfile
import time
from math import ceil, sqrt
from pathlib import Path

from playwright.async_api import async_playwright

CHROME_EXEC = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_USER_DATA_DIR = os.path.expanduser("~/Library/Application Support/Google/Chrome")
SOURCE_PROFILE_DIR_NAME = os.environ.get("CHROME_PROFILE_DIR", "Default")

sites = [
    {
        "name": "ChatGPT",
        "url": "https://chat.openai.com/",
        "selectors": ["textarea[placeholder*='Send a message']", "textarea", "div[contenteditable='true']"],
    },
    {
        "name": "Gemini",
        "url": "https://gemini.google.com/",
        "selectors": ["textarea", "div[contenteditable='true']"],
    },
    {
        "name": "Claude",
        "url": "https://claude.ai/",
        "selectors": ["textarea", "div[contenteditable='true']"],
    },
    {
        "name": "Perplexity",
        "url": "https://www.perplexity.ai/",
        "selectors": ["textarea", "div[contenteditable='true']"],
    },
]

ORIGINS = {
        sites[0]["name"]: "top left",
        sites[1]["name"]: "-50% -25%",
        sites[2]["name"]: "top left",
        sites[3]["name"]: "-50% 25%"
    }


def get_screen_bounds():
    try:
        p = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to tell application process "Finder" to get value of attribute "AXFrame" of window 1'],
            capture_output=True,
            text=True,
            check=True,
        )
    except:
        pass

    # Reliable fallback: subtract menu bar manually
    return (0, 40, 1440, 900)

def set_front_window_bounds(left, top, right, bottom):
    applescript = f'tell application "Google Chrome" to set bounds of front window to {{{left}, {top}, {right}, {bottom}}}'
    subprocess.run(["osascript", "-e", applescript])

def bring_all_chrome_windows_forward():
    bot_names = [s["name"] for s in sites]
    apps_list = "{" + ", ".join([f'"{name}"' for name in bot_names]) + "}"

    applescript = f"""
    set targetTitles to {apps_list}

    tell application "System Events"
        repeat with tTitle in targetTitles
            set chromeProcs to (every process whose name is "Google Chrome")
            repeat with p in chromeProcs
                try
                    if exists (windows of p whose title contains tTitle) then
                        set frontmost of p to true
                        delay 0.3
                        set matchWindows to (every window of p whose title contains tTitle)
                        repeat with w in matchWindows
                            perform action "AXRaise" of w
                            delay 0.2
                        end repeat
                        exit repeat -- stop after finding correct process
                    end if
                end try
            end repeat
        end repeat
    end tell
    """

    subprocess.run(["osascript", "-e", applescript])


def create_minimal_profile_copy(src_root: Path, src_profile_name: str, dst_root: Path, site: str) -> Path:
    """
    Copies full profile storage to maintain logins, but surgically patches the 
    Preferences file to strip out Chrome Sync/Identity triggers that cause crashes.
    """
    src_profile = src_root / src_profile_name
    dst_profile = dst_root / "Default"
    dst_root.mkdir(parents=True, exist_ok=True)
    dst_profile.mkdir(parents=True, exist_ok=True)


    # 2. Define files to copy (Unified list for all bots)
    must_copy = [
        "Cookies",           # Legacy Chrome location
        "Network",           # Modern Chrome location (contains Cookies DB)
        "Login Data", 
        "Preferences",       # MUST be copied so Chrome accepts the cookies
        "Secure Preferences", 
        "Web Data", 
        "Local Storage", 
        "Session Storage", 
        "Sessions", 
        "IndexedDB", 
        "Service Worker",
        "Favicons"
    ]

    # 3. Perform the copy
    for item in must_copy:
        src_item = src_profile / item
        dst_item = dst_profile / item
        try:
            if src_item.is_dir():
                shutil.copytree(src_item, dst_item, symlinks=True, dirs_exist_ok=True)
            elif src_item.exists():
                shutil.copy2(src_item, dst_item)
        except Exception:
            pass

    # 4. Clean up lock files to prevent SQLite crashes
    for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket", "Lockfile"]:
        try:
            (dst_profile / lock).unlink(missing_ok=True)
        except Exception:
            pass

    # 5. Patch Preferences to strip out Sync and prevent the crash
    try:
        prefs_path = dst_profile / "Preferences"
        if prefs_path.exists():
            import json
            with open(prefs_path, "r", encoding="utf-8") as f:
                prefs = json.load(f)
            
            # Suppress "Restore Pages?" crashed bubble
            prefs.setdefault("profile", {})
            prefs["profile"]["exit_type"] = "Normal"
            prefs["profile"]["exited_cleanly"] = True
            
            # CRITICAL: Strip browser-level Identity/Sync data.
            # This stops Chrome from reaching out to Google's servers to validate the 
            # browser session, which prevents the `OnGetTokenFailure` crash.
            # (Web cookies remain untouched so ChatGPT/Claude stay logged in).
            for key in ["signin", "sync", "account_info", "invalidation", "google"]:
                prefs.pop(key, None)
            
            with open(prefs_path, "w", encoding="utf-8") as f:
                json.dump(prefs, f)
    except Exception as e:
        print(f"Warning: Failed to patch Preferences: {e}")

    return dst_root


async def wait_for_ready(page, site: dict):
    """Wait for Cloudflare or specific sites to fully render."""
    await page.wait_for_timeout(3500)
    
    # Zoom out so content fits better in a small tiled window (75% scale)
    scale = 0.65

    origin = ORIGINS.get(site["name"], "top left")
    try:
            if site["name"] == "Perplexity":
                await page.wait_for_timeout(1200)
            await page.add_style_tag(content=f"""
                html {{
                    transform: scale({scale});
                    transform-origin: {origin};
                    width: {100/scale}%;
                    height: {100/scale}%;
                }}
            """)
        # await page.add_style_tag(content="body { zoom: 0.75 !important; }")
    except Exception:
        pass
        
    # Look for Google SSO "Continue" popup frame on ChatGPT
    if site["name"] == "ChatGPT":
        for frame in page.frames:
            if "google.com" in frame.url or "smartlock" in frame.url:
                try:
                    loc = frame.locator("text=Continue").first
                    if await loc.count() > 0:
                        await loc.click(force=True)
                        await page.wait_for_timeout(1000)
                except Exception:
                    pass


async def find_input_handle_anywhere(page, selectors, timeout_ms=20000):
    """Search page and all frames for a visible & editable element matching selectors.
       Also checks for role=textbox/contenteditable variants commonly used by Perplexity.
    """
    deadline = time.monotonic() + (timeout_ms / 1000.0)

    # extra candidate selectors common on Perplexity-like UIs
    extra_selectors = [
        'div[role="textbox"][contenteditable="true"]',
        'div[contenteditable="true"][role="textbox"]',
        'div[role="textbox"]',
        '[aria-label*="Ask"]',
        '[aria-label*="Send"]'
    ]
    combined = selectors + extra_selectors

    while time.monotonic() < deadline:
        # search main page
        for sel in combined:
            try:
                el = await page.query_selector(sel)
                if el:
                    try:
                        if await el.is_visible() and await el.is_editable():
                            return el, page  # return element and associated frame (page)
                    except Exception:
                        # some elements may be contenteditable but not "editable" per Playwright api
                        return el, page
            except Exception:
                pass

        # search in frames
        for frame in page.frames:
            for sel in combined:
                try:
                    el = await frame.query_selector(sel)
                    if el:
                        try:
                            if await el.is_visible() and await el.is_editable():
                                return el, frame
                        except Exception:
                            return el, frame
                except Exception:
                    pass

        await page.wait_for_timeout(250)
    return None, None

# ---------- improved submit_prompt using the above ----------
async def submit_prompt(page, site, prompt):
    """Submits the prompt into an already-open window (searching frames, clicking & typing)."""
    try:
        input_handle, input_frame = await find_input_handle_anywhere(page, site["selectors"], timeout_ms=25000)
        if not input_handle:
            print(f"[{site['name']}] input box not found (you may need to adjust selectors).")
            return

        # Ensure we have the frame/page to operate keyboard on
        target_page = input_frame or page

        # Click the element first (force) to ensure focus; sometimes placeholder overlay must be clicked
        try:
            # Scroll into view first (CRITICAL for Gemini)
            try:
                await input_handle.scroll_into_view_if_needed()
            except:
                pass

            # Triple focus strategy
            try:
                await input_handle.click(force=True)
                await target_page.wait_for_timeout(200)
                await input_handle.evaluate("el => el.focus()")
                await target_page.wait_for_timeout(200)
            except:
                pass
        except Exception:
            # Last resort: evaluate a .focus() in the element's frame context
            try:
                await input_handle.evaluate("el => el.focus && el.focus()")
                await target_page.wait_for_timeout(120)
            except Exception:
                pass

        tag = await input_handle.evaluate("el => el.tagName.toLowerCase()")
        if tag in ("textarea", "input"):
            # use fill for textareas/inputs
            try:
                await input_handle.fill(prompt)
            except Exception:
                # fallback to typing
                await target_page.keyboard.type(prompt, delay=10)
        else:
            # contenteditable / React editors: use keyboard typing (more robust)
            try:
                # clear any existing selection / placeholder by selecting all then deleting
                await target_page.keyboard.press("Meta+A")
                await target_page.keyboard.press("Backspace")
            except Exception:
                pass
            # insert text like a user would
            await target_page.keyboard.insert_text(prompt)

            if site["name"] == "Gemini":
                await input_handle.evaluate("""(el) => {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""")

        await target_page.wait_for_timeout(400)

        # Try clicking common send buttons within same frame
        send_selectors = [
            'button[aria-label="Submit"]',
            'button[data-testid="send-button"]',
            'button[data-test-id="send-button"]',
            'button[aria-label="Send message"]',
            'button[aria-label*="Send message"]',
            'button[aria-label*="Send"]',
            'div[role="button"][aria-label*="Send"]',
            'button:has-text("Send")',
            'button:has-text("Ask")',
            'button[type=submit]'
        ]
        sent = False
        # search send buttons in the frame where input was found, then fallback to page
        frames_to_search = [input_frame] if input_frame else [page]
        frames_to_search += [page]  # ensure main page is also checked

        for fr in frames_to_search:
            if fr is None:
                continue
            for ss in send_selectors:
                try:
                    btn = await fr.query_selector(ss)
                    if btn:
                        try:
                            await btn.scroll_into_view_if_needed()
                        except Exception:
                            pass
                        if (not await btn.is_disabled()):
                            try:
                                await btn.click()
                            except Exception:
                                await btn.click(force=True)
                        sent = True
                        break
                except Exception:
                    pass
            if sent:
                break

        if not sent:
            # Try ARIA role based button detection (much more reliable)
            try:
                send_button = input_frame.get_by_role("button", name="Submit")
                if await send_button.count() > 0:
                    await send_button.first.click()
                    sent = True
            except:
                pass

            if not sent:
                try:
                    send_button = input_frame.get_by_role("button", name="Send")
                    if await send_button.count() > 0:
                        await send_button.first.click()
                        sent = True
                except:
                    pass

        if not sent:
            # Fallback keyboard sends
            if site["name"] == "Gemini":
                await target_page.keyboard.press("Enter")
                await target_page.wait_for_timeout(150)
                await target_page.keyboard.press("Meta+Enter")
            else:
                await target_page.keyboard.press("Meta+Enter")

        # If input still has content, attempt one more forced send (Gemini can be finicky)
        await target_page.wait_for_timeout(200)
        try:
            if tag in ("textarea", "input"):
                remaining = await input_handle.input_value()
            else:
                remaining = await input_handle.evaluate("el => el.innerText || el.textContent || ''")
        except Exception:
            remaining = ""

        if remaining.strip():
            if site["name"] == "Gemini":
                try:
                    frame_for_js = input_frame or page
                    await frame_for_js.evaluate(
                        """
                        () => {
                            const btn = document.querySelector(
                                'button[aria-label="Send message"],' +
                                'button[aria-label*="Send"],' +
                                'div[role="button"][aria-label*="Send"],' +
                                'button[type="submit"]'
                            );
                            if (btn) btn.click();
                        }
                        """
                    )
                except Exception:
                    pass
                await target_page.wait_for_timeout(200)
                try:
                    if tag in ("textarea", "input"):
                        remaining = await input_handle.input_value()
                    else:
                        remaining = await input_handle.evaluate("el => el.innerText || el.textContent || ''")
                except Exception:
                    remaining = ""

        if remaining.strip():
            print(f"[{site['name']}] prompt entered but not sent (send button not found).")
        else:
            print(f"[{site['name']}] prompt submitted.")
    except Exception as e:
        print(f"[{site['name']}] error: {e}")

async def slide_window_onscreen(browser_context, page, bounds):
    """Slide window smoothly onscreen using native Chrome DevTools Protocol"""
    left, top, right, bottom = bounds
    width = right - left
    height = bottom - top
    
    try:
        cdp = await browser_context.new_cdp_session(page)
        target_info = await cdp.send("Target.getTargetInfo")
        target_id = target_info["targetInfo"]["targetId"]
        window_for_target = await cdp.send("Browser.getWindowForTarget", {"targetId": target_id})
        window_id = window_for_target["windowId"]
        
        await cdp.send("Browser.setWindowBounds", {
            "windowId": window_id,
            "bounds": {
                "left": left,
                "top": top,
                "width": width,
                "height": height,
                "windowState": "normal"
            }
        })
    except Exception as e:
        print("Failed to slide window via CDP, ignoring.")


async def main():
    if not Path(CHROME_EXEC).exists():
        print("Chrome executable not found at", CHROME_EXEC)
        return
    chrome_root = Path(CHROME_USER_DATA_DIR)
    if not chrome_root.exists():
        print("Chrome user data dir not found at", chrome_root)
        return

    left0, top0, right0, bottom0 = get_screen_bounds()
    screen_w = right0 - left0
    screen_h = bottom0 - top0
    n = len(sites)
    cols = int(ceil(sqrt(n)))
    rows = int(ceil(n / cols))
    tile_w = max(420, screen_w // cols)
    tile_h = max(300, screen_h // rows)

    tmp_root = Path(tempfile.mkdtemp(prefix="chrome_profiles_"))
    
    print("Initialize background browsers...")
    bots = []
    
    try:
        async with async_playwright() as p:
            # 1. Boot all 4 browsers completely hidden in the background
            for idx, site in enumerate(sites):
                dst = tmp_root / f"profile_{idx}"
                create_minimal_profile_copy(chrome_root, SOURCE_PROFILE_DIR_NAME, dst, site["name"])

                row = idx // cols
                col = idx % cols
                left = left0 + col * tile_w
                top = top0 + row * tile_h
                right = left + tile_w
                bottom = top + tile_h
                
                width = right - left
                height = bottom - top

                chrome_args = [
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-session-crashed-bubble",
                    "--disable-infobars",
                    "--test-type",  # Hides the "--no-sandbox" warning banner
                    "--ignore-certificate-errors",
                    # f"--window-position=-3000,0",  # Initialize totally offscreen
                    f"--window-size={width},{height}",
                    "--disable-blink-features=AutomationControlled"
                ]
                
                context = await p.chromium.launch_persistent_context(
                    str(dst),
                    executable_path=CHROME_EXEC,
                    headless=False,
                    ignore_https_errors=True,
                    ignore_default_args=["--use-mock-keychain", "--password-store=basic", "--enable-automation"],
                    args=chrome_args,
                    viewport=None,
                )
                
                # # Immediately return focus to the user so they can keep working
                # force_terminal_focus()
                
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(site["url"], wait_until="domcontentloaded", timeout=120000)
                await wait_for_ready(page, site)
                bots.append({
                    "name": site["name"],
                    "site": site,
                    "context": context,
                    "page": page,
                    "bounds": (left, top, right, bottom)
                })
                
                # Stagger the launches
                await asyncio.sleep(1.0)
                
            print("Background services ready.\n")
            
            # 2. Continuous prompting loop
            # Run indefinitely until user types 'QUIT'
            loop = asyncio.get_running_loop()
            while True:
                prompt_task = loop.run_in_executor(None, input, "Enter your prompt (or type 'QUIT' to exit):\n> ")
                prompt = await prompt_task
                prompt = prompt.strip()
                
                if prompt.upper() == "QUIT":
                    print("QUIT received. Closing all windows and exiting...")
                    break
                if not prompt:
                    continue
                    
                print("Working...")
                
                # Submit responses
                submit_tasks = []
                for bot in bots:
                    submit_tasks.append(submit_prompt(bot["page"], bot["site"], prompt))
                await asyncio.gather(*submit_tasks, return_exceptions=True)
                
                print("Submissions injected. Sliding windows onscreen!")
                # Move all windows
                await asyncio.gather(*[
                    slide_window_onscreen(bot["context"], bot["page"], bot["bounds"])
                    for bot in bots
                ])

                # Then bring all Chrome instances forward
                await asyncio.sleep(0.15)
                bring_all_chrome_windows_forward()
                    
                print("Ready for next prompt!\n")

    except KeyboardInterrupt:
        print("\nInterrupted by user — exiting and leaving Chrome windows open.")
    finally:
        pass

if __name__ == "__main__":
    asyncio.run(main())
