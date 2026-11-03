"""Advertise Pro bot server: Telegram Stars payments + the API the Mini App talks to.
People pay to promote a Telegram channel or bot.

Setup
  pip install fastapi uvicorn httpx
  export BOT_TOKEN=123456:ABC...            # from BotFather
  export ADMIN_ID=111222333                 # your own Telegram user id (you approve ads)
  export WEBAPP_ORIGIN=https://your-page-host.example   # where webapp/index.html is hosted (optional
                                            # at first; the page can't call the API until it's set)
  export DB_PATH=/data/adpro.db             # optional, default adpro.db
  uvicorn bot_server:app --host 0.0.0.0 --port 8000
  Put HTTPS in front of port 8000 (Caddy, nginx, or a host that provides it), then set API_URL
  in adpro_webapp.html to that address.

Flow
  1. The page POSTs the promotion (channel or bot, @username, description, plan) to /api/campaigns.
     The server saves it as "awaiting_payment" and returns a Stars invoice link.
  2. The page opens the invoice inside Telegram (WebApp.openInvoice).
  3. Telegram sends the bot a pre_checkout_query (we approve) and then a successful_payment.
  4. The promotion becomes "in_review" and you get a message with Approve / Reject buttons.
     Reject refunds the Stars automatically.

Note: this server does not check that the buyer owns the channel or bot. Look at each request
before you approve it, and never approve a username the buyer doesn't run.
"""
import asyncio
import contextlib
import hashlib
import hmac
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

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
# Optional until the page is hosted (deploy step 7). Until it is set, browsers can't call the API.
ORIGIN = os.environ.get("WEBAPP_ORIGIN", "").strip().rstrip("/")
API = f"https://api.telegram.org/bot{BOT_TOKEN}/"

# Prices are in Telegram Stars. Keep this in sync with PLANS in adpro_webapp.html.
PLANS = {
    "starter": {"days": 3, "stars": 100},
    "standard": {"days": 7, "stars": 200},
    "extended": {"days": 14, "stars": 350},
}
CATEGORIES = ["Crypto", "Tech", "News", "Business", "Education", "Entertainment", "Games", "Other"]
USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")

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
db.commit()

http = httpx.AsyncClient(timeout=40)


async def tg(method: str, **params):
    r = await http.post(API + method, json=params)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method} failed: {data.get('description')}")
    return data["result"]


def get_campaign(cid: int):
    return db.execute("SELECT * FROM campaigns WHERE id = ?", (cid,)).fetchone()


def verify_init_data(init_data: str) -> int:
    """Check the data Telegram gives the Mini App is genuine. Returns the user's id."""
    denied = HTTPException(401, "Open this page from Telegram and try again.")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    got = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not got or not hmac.compare_digest(calc, got):
        raise denied
    try:
        fresh = time.time() - int(pairs.get("auth_date", "0")) < 86400
        uid = int(json.loads(pairs["user"])["id"])
    except (ValueError, KeyError, TypeError):
        raise denied
    if not fresh:
        raise denied
    return uid


def user_id_from(authorization: str | None) -> int:
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(401, "Open this page from Telegram and try again.")
    return verify_init_data(authorization[4:])


async def make_invoice(cid: int, plan_id: str, handle: str) -> str:
    plan = PLANS[plan_id]
    return await tg(
        "createInvoiceLink",
        title=f"Promote @{handle}"[:32],
        description=f"Promote @{handle} to Telegram users for {plan['days']} days.",
        payload=str(cid),
        currency="XTR",  # Telegram Stars. No provider token is needed.
        prices=[{"label": f"{plan['days']} days", "amount": plan["stars"]}],
    )


class CampaignIn(BaseModel):
    plan: str
    kind: Literal["channel", "bot"]
    handle: str
    text: str = Field(min_length=1, max_length=200)
    category: str = "Other"
    lang: str = Field(default="Any", max_length=20)


@contextlib.asynccontextmanager
async def lifespan(_):
    task = asyncio.create_task(poll())
    yield
    task.cancel()


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
    return {"ok": True, "webapp_origin_set": bool(ORIGIN)}


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
    category = body.category if body.category in CATEGORIES else "Other"
    cur = db.execute(
        "INSERT INTO campaigns (user_id, kind, handle, text, category, lang, plan, status, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'awaiting_payment', ?)",
        (uid, body.kind, handle, body.text, category, body.lang, body.plan, int(time.time())),
    )
    db.commit()
    return {"id": cur.lastrowid, "invoice_link": await make_invoice(cur.lastrowid, body.plan, handle)}


@app.post("/api/campaigns/{cid}/invoice")
async def new_invoice(cid: int, authorization: str | None = Header(default=None)):
    uid = user_id_from(authorization)
    c = get_campaign(cid)
    if not c or c["user_id"] != uid or c["status"] != "awaiting_payment":
        raise HTTPException(404, "This campaign can't be paid.")
    return {"id": cid, "invoice_link": await make_invoice(cid, c["plan"], c["handle"])}


@app.get("/api/campaigns")
async def list_campaigns(authorization: str | None = Header(default=None)):
    uid = user_id_from(authorization)
    rows = db.execute("SELECT * FROM campaigns WHERE user_id = ? ORDER BY id DESC LIMIT 50", (uid,)).fetchall()
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "name": "@" + r["handle"],
            "status": r["status"],
            "days": PLANS[r["plan"]]["days"],
            "stars": PLANS[r["plan"]]["stars"],
        }
        for r in rows
    ]


async def channel_info(handle: str) -> str:
    """Best-effort look-up to help you review a channel. A failure never blocks anything."""
    try:
        chat = await tg("getChat", chat_id="@" + handle)
        members = await tg("getChatMemberCount", chat_id="@" + handle)
        return f"Looked up: {chat.get('type')} \"{chat.get('title')}\", {members} members"
    except Exception:
        return "Couldn't look this channel up. Check it yourself."


async def handle(u: dict):
    # 1. Telegram asks if the payment may go ahead. We have 10 seconds to answer.
    q = u.get("pre_checkout_query")
    if q:
        c = get_campaign(int(q["invoice_payload"]))
        ok = bool(c) and c["status"] == "awaiting_payment" and q["total_amount"] == PLANS[c["plan"]]["stars"]
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
        info = await channel_info(c["handle"]) if c["kind"] == "channel" else "Bot: not looked up."
        await tg("sendMessage", chat_id=c["user_id"], text="Payment received. Your promotion is in review.")
        await tg(
            "sendMessage",
            chat_id=ADMIN_ID,
            text=(
                f"New {c['kind']} promotion #{cid}: @{c['handle']}\n"
                f"{PLANS[c['plan']]['days']} days, {c['category']}, {c['lang']}\n"
                f"{info}\n\n{c['text']}\n\nhttps://t.me/{c['handle']}"
            ),
            reply_markup={"inline_keyboard": [[
                {"text": "Approve", "callback_data": f"ok:{cid}"},
                {"text": "Reject and refund", "callback_data": f"no:{cid}"},
            ]]},
        )
        return

    # Someone opens the bot chat. Point them at the Mini App.
    if m and m.get("chat", {}).get("type") == "private" and (m.get("text") or "").startswith("/start"):
        text = "Promote your public Telegram channel or bot. Pay in Stars; every promotion is reviewed first."
        extra = {}
        if ORIGIN:
            extra["reply_markup"] = {"inline_keyboard": [[{"text": "Open Advertise Pro", "web_app": {"url": ORIGIN + "/"}}]]}
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
            db.execute("UPDATE campaigns SET status = 'live' WHERE id = ?", (c["id"],))
            note = f"Your promotion of @{c['handle']} was approved and is live."
            outcome = "Approved"
        else:
            try:
                await tg("refundStarPayment", user_id=c["user_id"], telegram_payment_charge_id=c["charge_id"])
            except Exception as e:
                # Leave it in review so you can try again.
                await tg("answerCallbackQuery", callback_query_id=cb["id"], text=f"Refund failed: {e}"[:200], show_alert=True)
                return
            db.execute("UPDATE campaigns SET status = 'rejected' WHERE id = ?", (c["id"],))
            note = f"Your promotion of @{c['handle']} wasn't approved. Your Stars have been refunded."
            outcome = "Rejected and refunded"
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


async def poll():
    # Retry until Telegram answers: if this failed once at startup, the bot would never poll again.
    while True:
        try:
            await tg("deleteWebhook")
            break
        except Exception as e:
            print("deleteWebhook error:", e)
            await asyncio.sleep(3)
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
            print("poll error:", e)
            await asyncio.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                await handle(u)
            except Exception as e:
                print("handler error:", e)
