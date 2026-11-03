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
| `PROMO_CHANNEL` | optional | Your promo channel, e.g. `@adpro_promos`. Approved promotions are posted there and removed when their days run out. The bot must be an admin with **Post messages** and **Delete messages**. |
| `FREE_MODE` | optional | `1` = listings are free (still reviewed). Use it to launch and grow the channel. |
| `TEST_PRICES` | optional | `1` = every plan costs 1 Star, for testing payments and refunds. Remove it afterwards. |

Prices live only in `PLANS` in `bot_server.py`; the page loads them from the server.

Open the server's address in a browser to check it's running. It shows which bot the token belongs
to and which of the settings above are on.

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

**Test first with `TEST_PRICES=1`** on Railway. Pay once and approve, then pay again and reject to
confirm the refund arrives. Then remove `TEST_PRICES`.

## Discounts

Send these to the bot from your own account (`ADMIN_ID`). Nobody else can use them.

| Command | What it does |
| --- | --- |
| `/newcode LAUNCH50 50 100 7` | Promo code: 50% off, first 100 uses, for 7 days. Uses and days are optional. `100`% makes it free. |
| `/codes` | List codes, how often each was used, and the current sale |
| `/delcode LAUNCH50` | Delete a code |
| `/sale 20 3` | 20% off everything for 3 days (days optional) |
| `/sale 0` | End the sale |
| `/admin` | Show these commands |

Buyers type codes in the Mini App. A sale and a code don't stack: the bigger discount wins.
A code's use is counted when the buyer pays (or submits, if it makes the promotion free).

## Not built yet

- Checking that the buyer actually runs the channel or bot. Check this yourself before approving.
- A "Discover" feed inside the Mini App, click tracking and stats.
- Private channels.
