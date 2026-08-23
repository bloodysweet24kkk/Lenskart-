# Lenskart Bot — Render FREE Web Service (Multi-User + Points + Referral)

This runs your Telegram bot on Render's **free** Web Service tier (no credit card, no payment).

## New Features (v2)

- **Multi-User Access** — All Telegram users can use the bot (not just one ID)
- **Points System** — New users get 1 free point, 1 point per reward claim
- **Referral System** — Earn points by inviting friends with your referral link
- **Force Channel Join** — Users must join your Telegram channel to use the bot
- **Device Verification** — Blocks same device fingerprint from creating multiple accounts
- **Admin Commands** — Ban/unban users, add points, view stats, broadcast messages
- **Random Device + Session** — Each login generates a fresh random device fingerprint

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
| `lenskart_bot.py` | The Telegram bot (multi-user, points, referral, channel join, device verification) |
| `requirements.txt` | `requests>=2.31.0` |
| `render.yaml` | Render Blueprint (web service, free plan) |

## Configuration

Edit the CONFIG section at the top of `lenskart_bot.py`:

```python
BOT_TOKEN = "your_bot_token"
ADMIN_USER_IDS = [123456789]          # your Telegram user ID(s)

# Force-join requirements
CHANNEL_INVITE_LINK = "https://t.me/+wL3ZmELVqtBmNjc1"  # private channel invite link
GROUP_USERNAME = "swigyyyyyyyy"          # public group username (without @)
GROUP_INVITE_LINK = "https://t.me/swigyyyyyyyy"  # public group invite link

# Points
POINTS_NEW_USER = 1                   # free points for new users
POINTS_PER_REFERRAL = 1               # points per successful referral
POINTS_COST_PER_CLAIM = 1             # points deducted per reward claim
MAX_DEVICES_PER_USER = 1              # max device fingerprints per user
```

## Bot Commands

### User Commands
| Command | Description |
|---------|-------------|
| `/start` | Show main menu (with referral support: `/start ref_CODE`) |
| `/points` | View your points balance and referral link |
| `/history` | View your voucher/reward history |
| `/cancel` | Cancel current flow |

### Admin Commands
| Command | Description |
|---------|-------------|
| `/ban USER_ID` | Ban a user from using the bot |
| `/unban USER_ID` | Unban a user |
| `/addpoints USER_ID AMOUNT` | Add points to a user |
| `/stats` | View bot statistics (total users, claims, points) |
| `/broadcast MESSAGE` | Send a message to all users |
| `/userinfo USER_ID` | View details about a specific user |

## Data Files

The bot creates these JSON files automatically:

| File | Purpose |
|------|---------|
| `users.json` | User database (points, referral codes, ban status, etc.) |
| `device_fingerprints.json` | Device fingerprint tracking (anti multi-account) |
| `voucher_history.json` | History of all successful reward claims |

## Deploy steps

1. Push these files to your GitHub repo.
2. On Render: **New** → **Web Service** (NOT Background Worker).
3. Connect your GitHub repo.
4. Settings:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python3 -u run_bot.py`
   - **Plan:** Free
5. Add Environment Variables:
   - `BOT_TOKEN` = your bot token
6. Click **Create Web Service**.
7. Wait for deploy. Check logs for `✅ Bot is running...`.
8. (Optional but recommended) Set up UptimeRobot to ping your Render URL every 5 min.

## Setup Checklist

1. **Add the bot as admin** to your Telegram **channel** (private: `https://t.me/+wL3ZmELVqtBmNjc1`) — the bot will auto-detect the channel ID once added
2. **Add the bot as admin** to your Telegram **group** (`@swigyyyyyyyy` / DEXTER TOOL) — so it can check member status
3. **Find your Telegram user ID** and add it to `ADMIN_USER_IDS`
4. **Deploy** to Render or run locally with `python3 run_bot.py`

> **Note:** The bot needs to be an **admin** in both the channel and group so it can use `getChatMember` to verify if users have joined. The channel ID is auto-detected when the bot is added or when any message is posted in the channel.
