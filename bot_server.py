"""Advertise Pro bot server: Telegram Stars payments + the API the Mini App talks to.
People pay to promote a Telegram channel or bot. You review each one; approved promotions are
posted in your promo channel and removed when their time is up.

Settings (environment variables)
  BOT_TOKEN       required  from BotFather
  ADMIN_ID        required  your own Telegram user id (you approve promotions)
  WEBAPP_ORIGIN   optional  where webapp/index.html is hosted, e.g. https://adprotg.netlify.app
                            (the page can't call the API until it's set)
  DB_PATH         optional  SQLite file, default adpro.db (use /data/adpro.db on a volume)
  PROMO_CHANNEL   optional  your promo channel, e.g. @adpro_promos. The bot must be an admin there
                            with "Post messages" and "Delete messages". Without it, approving only
                            marks a promotion live.
  FREE_MODE       optional  1 = listings are free (no payment, still reviewed). For launch.
  TEST_PRICES     optional  1 = every plan costs 1 Star. For testing payments and refunds.

Run
  pip install -r requirements.txt
  uvicorn bot_server:app --host 0.0.0.0 --port 8000

Flow
  1. The page POSTs the promotion to /api/campaigns. Paid: saved as "awaiting_payment" and a Stars
     invoice link is returned. Free mode: goes straight to "in_review".
  2. Paid: the page opens the invoice; Telegram sends pre_checkout_query (we approve) and then
     successful_payment, and the promotion becomes "in_review".
  3. You get a message with Approve / Reject. Approve posts it in PROMO_CHANNEL and makes it "live".
     Reject refunds the Stars (if it was paid).
  4. When the plan's days are up, the post is removed and the promotion becomes "ended".

Note: this server does not check that the buyer owns the channel or bot. Look at each request
before you approve it, and never approve a username the buyer doesn't run.
"""
import asyncio
import contextlib
import hashlib
import hmac
import html
import json
import os
import re
import sqlite3
import time
from typing import Literal
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# Strip spaces, quotes and newlines that sneak in when pasting into a host's settings page.
BOT_TOKEN = os.environ["BOT_TOKEN"].strip().strip("'\"").strip()
ADMIN_ID = int(os.environ["ADMIN_ID"])
# Optional until the page is hosted (deploy step 7). Until it is set, browsers can't call the API.
ORIGIN = os.environ.get("WEBAPP_ORIGIN", "").strip().rstrip("/")
PROMO_CHANNEL = os.environ.get("PROMO_CHANNEL", "").strip().replace("https://t.me/", "").lstrip("@")
FREE_MODE = env_flag("FREE_MODE")
TEST_PRICES = env_flag("TEST_PRICES")
API = f"https://api.telegram.org/bot{BOT_TOKEN}/"

# Prices are in Telegram Stars. The page reads them from /api/config, so this is the only place to change them.
PLANS = {
    "starter": {"days": 3, "stars": 100},
    "standard": {"days": 7, "stars": 200},
    "extended": {"days": 14, "stars": 350},
}
if TEST_PRICES:
    PLANS = {k: {**v, "stars": 1} for k, v in PLANS.items()}
CATEGORIES = ["Crypto", "Tech", "News", "Business", "Education", "Entertainment", "Games", "Other"]
USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
MAX_PENDING_FREE = 3  # free mode: how many promotions one person can have waiting for review

db = sqlite3.connect(os.environ.get("DB_PATH", "adpro.db"), check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute(
    """CREATE TABLE IF NOT EXISTS campaigns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        handle TEXT NOT NULL,
        text TEXT NOT NULL,
        category TEXT NOT NULL,
        lang TEXT NOT NULL,
        plan TEXT NOT NULL,
        status TEXT NOT NULL,
        charge_id TEXT,
        created INTEGER NOT NULL
    )"""
)
# Columns added after the first release. Existing databases get them here.
_have = {r["name"] for r in db.execute("PRAGMA table_info(campaigns)")}
for col, typ in (("title", "TEXT"), ("post_id", "INTEGER"), ("live_until", "INTEGER"), ("refunded", "INTEGER"),
                 ("price", "INTEGER"), ("code", "TEXT")):
    if col not in _have:
        db.execute(f"ALTER TABLE campaigns ADD COLUMN {col} {typ}")
# Promo codes and settings (the current sale) that you manage from your bot chat.
db.execute("""CREATE TABLE IF NOT EXISTS codes (
    code TEXT PRIMARY KEY, percent INTEGER NOT NULL, max_uses INTEGER, uses INTEGER NOT NULL DEFAULT 0,
    expires INTEGER, created INTEGER NOT NULL)""")
db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
db.commit()

http = httpx.AsyncClient(timeout=40)
BOT_USERNAME = ""  # filled in at startup from getMe
STATUS = {"polling": False, "last_error": None, "last_error_at": None}  # shown at GET /


def note_error(where: str, e: Exception):
    msg = f"{where}: {e}".replace(BOT_TOKEN, "<token>")[:300]  # never show the token
    STATUS["last_error"], STATUS["last_error_at"] = msg, time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(msg)


async def tg(method: str, **params):
    r = await http.post(API + method, json=params)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method} failed: {data.get('description')}")
    return data["result"]


def get_campaign(cid: int):
    return db.execute("SELECT * FROM campaigns WHERE id = ?", (cid,)).fetchone()


# ---------- Discounts: promo codes and sales ----------

CODE_RE = re.compile(r"^[A-Z0-9_-]{3,20}$")


def setting(key: str, default: str | None = None) -> str | None:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key: str, value) -> None:
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (key, None if value is None else str(value)))
    db.commit()


def current_sale() -> tuple[int, int | None]:
    """(percent off everything, ends at) — (0, None) when there's no sale."""
    pct = int(setting("sale_percent") or 0)
    until = setting("sale_until")
    until = int(until) if until else None
    if pct and until and until <= time.time():
        set_setting("sale_percent", 0)
        set_setting("sale_until", None)
        return 0, None
    return pct, until


def find_code(code: str):
    """The promo code's row if it exists and can still be used, else None."""
    row = db.execute("SELECT * FROM codes WHERE code = ?", (code.strip().upper(),)).fetchone()
    if not row:
        return None
    if row["expires"] and row["expires"] <= time.time():
        return None
    if row["max_uses"] and row["uses"] >= row["max_uses"]:
        return None
    return row


def discounted(stars: int, pct: int) -> int:
    """Price after pct% off. 100% off is free (0); anything else costs at least 1 Star."""
    if pct >= 100:
        return 0
    return max(1, int(stars * (100 - pct) / 100 + 0.5))


def quote(plan_id: str, code_row=None) -> tuple[int, int, str | None]:
    """(price, percent off, code that gave it). Sale and code don't stack: the bigger one wins."""
    sale, _ = current_sale()
    code_pct = code_row["percent"] if code_row else 0
    if code_pct > sale:
        return discounted(PLANS[plan_id]["stars"], code_pct), code_pct, code_row["code"]
    return discounted(PLANS[plan_id]["stars"], sale), sale, None


def price_of(c) -> int:
    """What this promotion costs (older ones, from before discounts, have no stored price)."""
    return c["price"] if c["price"] is not None else PLANS[c["plan"]]["stars"]


def use_code(code: str | None) -> None:
    if code:
        db.execute("UPDATE codes SET uses = uses + 1 WHERE code = ?", (code,))
        db.commit()


def verify_init_data(init_data: str) -> int:
    """Check the data Telegram gives the Mini App is genuine. Returns the user's id."""
    denied = HTTPException(401, "Open this page from Telegram and try again.")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    got = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not got:
        print("auth rejected: no hash in initData")
        raise denied
    if not hmac.compare_digest(calc, got):
        # Almost always means BOT_TOKEN isn't the token of the bot the Mini App was opened from.
        print(f"auth rejected: signature mismatch (server token is for @{BOT_USERNAME or '?'})")
        raise denied
    try:
        fresh = time.time() - int(pairs.get("auth_date", "0")) < 86400
        uid = int(json.loads(pairs["user"])["id"])
    except (ValueError, KeyError, TypeError):
        print("auth rejected: missing auth_date or user")
        raise denied
    if not fresh:
        print("auth rejected: initData older than 24 hours")
        raise denied
    return uid


def user_id_from(authorization: str | None) -> int:
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(401, "Open this page from Telegram and try again.")
    return verify_init_data(authorization[4:])


# ---------- Channel look-ups (cached so typing in the page doesn't hammer Telegram) ----------

_lookups: dict[str, tuple[float, dict | None]] = {}


async def lookup_chat(handle: str) -> dict | None:
    """{"type", "title", "members"} for a public chat, or None if it doesn't exist.
    Raises RuntimeError if Telegram couldn't be asked (so callers can let it through)."""
    key = handle.lower()
    hit = _lookups.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        chat = await tg("getChat", chat_id="@" + handle)
    except RuntimeError as e:
        if "not found" in str(e).lower():
            _lookups[key] = (time.time(), None)
            return None
        raise
    members = None
    with contextlib.suppress(Exception):
        members = await tg("getChatMemberCount", chat_id="@" + handle)
    info = {"type": chat.get("type"), "title": chat.get("title") or chat.get("first_name") or "", "members": members}
    _lookups[key] = (time.time(), info)
    return info


async def check_channel(handle: str) -> dict | None:
    """Raise a 400 the page can show if this isn't a public channel. Returns its info when known."""
    try:
        info = await lookup_chat(handle)
    except Exception:
        return None  # Telegram didn't answer; don't block the buyer, you'll check it in review.
    if info is None:
        raise HTTPException(400, f"No public channel @{handle} found. Check the username.")
    if info["type"] != "channel":
        kind = {"supergroup": "a group", "group": "a group", "private": "a user or bot"}.get(info["type"], "not a channel")
        raise HTTPException(400, f"@{handle} is {kind}, not a channel.")
    return info


# ---------- Payments ----------

async def make_invoice(cid: int, plan_id: str, handle: str, price: int) -> str:
    plan = PLANS[plan_id]
    return await tg(
        "createInvoiceLink",
        title=f"Promote @{handle}"[:32],
        description=f"Promote @{handle} to Telegram users for {plan['days']} days.",
        payload=str(cid),
        currency="XTR",  # Telegram Stars. No provider token is needed.
        prices=[{"label": f"{plan['days']} days", "amount": price}],
    )


class CampaignIn(BaseModel):
    plan: str
    kind: Literal["channel", "bot"]
    handle: str
    text: str = Field(min_length=1, max_length=200)
    category: str = "Other"
    lang: str = Field(default="Any", max_length=20)
    code: str | None = Field(default=None, max_length=30)


@contextlib.asynccontextmanager
async def lifespan(_):
    tasks = [asyncio.create_task(poll()), asyncio.create_task(expire_loop())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ORIGIN] if ORIGIN else [],
    allow_methods=["GET", "POST"],
    allow_headers=["authorization", "content-type"],
)


@app.get("/")
async def health():
    """Open the server's address in a browser to check it's running."""
    return {
        "ok": True,
        "bot": "@" + BOT_USERNAME if BOT_USERNAME else "not connected yet",
        "receiving_messages": STATUS["polling"],
        "last_error": STATUS["last_error"],
        "last_error_at": STATUS["last_error_at"],
        "webapp_origin": ORIGIN or None,
        "promo_channel": "@" + PROMO_CHANNEL if PROMO_CHANNEL else None,
        "free_mode": FREE_MODE,
        "test_prices": TEST_PRICES,
    }


@app.get("/api/config")
async def config():
    """What the page shows: plans, prices, free mode and the promo channel."""
    members = None
    if PROMO_CHANNEL:
        with contextlib.suppress(Exception):
            info = await lookup_chat(PROMO_CHANNEL)
            members = info and info["members"]
    sale, sale_until = current_sale()
    return {
        "plans": [{"id": k, **v, "price": discounted(v["stars"], sale)} for k, v in PLANS.items()],
        "sale": sale,
        "sale_until": sale_until,
        "free": FREE_MODE,
        "channel": "@" + PROMO_CHANNEL if PROMO_CHANNEL else None,
        "channel_members": members,
    }


@app.get("/api/lookup")
async def lookup(handle: str, authorization: str | None = Header(default=None)):
    """The page calls this while the buyer types a channel username."""
    user_id_from(authorization)
    handle = handle.strip().lstrip("@")
    if not USERNAME.match(handle):
        raise HTTPException(400, "Enter a valid username: 5 to 32 letters, numbers or underscores.")
    info = await check_channel(handle)
    if info is None:
        raise HTTPException(503, "Couldn't check this channel right now.")
    return {"title": info["title"], "members": info["members"]}


@app.get("/api/code")
async def check_code(code: str, authorization: str | None = Header(default=None)):
    """The page calls this when the buyer taps Apply on a promo code."""
    user_id_from(authorization)
    row = find_code(code)
    if not row:
        raise HTTPException(404, "That code isn't valid or has expired.")
    sale, _ = current_sale()
    if row["percent"] <= sale:
        raise HTTPException(400, f"There's already a {sale}% sale on, which beats this code.")
    return {"code": row["code"], "percent": row["percent"],
            "prices": {k: discounted(v["stars"], row["percent"]) for k, v in PLANS.items()}}


@app.post("/api/campaigns")
async def create_campaign(body: CampaignIn, authorization: str | None = Header(default=None)):
    uid = user_id_from(authorization)
    handle = body.handle.strip().lstrip("@")
    if body.plan not in PLANS:
        raise HTTPException(400, "Unknown plan.")
    if not USERNAME.match(handle):
        raise HTTPException(400, "Enter a valid username: 5 to 32 letters, numbers or underscores.")
    if body.kind == "bot" and not handle.lower().endswith("bot"):
        raise HTTPException(400, "A bot username must end in \"bot\".")
    title = None
    if body.kind == "channel":
        info = await check_channel(handle)
        title = info and info["title"]
    if FREE_MODE and uid != ADMIN_ID:  # the owner can add as many listings as they like
        waiting = db.execute(
            "SELECT COUNT(*) FROM campaigns WHERE user_id = ? AND status = 'in_review'", (uid,)
        ).fetchone()[0]
        if waiting >= MAX_PENDING_FREE:
            raise HTTPException(429, "You already have promotions waiting for review. Try again once they're reviewed.")
    price, code = 0, None
    if not FREE_MODE:
        code_row = None
        if body.code and body.code.strip():
            code_row = find_code(body.code)
            if not code_row:
                raise HTTPException(400, "That code isn't valid or has expired.")
        price, _, code = quote(body.plan, code_row)
    free = FREE_MODE or price == 0
    category = body.category if body.category in CATEGORIES else "Other"
    cur = db.execute(
        "INSERT INTO campaigns (user_id, kind, handle, title, text, category, lang, plan, status, price, code, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, body.kind, handle, title, body.text, category, body.lang, body.plan,
         "in_review" if free else "awaiting_payment", price, code, int(time.time())),
    )
    db.commit()
    cid = cur.lastrowid
    if free:
        use_code(code)
        await send_for_review(get_campaign(cid))
        return {"id": cid, "invoice_link": None, "free": True}
    return {"id": cid, "invoice_link": await make_invoice(cid, body.plan, handle, price), "free": False}


@app.post("/api/campaigns/{cid}/invoice")
async def new_invoice(cid: int, authorization: str | None = Header(default=None)):
    uid = user_id_from(authorization)
    c = get_campaign(cid)
    if not c or c["user_id"] != uid or c["status"] != "awaiting_payment":
        raise HTTPException(404, "This campaign can't be paid.")
    return {"id": cid, "invoice_link": await make_invoice(cid, c["plan"], c["handle"], price_of(c))}


@app.post("/api/campaigns/{cid}/cancel")
async def cancel_campaign(cid: int, authorization: str | None = Header(default=None)):
    """The buyer withdraws a promotion: unpaid ones are dropped, ones in review are refunded,
    live ones are taken down (no refund: they've already run)."""
    uid = user_id_from(authorization)
    c = get_campaign(cid)
    if not c or c["user_id"] != uid:
        raise HTTPException(404, "Promotion not found.")
    status = c["status"]
    if status not in ("awaiting_payment", "in_review", "live"):
        raise HTTPException(400, "This promotion has already finished.")
    # Claim it first so an Approve or Reject pressed at the same moment can't also act on it.
    cur = db.execute("UPDATE campaigns SET status = 'cancelled' WHERE id = ? AND status = ?", (cid, status))
    db.commit()
    if cur.rowcount == 0:
        raise HTTPException(409, "This promotion just changed. Pull to refresh and try again.")
    what = ""
    if status == "in_review" and c["charge_id"]:
        try:
            await tg("refundStarPayment", user_id=uid, telegram_payment_charge_id=c["charge_id"])
        except Exception as e:
            db.execute("UPDATE campaigns SET status = 'in_review' WHERE id = ?", (cid,))
            db.commit()
            note_error(f"refunding cancelled promotion #{cid}", e)
            raise HTTPException(502, "Couldn't refund your Stars right now, so nothing was cancelled. Try again in a minute.")
        db.execute("UPDATE campaigns SET refunded = 1 WHERE id = ?", (cid,))
        db.commit()
        what = f" {price_of(c)} Stars refunded."
    elif status == "live":
        await remove_post(c["post_id"], "⏹ This promotion was stopped.")
        what = " Post removed from the channel."
    if status != "awaiting_payment":
        with contextlib.suppress(Exception):
            await tg("sendMessage", chat_id=ADMIN_ID,
                     text=f"Promotion #{cid} (@{c['handle']}) was cancelled by the buyer.{what}")
    return {"id": cid, "status": "cancelled", "refunded": bool(what.strip().endswith("refunded."))}


@app.get("/api/campaigns")
async def list_campaigns(authorization: str | None = Header(default=None)):
    uid = user_id_from(authorization)
    rows = db.execute("SELECT * FROM campaigns WHERE user_id = ? ORDER BY id DESC LIMIT 50", (uid,)).fetchall()
    out = []
    for r in rows:
        plan = PLANS.get(r["plan"], {"days": 0, "stars": 0})
        out.append({
            "id": r["id"],
            "kind": r["kind"],
            "name": "@" + r["handle"],
            "title": r["title"],
            "status": r["status"],
            "days": plan["days"],
            "stars": price_of(r) if r["charge_id"] or r["status"] == "awaiting_payment" else 0,
            "full_price": plan["stars"],
            "code": r["code"],
            "until": r["live_until"],
            "refunded": bool(r["refunded"]),
            "post_url": f"https://t.me/{PROMO_CHANNEL}/{r['post_id']}" if PROMO_CHANNEL and r["post_id"] and r["status"] == "live" else None,
        })
    return out


# ---------- Review, posting and expiry ----------

async def send_for_review(c):
    if c["kind"] == "channel":
        try:
            info = await lookup_chat(c["handle"])
            looked = f"Looked up: {info['type']} \"{info['title']}\", {info['members']} members" if info else "Channel not found!"
        except Exception:
            looked = "Couldn't look this channel up. Check it yourself."
    else:
        looked = "Bot: not looked up."
    if c["charge_id"]:
        paid = f"Paid {price_of(c)} Stars" + (f" (code {c['code']})" if c["code"] else "")
    else:
        paid = f"Free with code {c['code']}" if c["code"] else "Free listing"
    await tg(
        "sendMessage",
        chat_id=ADMIN_ID,
        text=(
            f"New {c['kind']} promotion #{c['id']}: @{c['handle']}\n"
            f"{PLANS[c['plan']]['days']} days · {paid} · {c['category']} · {c['lang']}\n"
            f"{looked}\n\n{c['text']}\n\nhttps://t.me/{c['handle']}"
        ),
        link_preview_options={"is_disabled": True},
        reply_markup={"inline_keyboard": [[
            {"text": "Approve", "callback_data": f"ok:{c['id']}"},
            {"text": "Reject and refund" if c["charge_id"] else "Reject", "callback_data": f"no:{c['id']}"},
        ]]},
    )


CATEGORY_EMOJI = {
    "Crypto": "🪙", "Tech": "💻", "News": "📰", "Business": "💼",
    "Education": "🎓", "Entertainment": "🎬", "Games": "🎮", "Other": "✨",
}


def short_count(n: int) -> str:
    """1234 -> 1.2K, 56789 -> 56.8K, 1200000 -> 1.2M"""
    for size, suffix in ((1_000_000, "M"), (1_000, "K")):
        if n >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + suffix
    return str(n)


def hashtag(word: str) -> str:
    return "#" + re.sub(r"[^0-9a-z_]", "", word.lower())


def post_text(c, members: int | None = None) -> str:
    """The promotion as it appears in the promo channel (Telegram HTML)."""
    is_bot = c["kind"] == "bot"
    name = html.escape(c["title"] or "@" + c["handle"])
    handle = f'<a href="https://t.me/{c["handle"]}">@{c["handle"]}</a>'
    emoji = CATEGORY_EMOJI.get(c["category"], "✨")
    facts = [f"{emoji} {html.escape(c['category'])}", "🤖 Bot" if is_bot else "📣 Channel"]
    if members:
        facts.append(f"👥 {short_count(members)} subscribers")
    tags = [hashtag(c["category"])] if c["category"] != "Other" else []
    if c["lang"] and c["lang"] != "Any":
        tags.append(hashtag(c["lang"]))
    tags.append("#bots" if is_bot else "#channels")
    label = "Sponsored" if c["charge_id"] else "Featured"
    footer = f"<i>{label}</i>"
    if BOT_USERNAME:
        footer += f' · <a href="https://t.me/{BOT_USERNAME}?start=promo">Get your {"bot" if is_bot else "channel"} featured</a>'
    title_line = f"<b>{name}</b>" if c["title"] else f"<b>{handle}</b>"
    lines = [
        title_line,
        " · ".join(facts),
        "",
        f"<blockquote>{html.escape(c['text'])}</blockquote>",
    ]
    if c["title"]:
        lines.append(f"👉 {handle}")
    lines += ["", " ".join(tags), footer]
    return "\n".join(lines)


async def publish(c) -> int | None:
    """Post an approved promotion in the promo channel. Returns the message id (None if no channel is set)."""
    if not PROMO_CHANNEL:
        return None
    members = None
    if c["kind"] == "channel":
        with contextlib.suppress(Exception):
            info = await lookup_chat(c["handle"])
            members = info and info["members"]
    buttons = [[{
        "text": "🤖 Start bot" if c["kind"] == "bot" else "➕ Join channel",
        "url": f"https://t.me/{c['handle']}",
    }]]
    if BOT_USERNAME:
        buttons.append([{"text": "📣 Promote yours", "url": f"https://t.me/{BOT_USERNAME}?start=promo"}])
    msg = await tg(
        "sendMessage",
        chat_id="@" + PROMO_CHANNEL,
        text=post_text(c, members),
        parse_mode="HTML",
        # Telegram's own preview card of the promoted channel/bot: its photo, name and bio.
        link_preview_options={"url": f"https://t.me/{c['handle']}", "prefer_small_media": True},
        reply_markup={"inline_keyboard": buttons},
    )
    return msg["message_id"]


def fmt_date(ts: int) -> str:
    return time.strftime("%-d %b", time.gmtime(ts))


async def remove_post(post_id: int | None, ended_text: str = "⏱ This promotion has ended."):
    """Take a promotion's post out of the promo channel."""
    if not (PROMO_CHANNEL and post_id):
        return
    try:
        await tg("deleteMessage", chat_id="@" + PROMO_CHANNEL, message_id=post_id)
    except Exception:
        # Older posts can't always be deleted; replace the text instead.
        with contextlib.suppress(Exception):
            await tg("editMessageText", chat_id="@" + PROMO_CHANNEL, message_id=post_id, text=ended_text)


async def expire_due():
    """End promotions whose time is up and remove their posts."""
    rows = db.execute(
        "SELECT * FROM campaigns WHERE status = 'live' AND live_until IS NOT NULL AND live_until <= ?",
        (int(time.time()),),
    ).fetchall()
    for c in rows:
        await remove_post(c["post_id"])
        db.execute("UPDATE campaigns SET status = 'ended' WHERE id = ?", (c["id"],))
        db.commit()
        with contextlib.suppress(Exception):
            await tg("sendMessage", chat_id=c["user_id"],
                     text=f"Your promotion of @{c['handle']} has ended. Thanks for using Advertise Pro!")


async def expire_loop():
    while True:
        try:
            await expire_due()
        except Exception as e:
            note_error("ending promotions", e)
        await asyncio.sleep(300)


ADMIN_HELP = """Your discount commands:

/newcode CODE PERCENT [USES] [DAYS]
  e.g. /newcode LAUNCH50 50 100 7
  50% off, first 100 uses, for 7 days. Leave out USES or DAYS (or use 0) for no limit.
  100% makes the promotion free.
/codes  list your codes
/delcode CODE  delete a code
/sale PERCENT [DAYS]  e.g. /sale 20 3 = 20% off everything for 3 days
/sale 0  end the sale

A sale and a code don't stack: buyers get whichever is bigger."""


def admin_command(text: str) -> str | None:
    """Handle /newcode, /codes, /delcode, /sale and /admin. Returns the reply, or None if it isn't one."""
    parts = text.split()
    if not parts:
        return None
    cmd, args = parts[0].lower().split("@")[0], parts[1:]

    def nums(vals, names):
        out = []
        for v, name in zip(vals, names):
            if not v.isdigit():
                raise ValueError(f"{name} must be a whole number, not \"{v}\".")
            out.append(int(v))
        return out

    try:
        if cmd == "/admin":
            return ADMIN_HELP
        if cmd == "/newcode":
            if len(args) < 2:
                return "Usage: /newcode CODE PERCENT [USES] [DAYS]\ne.g. /newcode LAUNCH50 50 100 7"
            code = args[0].upper()
            if not CODE_RE.match(code):
                return "Codes are 3 to 20 letters, numbers, - or _."
            pct, uses, days = (nums(args[1:4], ["PERCENT", "USES", "DAYS"]) + [0, 0])[:3]
            if not 1 <= pct <= 100:
                return "PERCENT must be between 1 and 100."
            expires = int(time.time()) + days * 86400 if days else None
            existed = db.execute("SELECT 1 FROM codes WHERE code = ?", (code,)).fetchone()
            db.execute("INSERT OR REPLACE INTO codes (code, percent, max_uses, uses, expires, created) VALUES (?, ?, ?, 0, ?, ?)",
                       (code, pct, uses or None, expires, int(time.time())))
            db.commit()
            what = "free" if pct == 100 else f"{pct}% off"
            limits = [f"{uses} uses" if uses else "unlimited uses", f"until {fmt_date(expires)}" if expires else "no expiry"]
            return f"{'Updated' if existed else 'Created'} code {code}: {what}, {', '.join(limits)}."
        if cmd == "/codes":
            rows = db.execute("SELECT * FROM codes ORDER BY created DESC").fetchall()
            if not rows:
                return "No codes yet. Make one with /newcode CODE PERCENT"
            lines = []
            for r in rows:
                used = f"{r['uses']}/{r['max_uses']} used" if r["max_uses"] else f"{r['uses']} used"
                state = ""
                if r["expires"] and r["expires"] <= time.time():
                    state = " · expired"
                elif r["max_uses"] and r["uses"] >= r["max_uses"]:
                    state = " · used up"
                elif r["expires"]:
                    state = f" · until {fmt_date(r['expires'])}"
                lines.append(f"{r['code']} · {r['percent']}% · {used}{state}")
            sale, until = current_sale()
            if sale:
                lines.append(f"\nSale: {sale}% off everything" + (f" until {fmt_date(until)}" if until else ""))
            return "\n".join(lines)
        if cmd == "/delcode":
            if not args:
                return "Usage: /delcode CODE"
            n = db.execute("DELETE FROM codes WHERE code = ?", (args[0].upper(),)).rowcount
            db.commit()
            return f"Deleted {args[0].upper()}." if n else f"There's no code {args[0].upper()}."
        if cmd == "/sale":
            if not args:
                sale, until = current_sale()
                now = f"Now: {sale}% off" + (f" until {fmt_date(until)}" if until else "") if sale else "No sale on."
                return f"{now}\nUsage: /sale PERCENT [DAYS], or /sale 0 to end it."
            pct, days = (nums(args[:2], ["PERCENT", "DAYS"]) + [0])[:2]
            if not 0 <= pct <= 99:
                return "PERCENT must be between 0 and 99. (For free listings, use FREE_MODE or a 100% code.)"
            until = int(time.time()) + days * 86400 if pct and days else None
            set_setting("sale_percent", pct)
            set_setting("sale_until", until)
            if not pct:
                return "Sale ended. Prices are back to normal."
            return f"Sale on: {pct}% off everything" + (f" until {fmt_date(until)}." if until else " until you end it with /sale 0.")
    except ValueError as e:
        return str(e)
    return None


async def handle(u: dict):
    # 1. Telegram asks if the payment may go ahead. We have 10 seconds to answer.
    q = u.get("pre_checkout_query")
    if q:
        c = get_campaign(int(q["invoice_payload"]))
        ok = bool(c) and c["status"] == "awaiting_payment" and q["total_amount"] == price_of(c)
        extra = {} if ok else {"error_message": "This campaign can no longer be paid."}
        await tg("answerPreCheckoutQuery", pre_checkout_query_id=q["id"], ok=ok, **extra)
        return

    # 2. The payment went through.
    m = u.get("message")
    if m and m.get("successful_payment"):
        sp = m["successful_payment"]
        cid = int(sp["invoice_payload"])
        cur = db.execute(
            "UPDATE campaigns SET status = 'in_review', charge_id = ? WHERE id = ? AND status = 'awaiting_payment'",
            (sp["telegram_payment_charge_id"], cid),
        )
        db.commit()
        c = get_campaign(cid)
        if cur.rowcount == 0 or not c:
            # Paid for something that isn't awaiting payment (e.g. paid twice). Flag it so you can refund by hand.
            await tg(
                "sendMessage",
                chat_id=ADMIN_ID,
                text=(
                    f"Unexpected payment for promotion #{cid} from user {m['from']['id']}.\n"
                    f"Charge id: {sp['telegram_payment_charge_id']}\nCheck it and refund if needed."
                ),
            )
            return
        use_code(c["code"])
        await tg("sendMessage", chat_id=c["user_id"], text="Payment received. Your promotion is in review.")
        await send_for_review(c)
        return

    # Your own commands (only you, in your private chat with the bot): promo codes and sales.
    if m and m.get("from", {}).get("id") == ADMIN_ID and m.get("chat", {}).get("type") == "private":
        reply = admin_command(m.get("text") or "")
        if reply:
            await tg("sendMessage", chat_id=m["chat"]["id"], text=reply)
            return

    # /app (or /app@thisbot): a button that opens the Mini App.
    cmd = (m.get("text") or "").split()[0].lower() if m and m.get("text") else ""
    if cmd.split("@")[0] == "/app" and ("@" not in cmd or cmd.endswith("@" + BOT_USERNAME.lower())):
        chat = m["chat"]
        if not ORIGIN:
            await tg("sendMessage", chat_id=chat["id"], text="The app isn't set up yet. Try again later.")
        elif chat.get("type") == "private":
            await tg("sendMessage", chat_id=chat["id"], text="Tap below to open Advertise Pro.",
                     reply_markup={"inline_keyboard": [[{"text": "Open Advertise Pro", "web_app": {"url": ORIGIN + "/"}}]]})
        else:
            # Telegram only allows Mini App buttons in private chats, so send people to the bot.
            await tg("sendMessage", chat_id=chat["id"], text="Open Advertise Pro in a private chat with the bot.",
                     reply_markup={"inline_keyboard": [[{"text": "Open Advertise Pro", "url": f"https://t.me/{BOT_USERNAME}?start=app"}]]})
        return

    # Someone opens the bot chat. Point them at the Mini App and the promo channel.
    if m and m.get("chat", {}).get("type") == "private" and (m.get("text") or "").startswith("/start"):
        how = "Listing is free for now" if FREE_MODE else "Pay in Stars"
        text = f"Promote your public Telegram channel or bot. {how}; every promotion is reviewed first."
        buttons = []
        if ORIGIN:
            buttons.append([{"text": "Open Advertise Pro", "web_app": {"url": ORIGIN + "/"}}])
        if PROMO_CHANNEL:
            text += f"\n\nSee what's being promoted in @{PROMO_CHANNEL}."
            buttons.append([{"text": f"Follow @{PROMO_CHANNEL}", "url": f"https://t.me/{PROMO_CHANNEL}"}])
        extra = {"reply_markup": {"inline_keyboard": buttons}} if buttons else {}
        await tg("sendMessage", chat_id=m["chat"]["id"], text=text, **extra)
        return

    # 3. You press Approve or Reject.
    cb = u.get("callback_query")
    if cb and cb["from"]["id"] == ADMIN_ID:
        action, raw = cb.get("data", ":0").split(":")
        c = get_campaign(int(raw))
        if not c or c["status"] != "in_review":
            await tg("answerCallbackQuery", callback_query_id=cb["id"], text="Already handled.")
            return
        if action == "ok":
            try:
                post_id = await publish(c)
            except Exception as e:
                # Leave it in review so you can fix the channel and press Approve again.
                await tg("answerCallbackQuery", callback_query_id=cb["id"], show_alert=True,
                         text=f"Couldn't post in @{PROMO_CHANNEL}. Is the bot an admin who can post? ({e})"[:200])
                return
            until = int(time.time()) + PLANS[c["plan"]]["days"] * 86400
            cur = db.execute("UPDATE campaigns SET status = 'live', post_id = ?, live_until = ? WHERE id = ? AND status = 'in_review'",
                             (post_id, until, c["id"]))
            if cur.rowcount == 0:  # the buyer cancelled it meanwhile
                db.commit()
                await remove_post(post_id)
                await tg("answerCallbackQuery", callback_query_id=cb["id"], text="The buyer cancelled this promotion.", show_alert=True)
                return
            note = f"Your promotion of @{c['handle']} was approved and is live until {fmt_date(until)}."
            if post_id:
                note += f"\n\nSee it and share it: https://t.me/{PROMO_CHANNEL}/{post_id}"
            outcome = "Approved"
        else:
            if c["charge_id"]:
                try:
                    await tg("refundStarPayment", user_id=c["user_id"], telegram_payment_charge_id=c["charge_id"])
                except Exception as e:
                    # Leave it in review so you can try again.
                    await tg("answerCallbackQuery", callback_query_id=cb["id"], text=f"Refund failed: {e}"[:200], show_alert=True)
                    return
                note = f"Your promotion of @{c['handle']} wasn't approved. Your Stars have been refunded."
                outcome = "Rejected and refunded"
            else:
                note = f"Your promotion of @{c['handle']} wasn't approved."
                outcome = "Rejected"
            db.execute("UPDATE campaigns SET status = 'rejected', refunded = ? WHERE id = ?", (1 if c["charge_id"] else 0, c["id"]))
        db.commit()
        await tg("answerCallbackQuery", callback_query_id=cb["id"], text=outcome)
        # Swap the buttons for the outcome so the message shows what you decided.
        with contextlib.suppress(Exception):
            await tg(
                "editMessageReplyMarkup",
                chat_id=cb["message"]["chat"]["id"],
                message_id=cb["message"]["message_id"],
                reply_markup={"inline_keyboard": [[{"text": outcome, "callback_data": "done:0"}]]},
            )
        with contextlib.suppress(Exception):
            await tg("sendMessage", chat_id=c["user_id"], text=note)


def profile_texts(channel: str = None, free: bool = None) -> tuple[str, str]:
    """(description, short description) shown on the bot's profile and in an empty chat."""
    channel = PROMO_CHANNEL if channel is None else channel
    free = FREE_MODE if free is None else free
    where = f"posted in @{channel}" if channel else "promoted"
    pay = "Listing is free for now" if free else "Pay with Telegram Stars"
    refund = "" if free else "\n• Not approved? Your Stars are refunded automatically"
    description = (
        "Get your Telegram channel or bot in front of new people.\n\n"
        "• Pick how long to run it: 3, 7 or 14 days\n"
        f"• {pay}\n"
        f"• Every promotion is reviewed, then {where}{refund}\n\n"
        "Tap Start, then Open Advertise Pro."
    )
    short = "Promote your Telegram channel or bot." + (f" Live promotions: @{channel}" if channel else "")
    return description[:512], short[:120]


async def setup_profile():
    """Commands menu, the Open button by the message box, and descriptions if they're still empty.
    Never overwrites a description you've written yourself in BotFather."""
    await tg("setMyCommands", commands=[
        {"command": "start", "description": "Welcome and how it works"},
        {"command": "app", "description": "Open Advertise Pro"},
    ])
    # Extra commands only you see in your chat with the bot.
    with contextlib.suppress(Exception):
        await tg("setMyCommands", scope={"type": "chat", "chat_id": ADMIN_ID}, commands=[
            {"command": "start", "description": "Welcome and how it works"},
            {"command": "app", "description": "Open Advertise Pro"},
            {"command": "admin", "description": "Your discount commands"},
            {"command": "newcode", "description": "Make a promo code"},
            {"command": "codes", "description": "List promo codes"},
            {"command": "delcode", "description": "Delete a promo code"},
            {"command": "sale", "description": "Start or end a sale"},
        ])
    if ORIGIN:
        await tg("setChatMenuButton", menu_button={"type": "web_app", "text": "Open", "web_app": {"url": ORIGIN + "/"}})
    description, short = profile_texts()
    # Texts this server could have written earlier (other settings). Those may be replaced; yours may not.
    ours = [profile_texts(ch, fr) for ch in ("", PROMO_CHANNEL) for fr in (False, True)]
    current = (await tg("getMyDescription")).get("description", "")
    if current != description and (not current or current in [d for d, _ in ours]):
        await tg("setMyDescription", description=description)
    current = (await tg("getMyShortDescription")).get("short_description", "")
    if current != short and (not current or current in [sd for _, sd in ours]):
        await tg("setMyShortDescription", short_description=short)


async def poll():
    global BOT_USERNAME
    # Retry until Telegram answers: if this failed once at startup, the bot would never poll again.
    while True:
        try:
            BOT_USERNAME = (await tg("getMe"))["username"]
            print(f"connected as @{BOT_USERNAME}")
            await tg("deleteWebhook")
            break
        except Exception as e:
            note_error("startup", e)
            await asyncio.sleep(3)
    try:
        await setup_profile()
    except Exception as e:
        note_error("setting up the bot profile", e)  # cosmetic; never stops the bot
    offset = 0
    while True:
        try:
            updates = await tg(
                "getUpdates",
                offset=offset,
                timeout=30,
                allowed_updates=["pre_checkout_query", "message", "callback_query"],
            )
        except Exception as e:
            STATUS["polling"] = False
            note_error("getUpdates", e)
            await asyncio.sleep(3)
            continue
        STATUS["polling"] = True
        for u in updates:
            offset = u["update_id"] + 1
            try:
                await handle(u)
            except Exception as e:
                note_error("handling an update", e)
