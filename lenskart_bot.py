#!/usr/bin/env python3
"""
Lenskart "Run For Frame" - TELEGRAM BOT VERSION (Termux / Python 3.14 compatible)
Clean & fast: only sends the final result. No progress spam.

Uses the raw Telegram Bot API via long polling (requests only).
No asyncio / no python-telegram-bot dependency -> works on Python 3.14 in Termux.

Usage:
    1. pip install requests
    2. python3 deep7_(1).py
    3. In Telegram: /start  -> send phone number -> reply with OTP
"""

import json
import random
import time
import uuid
import hashlib
import base64
import logging
import threading
from datetime import datetime

import requests

# ============================================================
#  CONFIG  --  EDIT THESE IF YOU EVER CHANGE BOT / ACCOUNT
# ============================================================
BOT_TOKEN = "8612664891:AAEoLHLXMpgYO7hWL7KFhbucQHVw4aMoqco"
ALLOWED_USER_ID = 8558480999   # Only this Telegram user can operate the bot
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("lenskart-bot")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
BASE = "https://api-gateway.juno.lenskart.com"

# Device pools for randomization
BRANDS = ["xiaomi", "realme", "samsung", "oneplus", "oppo", "vivo"]
MODELS = {
    "xiaomi": ["Mi 11X", "Redmi Note 10", "Mi 10", "Poco X3"],
    "realme": ["RMX3031", "RMX3370", "RMX3360", "RMX3263"],
    "samsung": ["SM-G998B", "SM-G991B", "SM-A526B", "SM-M515F"],
    "oneplus": ["LE2115", "LE2125", "KB2001", "IN2015"],
    "oppo": ["CPH2207", "CPH2249", "CPH2217"],
    "vivo": ["V2024", "V2036", "V2041", "V2115"],
}
ANDROID_VERSIONS = ["13", "14"]

# Pending sessions:
#   OTP flow:    {chat_id: {"device": LenskartFakeDevice, "phone": str}}
#   Email flow:  {chat_id: {"stage": "email" | "password", "email": str, "device": LenskartFakeDevice}}
_pending_sessions = {}
_email_pending = {}
_sessions_lock = threading.Lock()


# ============================================================
#  TELEGRAM HELPERS (raw Bot API)
# ============================================================

def tg_send(chat_id: int, text: str, parse_mode: str = "HTML"):
    try:
        r = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if r.status_code != 200:
            logger.warning(f"sendMessage failed {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"sendMessage exception: {e}")


def tg_get_updates(offset: int, timeout: int = 30):
    try:
        r = requests.get(
            f"{TELEGRAM_API}/getUpdates",
            params={"offset": offset, "timeout": timeout, "allowed_updates": json.dumps(["message"])},
            timeout=timeout + 10,
        )
        if r.status_code == 200:
            return r.json().get("result", [])
        logger.warning(f"getUpdates failed {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"getUpdates exception: {e}")
    return []


# ============================================================
#  LENSKART DEVICE CLASS  (silent - no progress logging)
# ============================================================

class LenskartFakeDevice:
    def __init__(self, phone: str, phone_code: str = "+91"):
        self.phone = phone
        self.phone_code = phone_code

        self.brand = random.choice(BRANDS)
        self.model = random.choice(MODELS.get(self.brand, ["RMX3031"]))
        self.android_version = random.choice(ANDROID_VERSIONS)
        self.udid = self.generate_udid()
        self.advertising_id = str(uuid.uuid4())
        self.build_version = f"TP1A.220905.00{random.randint(1, 9)}"

        self.session_token = None
        self.auth_token = None
        self.user_id = None
        self.customer_type = "EXISTING"
        self.s = requests.Session()

        self.x_assertion = self.generate_x_assertion()

    def generate_udid(self):
        return uuid.uuid4().hex[:16]

    def generate_x_assertion(self):
        device_data = f"{self.udid}:{self.advertising_id}:{self.brand}:{self.model}:{self.phone}"
        hash_obj = hashlib.sha256(device_data.encode())
        hash_bytes = hash_obj.digest()
        assertion = base64.b64encode(hash_bytes).decode("utf-8")
        assertion = assertion.replace("+", "-").replace("/", "_")
        while len(assertion) < 100:
            assertion += random.choice(
                "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            )
        return assertion[:100]

    def base_headers(self, extra: dict | None = None) -> dict:
        h = {
            "Content-Type": "application/json; charset=UTF-8",
            "api_key": "valyoo123",
            "x-api-client": "android",
            "x-app-version": "5.8.2 (260713001)",
            "appversion": "5.8.2 (260713001)",
            "X-Build-Version": "260713001",
            "x-country-code": "IN",
            "x-country-code-override": "IN",
            "x-accept-language": "en",
            "accept-language": "en",
            "x-customer-type": self.customer_type,
            "udid": self.udid,
            "uniqueId": self.advertising_id[:16],
            "brand": self.brand,
            "model": self.model,
            "x-b3-traceid": str(int(time.time() * 1000)),
            "User-Agent": f"Dalvik/2.1.0 (Linux; U; Android {self.android_version}; {self.model} Build/{self.build_version})",
            "Accept-Encoding": "gzip",
            "Connection": "Keep-Alive",
        }
        if self.phone:
            h["x-customer-phone"] = self.phone
            h["x-customer-phone-code"] = self.phone_code.replace("+", "")
        if self.session_token:
            h["x-session-token"] = self.session_token
        if self.x_assertion:
            h["x-assertion"] = self.x_assertion
        if extra:
            h.update(extra)
        return h

    def post(self, path, body=None, params=None):
        headers = self.base_headers()
        url = f"{BASE}{path}"
        if params:
            url += "?" + "&".join([f"{k}={v}" for k, v in params.items()])
        return self.s.post(url, headers=headers, json=body, timeout=30)

    def get(self, path, params=None):
        headers = self.base_headers()
        url = f"{BASE}{path}"
        if params:
            url += "?" + "&".join([f"{k}={v}" for k, v in params.items()])
        return self.s.get(url, headers=headers, timeout=30)

    def create_session(self):
        r = self.post("/v2/sessions", {})
        if r.status_code == 200:
            data = r.json()
            self.session_token = data.get("result", {}).get("id")
            return True
        return False

    def send_otp(self):
        if not self.session_token:
            return None
        body = {"phoneCode": self.phone_code, "telephone": self.phone}
        r = self.post("/v3/customers/sendOtp", body)
        if r.status_code == 200:
            data = r.json()
            res = data.get("result") or {}
            self.customer_type = "NEW" if res.get("isNewUser") else "EXISTING"
            return res
        return None

    def verify_otp(self, code: str):
        body = {"code": code, "phoneCode": self.phone_code, "telephone": self.phone}
        r = self.post("/v2/customers/authenticate/mobile", body)
        if r.status_code == 200:
            data = r.json()
            res = data.get("result") or {}
            self.auth_token = res.get("token")
            self.user_id = res.get("user_id")
            if self.auth_token:
                self.session_token = self.auth_token
                return res
        return None

    def login_email(self, email: str, password: str):
        """Login with email + password. Returns (result_dict, status_code)."""
        body = {"email": email, "password": password}
        r = self.post("/v2/customers/authenticate", body)
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:500]}
        if r.status_code == 200:
            res = data.get("result") or {}
            self.auth_token = res.get("token") or res.get("authToken")
            self.user_id = res.get("user_id") or res.get("id") or res.get("userId")
            if self.auth_token:
                self.session_token = self.auth_token
                return res, 200
            return res, 200
        return data, r.status_code

    def build_steps_payload(self, steps: int = 30000):
        DAY_MS = 86400000
        ist_offset_ms = 5.5 * 3600 * 1000
        now_utc_ms = int(time.time() * 1000)
        now_ist_ms = now_utc_ms + ist_offset_ms
        today_midnight_ist = (now_ist_ms // DAY_MS) * DAY_MS
        today_midnight_utc = today_midnight_ist - ist_offset_ms
        step_counts = [0, 0, 0, 0, 0, 0, steps]
        payload = []
        for i in range(6, -1, -1):
            ts = today_midnight_utc - i * DAY_MS
            payload.append({"distance": 0.0, "steps": step_counts[i], "timestamp": int(ts)})
        return payload

    def claim_reward(self, steps: int = 30000):
        body = self.build_steps_payload(steps)
        params = {"campaignName": "run-for-frame"}
        r = self.post("/v2/customers/bff/campaign/eligibility", body, params)
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:500]}
        if r.status_code == 200:
            res = data.get("result") or {}
            # Save reward JSON if voucher present
            if res.get("giftVoucher"):
                filename = f"reward_{self.phone}.json"
                with open(filename, "w") as f:
                    json.dump(data, f, indent=2)
            return res, r.status_code
        return data, r.status_code

    def check_vouchers(self):
        r = self.get("/v2/customers/me/giftVoucher", params={"campaignName": "run-for-frame"})
        if r.status_code == 200:
            return r.json()
        return None


# ============================================================
#  FLOW HANDLERS  (run in worker threads)
# ============================================================

def authorized(user_id) -> bool:
    return user_id == ALLOWED_USER_ID


def cmd_start(chat_id):
    with _sessions_lock:
        _pending_sessions.pop(chat_id, None)
        _email_pending.pop(chat_id, None)
    help_text = (
        "🤖 <b>Lenskart Run-For-Frame Bot</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>Option 1 — Phone + OTP</b>\n"
        "Send a 10-digit phone number (without +91).\n"
        "1️⃣ You send phone number\n"
        "2️⃣ I send OTP to that number\n"
        "3️⃣ You reply with the OTP\n"
        "4️⃣ I claim the reward and send the result\n\n"
        "<b>Option 2 — Email + Password</b>\n"
        "Send /login , then I'll ask for your email and password.\n"
        "1️⃣ /login\n"
        "2️⃣ You send your email\n"
        "3️⃣ You send your password\n"
        "4️⃣ I log in and claim the reward\n\n"
        "Commands:\n"
        "/start  - show this menu\n"
        "/login  - start email login\n"
        "/cancel - cancel current flow"
    )
    tg_send(chat_id, help_text)


def cmd_cancel(chat_id):
    with _sessions_lock:
        a = _pending_sessions.pop(chat_id, None)
        b = _email_pending.pop(chat_id, None)
    if a or b:
        tg_send(chat_id, "✅ Cancelled. Send /start to begin again.")
    else:
        tg_send(chat_id, "ℹ️ Nothing to cancel.")


def cmd_login(chat_id):
    """Start the email login flow — ask for the email first."""
    with _sessions_lock:
        # clear any other pending flow
        _pending_sessions.pop(chat_id, None)
        _email_pending[chat_id] = {"stage": "email", "email": None, "device": None}
    tg_send(chat_id, "📧 Send your Lenskart account email address:")


def handle_email_step(chat_id, text):
    """Handle the email-login conversation: first email, then password."""
    with _sessions_lock:
        state = _email_pending.get(chat_id)
    if not state:
        return False  # not in email flow

    text = text.strip()

    if state["stage"] == "email":
        if "@" not in text or "." not in text.split("@")[-1]:
            tg_send(chat_id, "⚠️ That doesn't look like a valid email. Try again or /cancel.")
            return True
        with _sessions_lock:
            _email_pending[chat_id]["email"] = text
            _email_pending[chat_id]["stage"] = "password"
        tg_send(chat_id, "🔒 Send your password:")
        return True

    if state["stage"] == "password":
        email = state["email"]
        password = text
        with _sessions_lock:
            _email_pending.pop(chat_id, None)

        tg_send(chat_id, "🔐 Logging in, please wait...")

        def worker():
            device = LenskartFakeDevice(phone="email_login")
            if not device.create_session():
                tg_send(chat_id, "❌ Failed to start session. Try again.")
                return
            res, status = device.login_email(email, password)
            if status != 200 or not device.auth_token:
                msg = res.get("message") if isinstance(res, dict) else "Login failed"
                tg_send(chat_id, f"❌ Login failed: {msg}")
                return
            # Claim reward with the authenticated session
            rres, rstatus = device.claim_reward(steps=30000)
            if rres.get("giftVoucher"):
                msg = (
                    f"🎉 <b>REWARD UNLOCKED</b> — {email}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🏆 Tier: <code>{rres.get('tier')}</code>\n"
                    f"🎁 Voucher: <code>{rres.get('giftVoucher')}</code>\n"
                    f"📊 Steps: {rres.get('steps')}\n"
                )
                if rres.get("giftVoucherExpiryDate"):
                    exp = rres.get("giftVoucherExpiryDate")
                    exp_dt = datetime.fromtimestamp(exp / 1000)
                    msg += f"⏰ Expiry: {exp_dt.strftime('%d %b %Y')}\n"
                msg += "━━━━━━━━━━━━━━━━━━━━━━━\n💾 Saved to reward_email.json"
            elif rres.get("message"):
                msg = f"⚠️ {email}: {rres.get('message')}"
            elif rstatus != 200:
                msg = f"❌ {email}: Claim failed (HTTP {rstatus})"
            else:
                msg = f"⚠️ {email}: Reward not unlocked."
            tg_send(chat_id, msg)

        threading.Thread(target=worker, daemon=True).start()
        return True

    return False


def handle_phone(chat_id, text):
    phone = "".join(ch for ch in text if ch.isdigit())
    if len(phone) < 10:
        tg_send(chat_id, "⚠️ Please send a valid 10-digit phone number (without +91).")
        return
    phone = phone[-10:]

    def worker():
        device = LenskartFakeDevice(phone)
        if not device.create_session():
            tg_send(chat_id, f"❌ {phone}: Failed to start session. Try again.")
            return
        res = device.send_otp()
        if res is None:
            tg_send(chat_id, f"❌ {phone}: Could not send OTP. Check the number.")
            return
        with _sessions_lock:
            _pending_sessions[chat_id] = {"device": device, "phone": phone}
        tg_send(chat_id, f"🔑 OTP sent to {phone}!\nReply with the OTP digits.")

    threading.Thread(target=worker, daemon=True).start()


def handle_otp(chat_id, text):
    with _sessions_lock:
        session = _pending_sessions.get(chat_id)
    if not session:
        return

    code = "".join(ch for ch in text if ch.isdigit())
    if len(code) < 4:
        tg_send(chat_id, "⚠️ That doesn't look like an OTP. Send the digits only.")
        return

    device = session["device"]
    phone = session["phone"]

    with _sessions_lock:
        _pending_sessions.pop(chat_id, None)

    def worker():
        if not device.verify_otp(code):
            tg_send(chat_id, f"❌ {phone}: OTP verification failed.")
            return

        res, status = device.claim_reward(steps=30000)

        # Build ONE final concise message
        if res.get("giftVoucher"):
            msg = (
                f"🎉 <b>REWARD UNLOCKED</b> — {phone}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🏆 Tier: <code>{res.get('tier')}</code>\n"
                f"🎁 Voucher: <code>{res.get('giftVoucher')}</code>\n"
                f"📊 Steps: {res.get('steps')}\n"
            )
            if res.get("giftVoucherExpiryDate"):
                exp = res.get("giftVoucherExpiryDate")
                exp_dt = datetime.fromtimestamp(exp / 1000)
                msg += f"⏰ Expiry: {exp_dt.strftime('%d %b %Y')}\n"
            msg += "━━━━━━━━━━━━━━━━━━━━━━━\n💾 Saved to reward_{}.json".format(phone)
        elif res.get("message"):
            msg = f"⚠️ {phone}: {res.get('message')}"
        elif status != 200:
            msg = f"❌ {phone}: Claim failed (HTTP {status})"
        else:
            msg = f"⚠️ {phone}: Reward not unlocked."

        tg_send(chat_id, msg)

    threading.Thread(target=worker, daemon=True).start()


# ============================================================
#  MAIN LOOP  (long polling)
# ============================================================

def main():
    global BOT_TOKEN, ALLOWED_USER_ID, TELEGRAM_API

    print("=" * 60)
    print("🤖 LENSKART RUN-FOR-FRAME TELEGRAM BOT")
    print(f"🔒 Authorized user ID: {ALLOWED_USER_ID}")
    print("=" * 60)

    # Allow config override via environment variables (for cloud hosting)
    import os
    env_token = os.environ.get("BOT_TOKEN")
    env_user = os.environ.get("ALLOWED_USER_ID")
    if env_token:
        BOT_TOKEN = env_token
        TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
    if env_user:
        try:
            ALLOWED_USER_ID = int(env_user)
        except ValueError:
            pass

    try:
        me = requests.get(f"{TELEGRAM_API}/getMe", timeout=15).json()
        if me.get("ok"):
            print(f"✅ Bot connected: @{me['result']['username']}")
        else:
            print(f"❌ Bot token invalid: {me}")
            return
    except Exception as e:
        print(f"❌ Cannot reach Telegram API: {e}")
        return

    offset = 0
    print("✅ Bot is running... press Ctrl+C to stop.\n")

    try:
        while True:
            updates = tg_get_updates(offset, timeout=30)
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]
                user_id = msg.get("from", {}).get("id")
                text = (msg.get("text") or "").strip()

                if not authorized(user_id):
                    tg_send(chat_id, "🚫 You are not authorized to use this bot.")
                    continue

                if text == "/start":
                    cmd_start(chat_id)
                elif text == "/cancel":
                    cmd_cancel(chat_id)
                elif text == "/login":
                    cmd_login(chat_id)
                elif text.startswith("/"):
                    tg_send(chat_id, "Unknown command. Use /start, /login or /cancel.")
                else:
                    with _sessions_lock:
                        pending_otp = chat_id in _pending_sessions
                        pending_email = chat_id in _email_pending
                    if pending_email:
                        handle_email_step(chat_id, text)
                    elif pending_otp:
                        handle_otp(chat_id, text)
                    else:
                        handle_phone(chat_id, text)
    except KeyboardInterrupt:
        print("\n👋 Bot stopped.")
    except Exception as e:
        logger.exception(f"Polling loop crashed: {e}")
        raise


if __name__ == "__main__":
    # Auto-restart wrapper: if the bot crashes, it restarts after 5 seconds.
    # This keeps it running permanently without manual intervention.
    import os
    if os.environ.get("NO_AUTO_RESTART") == "1":
        main()
    else:
        while True:
            try:
                main()
            except KeyboardInterrupt:
                print("\n👋 Bot stopped by user.")
                break
            except Exception as e:
                print(f"\n⚠️ Bot crashed: {e}")
                print("🔁 Restarting in 5 seconds...")
                time.sleep(5)
