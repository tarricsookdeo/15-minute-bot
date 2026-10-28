# 15-Minute Bot

A web app that trades Public.com's 15-minute prediction markets (BTC, ETH, Gold, EUR/USD, …) using
rules each user sets per market. Host it once and share the URL: every user connects with their own
Public API key and gets their own private bot, which keeps trading on the server, through deploys
and restarts, until they disconnect.

> **The bot places real orders with real money.** Nothing is traded until the user presses
> **Start**, which asks them to confirm first. They can lose the full amount paid for each contract.

## How it works for users

1. Open the app's URL and follow the setup wizard: paste a Public API key, pick the account,
   pick markets, set a rule, review, start.
2. The bot trades on the server, so it **keeps running after the page is closed** or the
   computer is turned off. The dashboard shows each market's status, live P/L on open positions,
   results and an activity log, all kept since the bot was started.
3. **Coming back:** the browser remembers the session (not the key), so reopening the app in the
   same browser goes straight to the running bot. On another device, pasting the same key rejoins
   it.
4. **Deploys and restarts don't interrupt it.** The hosted app saves each session (the key
   encrypted) in its database, and the next server picks every bot up where it left off: running
   state, open orders, P/L and history. Users don't need to do anything; the page shows
   "Reconnecting…" for a few seconds.
5. **The session ends** when the user clicks **Disconnect**, after a day with the bot stopped and
   no page open, or if Public stops accepting the key (e.g. it was deleted) for two minutes.
   Ending it stops the bot, cancels unfilled orders and deletes everything saved for it.
   Contracts already bought stay in their Public account and settle as normal.

Details users might ask about:

- **Refreshing the page or a Wi-Fi blip** doesn't end the session; the page reconnects.
- **A second tab or device** with the same key joins the same bot rather than starting another one.
- **Stop vs. Disconnect:** **Stop** pauses trading and keeps the results; **Disconnect** ends the
  session and clears them.
- **One bot per account:** if an account is already being traded in another session (connected
  with a different key), it can't be picked until that session ends. Two bots on one account could
  buy twice in the same window and would share its rate limit.
- **During a deploy** the bot pauses for a few seconds while the new server takes over. A buy or
  sell window that falls in that gap can be missed; orders already placed stay open at Public and
  are tracked by the new server.
- **Without a database** (`DATABASE_URL` unset, e.g. running locally) nothing is saved, and a
  restart ends every session: unfilled orders are cancelled and results cleared.

### Getting a Public API key

1. Log in to public.com and open the API settings page: <https://public.com/settings/security/api>.
   The setup wizard links to it too.
2. Generate a secret key and copy it.

The account needs to be able to trade prediction markets. If you can buy a 15-minute market (e.g.
"BTC 15 min") in Public's own app, you're set. Treat the key like a password: anyone who has it can
trade in the account. If it's ever exposed, delete it on that same page and it stops working
immediately.

## Security

- **Keys** are used only to talk to Public, and never written to logs or the page. With a database,
  each key (and session token) is stored **encrypted** with Fernet (AES-128-CBC + HMAC-SHA256)
  using `SESSION_ENCRYPTION_KEY`, which belongs in your hosting platform's secret store, never in
  the database or the repository. Ending a session deletes its saved data. Sessions are matched to keys by a
  SHA-256 hash, not the key itself.
- **What a leak would mean:** someone with both the database *and* `SESSION_ENCRYPTION_KEY` could
  trade every connected user's account until they delete their keys at Public. The database alone
  is useless without the encryption key. Keep the database's network access and wherever the
  secrets are stored locked down.
- **Session tokens** are random 256-bit values saved in the browser's `localStorage` (so a
  returning user skips the key) and sent in an `X-Session` header (or the WebSocket's first
  message). No cookies, so other websites can't send requests as a user, and tokens never appear in
  URLs or logs. Anyone using that browser profile can control the bot until **Disconnect** is
  clicked; deleting the key in Public's API settings also cuts it off.
- **Isolation:** every API call is scoped to the caller's session; one user can't see or act on
  another's bot. Account IDs are checked against the user's own accounts before anything else.
- **Abuse limits:** 10 connection attempts per minute per IP address, 200 sessions per server
  (`MAX_SESSIONS`), 50 markets per session, and size limits on every request field.
- **Browser hardening:** strict Content-Security-Policy (no inline or third-party scripts), no
  framing, `nosniff`, no referrer, HSTS when served over HTTPS. API docs are disabled.
- **Errors** are logged on the server; users only see a generic message.

## Reliability

- **Each user's bot is independent:** one session's errors can't stop another's.
- **Public's 10 requests/second limit is per account.** Each session has its own limiter that sends
  at most 8 per second, evenly spaced, with orders and cancels ahead of routine price checks. If
  Public still answers 429, that session pauses for the Retry-After time. Prices for all of a
  user's markets come from one batched request and open orders from one search, so typical use is
  1–3 requests per second.
- **Requests time out after 10 seconds**, so one slow response can't make the bot miss a buy window.
- **The page reconnects automatically** with back-off; the bot keeps trading meanwhile.
- **Every change is saved** to the database within half a second, and **every order is saved
  before it's sent**, so after a crash the next server knows about every order that could exist at
  Public and never places one twice. If the database can't be reached, the bot doesn't send the
  order.
- **One server at a time:** a server must hold a Postgres advisory lock before it runs any bot.
  During a deploy the new server reports healthy but waits ("ready": false in `/healthz`, 503
  "restarting" to the page) until the old one has stopped and released the lock. If a server ever
  loses the lock, it shuts itself down so two can never trade one account.
- **On shutdown** (deploys, restarts) bots are paused and saved, not ended; their orders are left
  for the next server. Without a database, every session cancels its unfilled orders and ends.
- **Health check** at `/healthz`; `"ready": true` once the server has taken over and resumed every
  saved session.

## Configuration

Environment variables:

| Variable | Default | What it does |
|---|---|---|
| `MAX_SESSIONS` | `200` | Most users connected at once |
| `DATABASE_URL` | unset | Where sessions are saved: `postgresql://…`, or `sqlite:///path` for a local file. Unset: nothing is saved and a restart ends every session |
| `SESSION_ENCRYPTION_KEY` | unset | Encrypts stored API keys and session tokens; required with `DATABASE_URL`. Comma-separate several to rotate (first one encrypts) |
| `PORT` | `8765` | Port the server listens on |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are trusted (real client IPs, HTTPS); set it when running behind a load balancer |

Create an encryption key with
`uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`,
and keep a copy somewhere safe: if it's lost, every saved session is lost with it.

**Rotating the encryption key:** set `SESSION_ENCRYPTION_KEY` to `<new key>,<old key>` and restart.
On start the server re-encrypts every saved session with the new key, so once it reports ready you
can set it to just `<new key>`. Replacing the key outright, without the old one alongside, makes
every saved session unreadable: they're deleted on the next start and users must connect again.

**Run a single server per database:** only one server can run the bots. A second one started on the
same Postgres database waits (see "One server at a time" above) until the first stops.

## Running it on your own computer

```sh
brew install uv          # or: curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
uv run python -m fifteen_bot
```

It opens at <http://127.0.0.1:8765>. Locally nothing is saved by default, so stopping the server
ends the session. To keep it through restarts, set `DATABASE_URL=sqlite:///bot.db` and a
`SESSION_ENCRYPTION_KEY`. Keep the terminal open; to stop the Mac sleeping while it trades,
start it with `caffeinate -i uv run python -m fifteen_bot`.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Public didn't accept that key" | Copy the whole key again, or generate a new one. Check it wasn't deleted in Public's API settings. |
| "Your session has ended" | Someone clicked Disconnect, the bot sat stopped with no page open for a day, Public stopped accepting the key, or the server restarted without a database. Connect again. |
| "The bot is restarting after an update" | A deploy is under way; the page retries by itself and the bot carries on once the new server has taken over. If it lasts more than a minute or two, check the server's logs and the database. |
| "This account is already being traded in another session" | Close the other session (Disconnect there), or wait for it to end. |
| "Too many connection attempts" | Wait a minute and try again. |
| "The bot is at capacity" | The server has `MAX_SESSIONS` users connected. Try later, or raise the limit. |
| "Reconnecting to the bot…" banner | The page lost its connection and is retrying. The bot keeps running meanwhile. |
| A card says "Not supported" | Markets with more than two outcomes (e.g. "Crypto Leader") can't be traded yet. Remove it. |
| Odd errors after an update (running locally) | Restart the server: Ctrl+C, then start it again. |

## How trading works

- A "market" is a recurring series such as `KXBTC15M`. Every 15 minutes it opens a new window
  (event). Each market has its own rule and can be switched on/off independently.
- Rule: between *start* and *stop* seconds before the window closes, buy YES/NO/either side
  once its ask is within *[lowest, highest]* chance (price in ¢ = implied %). At most **one trade
  per market per window**. Defaults: 2:00 → 0:10 left, 80–90%, either side, 1 contract.
- Orders are DAY limit buys at the ask rounded up to the cent, so the bot never pays more than the
  price the rule matched. Unfilled orders are cancelled when the window closes.
- **Selling early (optional):** a rule can also set a *take-profit* and/or a *stop-loss*, in ¢ per
  contract after fees. While the window is open and the engine is running, once
  `bid − estimated selling fee − (entry price + buying fee per contract)` is at least the
  take-profit, or at most minus the stop-loss, the bot sells the filled contracts with a DAY
  limit sell at the bid (rounded down to the cent). A sell that hasn't filled after 5 seconds is
  cancelled and re-placed at the new bid while the target still holds. A buy still filling is
  cancelled first. If Public rejects a sell, the bot doesn't retry, and the position settles as
  normal. Open sells are cancelled when the window closes.
- **Fees on a sell** are estimated as the same per-contract fee Public quoted for the buy, since
  Public gives no fee estimate for a sell before it's placed. The P&L of sold contracts uses that
  estimate too.
- After a window settles the trade is marked won/lost and P&L is recorded
  (`$1 × contracts still held` if won, plus anything sold early, minus cost and Public's estimated
  fees). A position sold out completely is marked **sold** straight away, and counts as a win or a
  loss by its P&L.
- **Selling event contracts through Public's API is untested**: the SDK only has a helper for
  buying them, so sells are sent as generic `SELL` limit orders on the `EVENTCONTRACT` instrument.
  Try a take-profit on a 1-contract position before relying on it.

## For developers

```sh
uv run pytest                                          # run the tests
uv run python -m fifteen_bot --port 9000 --no-browser  # other port, no auto-open
TEST_DATABASE_URL=postgresql://… uv run pytest         # also test against a real Postgres
```

```
fifteen_bot/
  app.py        web server: JSON API + WebSocket + dashboard, all scoped per session
  sessions.py   per-user sessions: create/rejoin by key, idle expiry, resume/suspend (tested)
  persist.py    saved sessions: encryption, Postgres/SQLite, takeover lock (tested)
  engine.py     one user's trading loop: windows, quotes, orders, settlement
  ratelimit.py  keeps a session under Public's 10/s limit (tested)
  strategy.py   strategy model and pure decision logic (tested)
  store.py      in-memory SQLite for one session's markets, rules and trades (mirrored to persist)
  static/       dashboard (plain HTML/CSS/JS, no build step)
.github/        CI: the tests, on every pull request
tests/          uv run pytest
```

Uses the official [`publicdotcom-py`](https://github.com/PublicDotCom/publicdotcom-py) SDK.
