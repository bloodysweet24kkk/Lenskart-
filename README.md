# Lenskart Bot — Render FREE Web Service

This runs your Telegram bot on Render's **free** Web Service tier (no credit card, no payment).

## How it stays awake 24/7 (the trick)

Render free web services spin down after 15 min of inactivity. This repo includes two mechanisms to prevent that:

1. **`app.py`** — serves a tiny HTTP page on `$PORT` (Render requires a web service to listen on a port). The page auto-refreshes every 5 minutes.
2. **Self-pinger** — `app.py` pings its own `RENDER_EXTERNAL_URL` every 10 minutes so Render always sees activity.
3. **(Recommended) UptimeRobot** — set up a free monitor at https://uptimerobot.com hitting your Render URL every 5 minutes as a third safety net.

## Files

| File | Purpose |
|------|---------|
| `run_bot.py` | Entry point — starts keep-alive server, then runs the bot with auto-restart |
| `app.py` | Keep-alive HTTP server on $PORT + self-pinger |
| `lenskart_bot.py` | The Telegram bot (long polling) |
| `requirements.txt` | `requests>=2.31.0` |
| `render.yaml` | Render Blueprint (web service, free plan) |

## Deploy steps

1. Push these files to your GitHub repo (`bloodysweet24kkk/Lenskart-`).
2. On Render: **New** → **Web Service** (NOT Background Worker).
3. Connect your GitHub repo.
4. Settings:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python3 -u run_bot.py`
   - **Plan:** Free
5. Add Environment Variables:
   - `BOT_TOKEN` = your bot token
   - `ALLOWED_USER_ID` = your Telegram user ID
6. Click **Create Web Service**.
7. Wait for deploy. Check logs for `✅ Bot is running...`.
8. (Optional but recommended) Set up UptimeRobot to ping your Render URL every 5 min.
