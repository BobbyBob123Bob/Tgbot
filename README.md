# Advertise Pro

Telegram bot ([@adprobot_bot](https://t.me/adprobot_bot)) with a Mini App where people pay in
Telegram Stars to promote their public channel or bot. You review every promotion before it goes live,
and rejecting one refunds the Stars automatically.

| Path | What it is |
| --- | --- |
| `bot_server.py` | Server: the Mini App's API, Stars invoices, payments, approve/reject/refund |
| `requirements.txt`, `Procfile`, `.python-version` | Everything a host like Railway needs to run it |
| `webapp/index.html` | The Mini App (one self-contained page, hosted separately) |
| `assets/banner.png` | 640×360 banner for BotFather's `/newapp` / `/editapp` |

## Server settings

| Variable | Required | Value |
| --- | --- | --- |
| `BOT_TOKEN` | yes | From BotFather. Only ever put it here, never in the page, the repo or a chat. |
| `ADMIN_ID` | yes | Your Telegram user id (from @userinfobot). You get the Approve / Reject messages. |
| `WEBAPP_ORIGIN` | after step 6 | Address the page is hosted at, no trailing slash. The page can't reach the API until it's set. |
| `DB_PATH` | recommended | `/data/adpro.db` on a volume, so promotions survive redeploys. |

Open the server's address in a browser to check it's running: it answers `{"ok": true, ...}`.

## Deploy

1. Get the bot token: BotFather → `/mybots` → your bot → API Token.
2. Get your user id from @userinfobot (this is `ADMIN_ID`).
3. Put the code on GitHub (this repo).
4. On Railway: New project → Deploy from GitHub repo → this repo. Set `BOT_TOKEN` and `ADMIN_ID`.
   Add a volume mounted at `/data` and set `DB_PATH=/data/adpro.db`. Under Networking, generate a
   public domain; that address is `API_URL`. The start command comes from the `Procfile`.
5. In `webapp/index.html`, set `var API_URL = 'https://…';` to that address.
6. Host the page (e.g. drag the `webapp` folder onto Netlify Drop) to get its HTTPS address.
7. Set `WEBAPP_ORIGIN` on Railway to that address (no trailing slash). Railway redeploys.
8. In BotFather, `/newapp` (or `/editapp`) → set the page address and upload `assets/banner.png`.

**Test first with every plan set to 1 Star**, in both `PLANS` in `bot_server.py` and `PLANS` in
`webapp/index.html`. Pay once and approve, then pay again and reject to confirm the refund arrives.
Then put the real prices back.

## Not built yet

- Delivering promotions to users (approving only marks a promotion live).
- Checking that the buyer actually runs the channel or bot. Check this yourself before approving.
- Real stats (views, clicks) and private channels.
