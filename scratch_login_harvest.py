"""Background: browser-login the burner, wait for the emailed code, then harvest.

Flow:
  1. submit credentials in the stealthed persona browser (KZ IP)
  2. if IG asks for the email code, BLOCK-poll secrets/ig_code.txt (operator drops
     the code there) — keeping the browser session alive
  3. enter code -> complete login -> save sessionid
  4. hand the sessionid to instagrapi and pull real posts WITH author_follower_count

Progress is streamed to secrets/harvest_log.txt so the run can be monitored.
Credentials read from secrets/ig_burner.env (never printed).
"""
import json
import os
import time

from core.harness import launch_persona, close_persona, human_pause

CODE_FILE = "secrets/ig_code.txt"
SESS_FILE = "secrets/ig_browser_session.json"
OUT_FILE = "secrets/harvest_result.json"
LOG_FILE = "secrets/harvest_log.txt"


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# fresh log
open(LOG_FILE, "w").close()

env = {}
with open("secrets/ig_burner.env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
USER, PW = env["IG_USERNAME"], env["IG_PASSWORD"]

ctx = launch_persona("ig-burner-main", headless=True)
page = ctx.new_page()

say("opening login page")
page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded", timeout=40000)
human_pause(2.5, 4.0)

for label in ["Allow all cookies", "Разрешить все cookie", "Only allow essential cookies",
              "Разрешить только необходимые файлы cookie"]:
    try:
        b = page.get_by_role("button", name=label)
        if b.count() > 0:
            b.first.click(); human_pause(1.0, 2.0); break
    except Exception:
        pass

say("typing credentials")
page.wait_for_selector('input[name="email"]', timeout=20000)
email = page.locator('input[name="email"]'); email.click(); email.press_sequentially(USER, delay=90)
human_pause(0.5, 1.2)
pw = page.locator('input[name="pass"]'); pw.click(); pw.press_sequentially(PW, delay=90)
human_pause(0.6, 1.4)
pw.press("Enter")
say("submitted; waiting for result")
human_pause(6.0, 8.0)
try:
    page.wait_for_load_state("networkidle", timeout=20000)
except Exception:
    pass
human_pause(2.0, 3.0)

# wait for the redirect to actually settle: either logged-in cookie, or a challenge URL
deadline = time.time() + 30
url = page.url
while time.time() < deadline:
    url = page.url
    if "sessionid" in {c["name"] for c in ctx.cookies()}:
        break
    if any(s in url for s in ("codeentry", "challenge", "checkpoint", "two_factor")):
        break
    time.sleep(2)
say(f"post-submit url settled: {url[:70]}")

if any(s in url for s in ("codeentry", "challenge", "checkpoint", "two_factor")):
    say("EMAIL CODE REQUIRED -> waiting for secrets/ig_code.txt (up to 10 min)")
    if os.path.exists(CODE_FILE):
        os.remove(CODE_FILE)
    code = None
    for _ in range(120):  # ~10 min @ 5s
        if os.path.exists(CODE_FILE):
            c = open(CODE_FILE).read().strip()
            if c:
                code = c; break
        time.sleep(5)
    if not code:
        say("TIMEOUT: no code provided"); close_persona(ctx); raise SystemExit(0)
    say(f"got code ({len(code)} chars); entering")
    inp = None
    for sel in ['input[autocomplete="one-time-code"]', 'input[name="verificationCode"]',
                'input[name="security_code"]', 'input[inputmode="numeric"]',
                'input[type="tel"]', 'input[type="text"]']:
        loc = page.locator(sel)
        if loc.count() > 0 and loc.first.is_visible():
            inp = loc.first; break
    if inp is None:
        say("could not find code input"); close_persona(ctx); raise SystemExit(0)
    inp.click(); inp.press_sequentially(code, delay=130)
    human_pause(0.8, 1.5)
    clicked = False
    for name in ["Continue", "Продолжить", "Confirm", "Подтвердить", "Next", "Далее"]:
        b = page.get_by_role("button", name=name)
        if b.count() > 0:
            b.first.click(); clicked = True; break
    if not clicked:
        inp.press("Enter")
    say("code submitted; waiting")
    human_pause(6.0, 9.0)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    human_pause(2.0, 3.0)

# dismiss post-login prompts
for name in ["Not Now", "Not now", "Не сейчас", "Dismiss", "Save info", "Сохранить данные"]:
    try:
        b = page.get_by_role("button", name=name)
        if b.count() > 0:
            b.first.click(); human_pause(1.0, 2.0)
    except Exception:
        pass

cookies = {c["name"]: c["value"] for c in ctx.cookies()}
logged_in = "sessionid" in cookies and bool(cookies.get("ds_user_id"))
say(f"LOGIN RESULT logged_in={logged_in} ds_user_id={cookies.get('ds_user_id')} url={page.url[:50]}")

if not logged_in:
    snippet = page.evaluate("() => (document.body?document.body.innerText:'').replace(/\\s+/g,' ').slice(0,200)")
    say(f"not logged in. page says: {snippet}")
    close_persona(ctx); raise SystemExit(0)

json.dump({"sessionid": cookies["sessionid"], "ds_user_id": cookies["ds_user_id"]}, open(SESS_FILE, "w"))
say("session saved -> " + SESS_FILE)
close_persona(ctx)

# ---- harvest with the minted session via instagrapi (full stats + followers) ----
say("harvesting via instagrapi using browser sessionid")
from instagrapi import Client
from adapters.instagram import InstagramAdapter, SoftBlockError
from core.schema import WatchedAccount
import dataclasses

cl = Client()
cl.set_locale("ru_RU"); cl.set_country("KZ"); cl.set_country_code(7); cl.set_timezone_offset(5 * 3600)
cl.delay_range = [4.0, 9.0]
cl.login_by_sessionid(cookies["sessionid"])
say(f"instagrapi authed as user_id={cl.user_id}")

a = InstagramAdapter(client=cl)
results = {}
for handle in ["natgeo"]:
    acct = WatchedAccount(handle=handle, platform="instagram", segment="adjacent", geo_tier="World")
    try:
        posts = a.fetch_account_posts(acct, limit=5)
        rows = []
        for p in posts:
            ai = p.raw.get("_account_info") or {}
            rows.append({
                "id": p.platform_post_id, "media_type": p.media_type,
                "likes": p.like_count, "comments": p.comment_count, "views": p.view_count,
                "author_follower_count": p.author_follower_count,
                "verified": ai.get("is_verified"), "following": ai.get("following_count"),
                "hashtags": p.hashtags, "raw_keys": len(p.raw),
            })
        results[handle] = rows
        say(f"{handle}: {len(rows)} posts, followers={rows[0]['author_follower_count'] if rows else None}")
    except SoftBlockError as e:
        results[handle] = {"error": str(e)[:160]}
        say(f"{handle}: SOFTBLOCK {str(e)[:120]}")

json.dump(results, open(OUT_FILE, "w"), ensure_ascii=False, indent=1)
say("DONE -> " + OUT_FILE)
