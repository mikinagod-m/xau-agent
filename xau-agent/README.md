# xau-agent

Telegram trading agent for the XAU-U10 Pine engine.
Flow: TradingView `alert()` webhook → FastAPI on Railway → parse CTX payload →
Postgres journal → Claude grades the setup against your own backtest stats →
Telegram message with a TAKE / REDUCE / SKIP verdict.

No Pine changes required — the parser reads your existing entry, WATCH, and
`XAU-U9 LIFE` lifecycle alert formats as-is.

## Project layout

```
app/main.py      FastAPI app: /webhook/{token} and /health
app/parser.py    Parses entry / watch / lifecycle alert text
app/db.py        Postgres journal (trades + raw_alerts)
app/stats.py     Backtest seed + live journal stats for Claude's context
app/analyst.py   Claude call with strategy-aware system prompt
app/notify.py    Telegram sender + optional metalpriceapi spot check
seed/backtest_stats.json   Regime x direction stats from your Strategy Tester export
```

## Setup — step by step

### 1. Create the repo
```bash
mkdir xau-agent && cd xau-agent   # copy these files in
git init && git add -A && git commit -m "initial"
```
Push to a new GitHub repo (private). Open the folder in Cursor for future edits.

### 2. Telegram bot (5 min)
1. Message **@BotFather** → `/newbot` → pick a name → copy the **bot token**.
2. Open a chat with your new bot and send it any message.
3. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser —
   your **chat id** is `result[0].message.chat.id`.
4. Optional: create a second group/channel for WATCH noise, add the bot,
   grab that chat id the same way → `TELEGRAM_WATCH_CHAT_ID`.

### 3. Railway (10 min)
1. New Project → **Deploy from GitHub repo** → select this repo.
   Railway auto-detects Python via the `Procfile`.
2. In the project, click **+ New → Database → PostgreSQL**. Railway injects
   `DATABASE_URL` into the service automatically once you reference it:
   service → Variables → Add Reference → Postgres `DATABASE_URL`.
3. Service → **Variables**, add:
   - `WEBHOOK_TOKEN` — a long random string (`openssl rand -hex 24`)
   - `ANTHROPIC_API_KEY` — from console.anthropic.com
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (and `TELEGRAM_WATCH_CHAT_ID` if used)
   - `METALPRICE_API_KEY` — optional, enables live spot sanity-check in the prompt
4. Settings → **Networking → Generate Domain**. Note the URL, e.g.
   `https://xau-agent-production.up.railway.app`.
5. Check `https://<your-domain>/health` returns `{"ok": true}`.

### 4. Test before touching TradingView
```bash
curl -X POST "https://<your-domain>/webhook/<WEBHOOK_TOKEN>" \
  --data-binary $'XAU-U9 \u25b2 LONG (A SWEEP)\nEntry 3345.6  SL 3340.1  TP 3356.0  RR 2.1R\nCTX: Score=11/16|Regime=TREND|HTF=DISCOUNT|Sess=LONDON UK|Bias=LONG|Flow=BULL|Struct=BULL|VWAP=ABOVE|Loc=DEMAND|Zone=FRESH|State=EXPANSION|Candle=ENGULF|U9=PASS|Momentum=UP|RSI=58|MACDHist=0.45|RiskPts=5.5|TpPts=10.4\nASIA: H=3350.2 L=3338.9,life_id=TEST-1'
```
You should get a Telegram message with Claude's read within a few seconds.
Then test the exit: `--data-binary 'XAU-U9 LIFE,TP HIT,life_id=TEST-1'`.

### 5. TradingView alert (needs Essential plan or above for webhooks)
1. Add XAU-U10 to your XAUUSD chart with your live settings.
2. Create Alert → Condition: **XAU-U10** → **Any alert() function call**.
   (This single alert carries entries, WATCH, and lifecycle events — the
   script controls the message content.)
3. Expiration: Open-ended. Notifications tab → tick **Webhook URL**:
   `https://<your-domain>/webhook/<WEBHOOK_TOKEN>`
4. Leave the Message box as-is — `alert()` calls supply their own message.
5. Make sure in the script inputs: `Include Full Dashboard Context In Alert` = ON,
   `Trade Lifecycle Alerts` = ON, min alert grade as you prefer.

### 6. Local development (Cursor)
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # fill in values
export $(grep -v '^#' .env | xargs)   # or use a dotenv loader
uvicorn app.main:app --reload --port 8000
```
Runs fine without `DATABASE_URL` (journal disabled, Telegram still works).

## Refreshing the backtest seed
Re-export the Strategy Tester trade list (ideally with min grade = C so grades
can be compared), then regenerate `seed/backtest_stats.json` with the same
groupby (regime, direction) aggregation and redeploy.

## Notes and known limits
- Lifecycle TP/SL detection in Pine is bar-close based; same-bar TP+SL
  collisions follow the policy you set in the script inputs.
- Claude's verdict is decision support, not execution. Keep it suggestion-only
  and journal the outcomes — `trades.claude_read` stores every verdict so you
  can later measure whether TAKE-rated trades actually outperform SKIP-rated.
- Costs: one Claude call per qualifying entry. At a handful of A-grade signals
  per day this is pennies.
