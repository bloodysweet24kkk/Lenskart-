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
_menu_pending = {}  # {chat_id: "phone" | "email"} — set when user picks a menu button
_sessions_lock = threading.Lock()

# Track the highest update_id we've already processed.
# Survives across main() restarts within the same process, preventing
# duplicate messages after a crash/restart (offset=0 would re-fetch old updates).
_last_processed_update_id = 0

# Voucher history file — stores all successful reward claims
import os as _os
HISTORY_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "voucher_history.json")


def _load_history():
    """Load voucher history from JSON file. Returns a list."""
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_history_entry(entry: dict):
    """Append a voucher entry to the history file (thread-safe)."""
    with _sessions_lock:
        history = _load_history()
        history.append(entry)
        try:
            with open(HISTORY_FILE, "w") as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save history: {e}")


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


def tg_send_with_buttons(chat_id: int, text: str, buttons: list, parse_mode: str = "HTML"):
    """Send a message with an inline keyboard (buttons).
    buttons = list of rows, each row = list of button dicts:
        {"text": "Label", "callback_data": "some_callback"}
    """
    inline_keyboard = buttons  # already in the correct format
    try:
        r = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": inline_keyboard},
            },
            timeout=15,
        )
        if r.status_code != 200:
            logger.warning(f"sendMessage(buttons) failed {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"sendMessage(buttons) exception: {e}")


def tg_answer_callback(callback_query_id: int, text: str = ""):
    """Answer a callback query — removes the loading spinner on the button."""
    try:
        requests.post(
            f"{TELEGRAM_API}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"answerCallbackQuery exception: {e}")


def tg_get_updates(offset: int, timeout: int = 30):
    try:
        r = requests.get(
            f"{TELEGRAM_API}/getUpdates",
            params={"offset": offset, "timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query"])},
            timeout=timeout + 10,
        )
        if r.status_code == 200:
            return r.json().get("result", [])
        logger.warning(f"getUpdates failed {r.status_code}: {r.text[:200]}")
        return None  # signal error so caller retries without resetting offset
    except Exception as e:
        logger.warning(f"getUpdates exception: {e}")
        return None


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
    """Show the interactive menu with inline buttons."""
    with _sessions_lock:
        _pending_sessions.pop(chat_id, None)
        _email_pending.pop(chat_id, None)
        _menu_pending.pop(chat_id, None)
    menu_text = (
        "\U0001f916 <b>Lenskart Run-For-Frame Bot</b>\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "Choose an option below \U0001f447\n\n"
        "\U0001f4f1 <b>Phone + OTP</b> \u2014 Login with phone number\n"
        "\U0001f4e7 <b>Email + Password</b> \u2014 Login with email\n"
        "\U0001f4dc <b>Voucher History</b> \u2014 View past rewards\n"
        "\u274c <b>Cancel</b> \u2014 Cancel current flow"
    )
    buttons = [
        [{"text": "\U0001f4f1 Phone + OTP", "callback_data": "menu_phone"},
         {"text": "\U0001f4e7 Email Login", "callback_data": "menu_email"}],
        [{"text": "\U0001f4dc Voucher History", "callback_data": "menu_history"},
         {"text": "\u274c Cancel", "callback_data": "menu_cancel"}],
    ]
    tg_send_with_buttons(chat_id, menu_text, buttons)


def cmd_menu_phone(chat_id):
    """User pressed the Phone button \u2014 ask for phone number."""
    with _sessions_lock:
        _pending_sessions.pop(chat_id, None)
        _email_pending.pop(chat_id, None)
        _menu_pending[chat_id] = "phone"
    tg_send(chat_id,
            "\U0001f4f1 <b>Phone + OTP Login</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            "Send your 10-digit phone number (without +91).\n"
            "Example: <code>9876543210</code>")


def cmd_menu_email(chat_id):
    """User pressed the Email button \u2014 start email login flow."""
    with _sessions_lock:
        _pending_sessions.pop(chat_id, None)
        _menu_pending.pop(chat_id, None)
        _email_pending[chat_id] = {"stage": "email", "email": None, "device": None}
    tg_send(chat_id,
            "\U0001f4e7 <b>Email + Password Login</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            "Send your Lenskart account email address:")


def cmd_voucher_history(chat_id):
    """Show the voucher history \u2014 all past successful reward claims."""
    history = _load_history()
    if not history:
        tg_send(chat_id,
                "\U0001f4dc <b>Voucher History</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\U0001f4ed No vouchers claimed yet.\n"
                "Claim a reward to see it here!")
        return

    # Build the history message (show last 10 entries, most recent first)
    recent = history[-10:][::-1]
    msg = (
        "\U0001f4dc <b>Voucher History</b>\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f4ca Total claims: <b>{len(history)}</b>\n"
        f"Showing last {len(recent)}:\n\n"
    )
    for i, entry in enumerate(recent, 1):
        account = entry.get("account", "Unknown")
        tier = entry.get("tier", "\u2014")
        voucher = entry.get("voucher", "\u2014")
        steps = entry.get("steps", "\u2014")
        ts = entry.get("timestamp", "")
        expiry = entry.get("expiry", "")
        msg += (
            f"{i}\ufe0f\u20e3 <b>{account}</b>\n"
            f"   \U0001f3c6 Tier: <code>{tier}</code>\n"
            f"   \U0001f381 Voucher: <code>{voucher}</code>\n"
            f"   \U0001f4ca Steps: {steps}\n"
        )
        if expiry:
            msg += f"   \u23f0 Expiry: {expiry}\n"
        if ts:
            msg += f"   \U0001f550 {ts}\n"
        msg += "\n"

    buttons = [[{"text": "\U0001f3e0 Back to Menu", "callback_data": "menu_start"}]]
    tg_send_with_buttons(chat_id, msg, buttons)


def cmd_cancel(chat_id):
    with _sessions_lock:
        a = _pending_sessions.pop(chat_id, None)
        b = _email_pending.pop(chat_id, None)
        c = _menu_pending.pop(chat_id, None)
    if a or b or c:
        tg_send(chat_id, "\u2705 Cancelled. Send /start to begin again.")
    else:
        tg_send(chat_id, "\u2139\ufe0f Nothing to cancel.")


def cmd_login(chat_id):
    """Start the email login flow \u2014 ask for the email first."""
    with _sessions_lock:
        # clear any other pending flow
        _pending_sessions.pop(chat_id, None)
        _menu_pending.pop(chat_id, None)
        _email_pending[chat_id] = {"stage": "email", "email": None, "device": None}
    tg_send(chat_id, "\U0001f4e7 Send your Lenskart account email address:")


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
            random_phone = random.choice("6789") + "".join(random.choice("0123456789") for _ in range(9))
            device = LenskartFakeDevice(phone=random_phone)
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
                # Save to voucher history
                history_entry = {
                    "account": email,
                    "type": "email",
                    "tier": rres.get("tier"),
                    "voucher": rres.get("giftVoucher"),
                    "steps": rres.get("steps"),
                    "timestamp": datetime.now().strftime("%d %b %Y, %I:%M %p"),
                }
                if rres.get("giftVoucherExpiryDate"):
                    try:
                        history_entry["expiry"] = exp_dt.strftime("%d %b %Y")
                    except Exception:
                        history_entry["expiry"] = ""
                _save_history_entry(history_entry)
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
            # Save to voucher history
            history_entry = {
                "account": phone,
                "type": "phone",
                "tier": res.get("tier"),
                "voucher": res.get("giftVoucher"),
                "steps": res.get("steps"),
                "timestamp": datetime.now().strftime("%d %b %Y, %I:%M %p"),
            }
            if res.get("giftVoucherExpiryDate"):
                try:
                    history_entry["expiry"] = exp_dt.strftime("%d %b %Y")
                except Exception:
                    history_entry["expiry"] = ""
            _save_history_entry(history_entry)
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

    # Flush any pending updates that accumulated while the bot was offline.
    # This prevents duplicate messages after a restart (offset=0 would re-fetch
    # old /start commands etc). We drain the queue with timeout=0 then advance.
    offset = 0
    print("🧹 Flushing pending updates...", flush=True)
    while True:
        stale = tg_get_updates(offset, timeout=0)
        if not stale:
            break
        offset = stale[-1]["update_id"] + 1
        print(f"   flushed {len(stale)} old update(s)", flush=True)
    print("✅ Bot is running... press Ctrl+C to stop.\n", flush=True)

    try:
        while True:
            updates = tg_get_updates(offset, timeout=30)
            if updates is None:
                # Network hiccup — wait and retry WITHOUT crashing.
                # Crashing would reset offset=0 and reprocess old messages.
                time.sleep(3)
                continue
            for u in updates:
                offset = u["update_id"] + 1

                # Dedup guard: skip any update we've already processed.
                # This prevents double replies if the bot restarts and
                # re-fetches old updates that were already handled.
                global _last_processed_update_id
                if u["update_id"] <= _last_processed_update_id:
                    continue
                _last_processed_update_id = u["update_id"]

                # ---- Handle callback_query (inline button press) ----
                cb = u.get("callback_query")
                if cb:
                    cb_user_id = cb.get("from", {}).get("id")
                    cb_chat_id = cb.get("message", {}).get("chat", {}).get("id")
                    cb_data = cb.get("data", "")
                    cb_id = cb.get("id")

                    if not authorized(cb_user_id):
                        tg_answer_callback(cb_id, "\U0001f6ab Not authorized")
                        continue

                    # Answer the callback (removes loading spinner)
                    tg_answer_callback(cb_id)

                    # Route based on callback_data
                    if cb_data == "menu_start":
                        cmd_start(cb_chat_id)
                    elif cb_data == "menu_phone":
                        cmd_menu_phone(cb_chat_id)
                    elif cb_data == "menu_email":
                        cmd_menu_email(cb_chat_id)
                    elif cb_data == "menu_history":
                        cmd_voucher_history(cb_chat_id)
                    elif cb_data == "menu_cancel":
                        cmd_cancel(cb_chat_id)
                    else:
                        tg_answer_callback(cb_id, "Unknown option")
                    continue

                # ---- Handle regular message ----
                msg = u.get("message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]
                user_id = msg.get("from", {}).get("id")
                text = (msg.get("text") or "").strip()

                if not authorized(user_id):
                    tg_send(chat_id, "\U0001f6ab You are not authorized to use this bot.")
                    continue

                if text == "/start":
                    cmd_start(chat_id)
                elif text == "/cancel":
                    cmd_cancel(chat_id)
                elif text == "/login":
                    cmd_login(chat_id)
                elif text == "/history":
                    cmd_voucher_history(chat_id)
                elif text.startswith("/"):
                    tg_send(chat_id, "Unknown command. Use /start, /login, /history or /cancel.")
                else:
                    with _sessions_lock:
                        pending_otp = chat_id in _pending_sessions
                        pending_email = chat_id in _email_pending
                        menu_state = _menu_pending.get(chat_id)
                    if pending_email:
                        handle_email_step(chat_id, text)
                    elif pending_otp:
                        handle_otp(chat_id, text)
                    elif menu_state == "phone":
                        # User pressed Phone button, now they sent the number
                        with _sessions_lock:
                            _menu_pending.pop(chat_id, None)
                        handle_phone(chat_id, text)
                    else:
                        # No active flow — treat as phone number (backward compat)
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
