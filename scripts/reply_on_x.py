#!/usr/bin/env python3
"""
reply_on_x.py — Checks account age for limits, then replies to a single post on the Home timeline.

Rules enforced:
- Detects if account is <30 days old and sets strict reply limits.
- Generates a unique, non-promotional reply to the top tweet in the timeline.
- Exits after one reply to spread out activity (human pacing).
"""

import sys
import os
import time
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone

# Force unbuffered output
import builtins
def print(*args, **kwargs):
    kwargs.setdefault('flush', True)
    builtins.print(*args, **kwargs)

import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument("--dry-run", action="store_true")
args = parser.parse_args()
IS_DRY_RUN = args.dry_run

SESSION_FILE    = Path("/tmp/x_session.json")
STATS_FILE      = Path("replied-stats.json")
BROWSER_SESSION = str(Path(os.getcwd()) / ".browser-session")

# LLM Fallback (same as post_to_x.py)
GH_MODELS_URL  = os.getenv("GH_MODELS_BASE_URL", "https://models.inference.ai.azure.com")
GH_MODELS_KEY  = os.getenv("GH_MODELS_TOKEN")
GH_MODEL       = os.getenv("GH_MODEL", "gpt-4o")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "llama3.2:3b")

API_KEY      = os.environ.get("API_KEY", "password")
API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8045/v1")
AG_MODEL     = "gemini-3-flash"

def get_client():
    if GH_MODELS_KEY:
        print("✅ LLM source: GitHub Models")
        return GH_MODELS_URL, GH_MODELS_KEY, GH_MODEL
    try:
        req = urllib.request.Request(f"{API_BASE_URL}/models")
        req.add_header("Authorization", f"Bearer {API_KEY}")
        urllib.request.urlopen(req, timeout=3)
        print("✅ LLM source: Antigravity Manager")
        return API_BASE_URL, API_KEY, AG_MODEL
    except Exception:
        try:
            urllib.request.urlopen(f"{OLLAMA_BASE_URL}/models", timeout=3)
            print("✅ LLM source: Ollama (local)")
            return OLLAMA_BASE_URL, "ollama", OLLAMA_MODEL
        except Exception as e:
            raise Exception("No LLM source available")

def get_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.common.exceptions import WebDriverException

    try:
        opts = Options()
        opts.add_experimental_option("debuggerAddress", "127.0.0.1:9222")
        driver = webdriver.Chrome(options=opts)
        print("Connected to existing Chrome on port 9222.")
        return driver
    except WebDriverException:
        pass

    opts = Options()
    opts.binary_location = "/usr/bin/google-chrome"
    for arg in [
        "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
        "--disable-session-crashed-bubble", "--no-first-run",
        "--no-default-browser-check", "--disable-infobars",
        "--window-size=2000,1550", "--window-position=0,0",
        f"--user-data-dir={BROWSER_SESSION}",
        "--remote-debugging-port=9222",
    ]:
        opts.add_argument(arg)
    if not os.environ.get("DISPLAY"):
        opts.add_argument("--headless=new")
    driver = webdriver.Chrome(options=opts)
    print("Launched new Chrome instance.")
    return driver

def set_cookies(driver, session: dict):
    driver.get("https://x.com/")
    driver.add_cookie({"name": "auth_token", "value": session["auth_token"], "domain": ".x.com"})
    driver.add_cookie({"name": "ct0",        "value": session["ct0"],        "domain": ".x.com"})

def check_account_age_and_limit(driver, username):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    
    # Default personal limit if scrape fails
    daily_limit = 15
    
    try:
        profile_url = f"https://x.com/{username}"
        print(f"🔍 Checking profile to determine account age: {profile_url}")
        driver.get(profile_url)
        wait = WebDriverWait(driver, 15)
        
        # Note: the testid might change, but typically it contains "Joined" text.
        # We will scrape the entire profile info block.
        user_info = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="UserProfileHeader_Items"]')))
        text = user_info.text
        print(f"Profile Header Text Found:\n{text}")
        
        # Example text: "Joined July 2024"
        import re
        match = re.search(r"Joined (\w+) (\d{4})", text)
        if match:
            month_str = match.group(1)
            year_str = match.group(2)
            joined_date = datetime.strptime(f"{month_str} {year_str}", "%B %Y")
            now = datetime.now()
            age_days = (now - joined_date).days
            print(f"📊 Account Age: Approx {age_days} days (Joined {month_str} {year_str})")
            
            if age_days < 30:
                print("⚠️ NEW ACCOUNT (<30 days) DETECTED! Limiting to 8 replies per day.")
                daily_limit = 8
            else:
                print("✅ Mature account detected. Setting limit to 20 replies per day.")
                daily_limit = 20
        else:
            print("⚠️ Could not parse 'Joined' date from profile header. Using default limit of 15.")
    except Exception as e:
        print(f"⚠️ Failed to determine account age ({e}). Using default limit of 15.")
        
    return daily_limit

def can_reply_today(daily_limit):
    today_str = datetime.now().strftime("%Y-%m-%d")
    stats = {}
    if STATS_FILE.exists():
        try:
            stats = json.loads(STATS_FILE.read_text())
        except:
            pass
            
    if stats.get("date") != today_str:
        stats = {"date": today_str, "count": 0}
        
    if stats["count"] >= daily_limit:
        print(f"🛑 Reached safe daily reply limit ({stats['count']}/{daily_limit}). Skipping.")
        return False, stats
        
    print(f"✅ Daily limit check passed. Replied {stats['count']}/{daily_limit} today.")
    return True, stats

def generate_reply(tweet_text: str) -> str:
    base_url, api_key, model_name = get_client()
    url = f"{base_url}/chat/completions"
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    prompt = f"""Task: Write a natural, human-like reply to this social media post.
    
Rule 1: MUST NOT sound like a typical bot. Be conversational.
Rule 2: MUST NOT include promotional links or ask people to follow/subscribe.
Rule 3: Keep it short (under 150 characters).
Rule 4: Vary your style (sometimes ask a question, sometimes agree, sometimes share a brief related thought).
Rule 5: Return ONLY the reply text, no quotes or additional formatting.

The post I am replying to says:
"{tweet_text}"
"""

    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": "You are a casual X user reading your timeline."},
            {"role": "user",   "content": prompt}
        ],
        "temperature": 0.8
    }

    print(f"🤖 Generating reply via {model_name}...")
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method='POST')
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read().decode())
        reply_text = data['choices'][0]['message']['content'].strip().strip('"\'')
        print(f"✅ Generated Reply: {reply_text}")
        return reply_text
    except Exception as e:
        print(f"❌ Failed to generate reply: {e}")
        return ""

def execute_reply(driver):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    
    driver.get("https://x.com/home")
    wait = WebDriverWait(driver, 30)
    
    try:
        print("⏳ Waiting for Home timeline to load...")
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="tweet"]')))
    except Exception as e:
        print("❌ Cannot find any tweets on the timeline.")
        return False
        
    tweets = driver.find_elements(By.CSS_SELECTOR, '[data-testid="tweet"]')
    if not tweets:
        return False
        
    # Pick the first suitable tweet (non-ad)
    target_tweet = None
    tweet_text = ""
    for t in tweets[:5]: # Search first 5
        text_content = t.text
        if "Ad" in text_content or "Promoted" in text_content:
            continue
        try:
            # Get the actual tweet text element
            text_el = t.find_element(By.CSS_SELECTOR, '[data-testid="tweetText"]')
            tweet_text = text_el.text
            if tweet_text and len(tweet_text) > 10:
                target_tweet = t
                break
        except:
            continue
            
    if not target_tweet:
        print("❌ Could not find a suitable non-promoted tweet with text.")
        return False
        
    print(f"\n[Found Target Tweet]\n{tweet_text}\n")
    
    reply_text = generate_reply(tweet_text)
    if not reply_text:
        return False
        
    if IS_DRY_RUN:
        print("\n[DRY RUN] Would have replied with:")
        print(f"-> {reply_text}")
        return True
        
    # Attempt to click reply
    try:
        reply_btn = target_tweet.find_element(By.CSS_SELECTOR, '[data-testid="reply"]')
        reply_btn.click()
        time.sleep(2)
        
        textarea = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="tweetTextarea_0"]')))
        
        # Paste reply text
        driver.execute_script("""
            const text = arguments[0];
            const dataTransfer = new DataTransfer();
            dataTransfer.setData('text/plain', text);
            const event = new ClipboardEvent('paste', {
                clipboardData: dataTransfer,
                bubbles: true
            });
            arguments[1].dispatchEvent(event);
        """, reply_text, textarea)
        time.sleep(1)
        
        post_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, '[data-testid="tweetButton"]')))
        post_btn.click()
        print("🚀 Clicked Reply Submit Button.")
        
        time.sleep(5)
        return True
    except Exception as e:
        print(f"❌ Automation failed during reply submission: {e}")
        return False

def main():
    print(f"\n{'='*60}")
    print(f"X Auto-Replier {'(DRY RUN) ' if IS_DRY_RUN else ''} (Safe Mode)")
    print(f"{'='*60}\n")

    if not SESSION_FILE.exists():
        print(f"❌ No session file at {SESSION_FILE}. Exiting.")
        sys.exit(1)
        
    session = json.loads(SESSION_FILE.read_text())
    username = session.get('username')
    if not username:
        print("⚠️ Session file missing 'username'. Will guess age limits or defaults.")
    else:
        print(f"Session loaded for @{username}")

    driver = get_driver()
    if not IS_DRY_RUN:
        set_cookies(driver, session)
        
    if username and not IS_DRY_RUN:
        daily_limit = check_account_age_and_limit(driver, username)
    else:
        daily_limit = 15
        
    can_run, stats = can_reply_today(daily_limit)
    if not can_run:
        if not IS_DRY_RUN:
            sys.exit(0)
            
    print("\nStarting reply sequence...")
    success = execute_reply(driver)
    
    if success and not IS_DRY_RUN:
        stats["count"] += 1
        STATS_FILE.write_text(json.dumps(stats, indent=2))
        print(f"✅ Saved stats. Replied {stats['count']}/{daily_limit} today.")
        print("✅ Finished processing 1 reply. Exiting to maintain human pacing.")
        
if __name__ == "__main__":
    main()
