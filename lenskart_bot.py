#!/usr/bin/env python3
"""
Lenskart "Run For Frame" - TELEGRAM BOT VERSION (Multi-User + Points + Referral)
Clean & fast: only sends the final result. No progress spam.

Features:
  - Multi-user access (all Telegram users, not just one ID)
  - Points system (new users get 1 free point, 1 point per claim)
  - Referral system (earn points by inviting friends)
  - Force channel join (must join Telegram channel to use bot)
  - Device verification (block same device fingerprint multiple accounts)
  - Admin commands (ban/unban/addpoints/stats/broadcast)
  - Random device + session generation per login

Uses the raw Telegram Bot API via long polling (requests only).
No asyncio / no python-telegram-bot dependency -> works on Python 3.14 in Termux.
"""

import json
import random
import time
import uuid
import hashlib
import base64
import logging
import threading
import string
from datetime import datetime

import requests

# ============================================================
#  CONFIG  --  EDIT THESE
# ============================================================
BOT_TOKEN = "8612664891:AAEoLHLXMpgYO7hWL7KFhbucQHVw4aMoqco"

# Admin user IDs (can ban/unban/addpoints/broadcast)
ADMIN_USER_IDS = [8558480999]

# ===== FORCE-JOIN REQUIREMENTS =====
# Users MUST join these before they can use the bot.
# Bot must be ADMIN in each of these for getChatMember to work.

# Telegram Channel (private — uses invite link, bot must be admin)
CHANNEL_INVITE_LINK = "https://t.me/+wL3ZmELVqtBmNjc1"
# Once the bot is added as admin, we cache the channel's numeric chat_id here.
# Leave as None — it will be auto-detected on startup.
CHANNEL_CHAT_ID = None

# Telegram Group (public — uses @username)
GROUP_USERNAME = "swigyyyyyyyy"          # without @, public group
GROUP_CHAT_ID = -1003717324221           # numeric ID (auto-detected if bot is admin)
GROUP_INVITE_LINK = "https://t.me/swigyyyyyyyy"

# Points configuration
POINTS_NEW_USER = 1          # points given to new users on first /start
POINTS_PER_REFERRAL = 1      # points given to referrer when invitee joins
POINTS_COST_PER_CLAIM = 1    # points deducted per reward claim
MAX_DEVICES_PER_USER = 1     # max different device fingerprints per user

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
BASE = "https://api-gateway.juno.lenskart.com"

# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("lenskart-bot")

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

# Pending sessions (in-memory)
_pending_sessions = {}
_email_pending = {}
_menu_pending = {}
_sessions_lock = threading.RLock()

# Track highest update_id processed
_last_processed_update_id = 0

# File paths
import os as _os
_DATA_DIR = _os.path.dirname(_os.path.abspath(__file__))
HISTORY_FILE = _os.path.join(_DATA_DIR, "voucher_history.json")
USERS_FILE = _os.path.join(_DATA_DIR, "users.json")
DEVICES_FILE = _os.path.join(_DATA_DIR, "device_fingerprints.json")


# ============================================================
#  USER DATABASE  (JSON-based)
# ============================================================

def _load_users():
    """Load users database. Returns dict {user_id_str: user_data}."""
    try:
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_users(users):
    """Save users database (thread-safe)."""
    with _sessions_lock:
        try:
            with open(USERS_FILE, "w") as f:
                json.dump(users, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save users: {e}")


def get_user(user_id):
    """Get user data by ID. Returns user dict or None."""
    users = _load_users()
    return users.get(str(user_id))


def create_user(user_id, referred_by=None):
    """Create a new user. Awards initial points and referral bonus."""
    users = _load_users()
    uid = str(user_id)
    if uid in users:
        return users[uid]

    # Generate unique referral code
    ref_code = generate_referral_code(users)

    users[uid] = {
        "user_id": user_id,
        "points": POINTS_NEW_USER,
        "referral_code": ref_code,
        "referred_by": referred_by,
        "is_banned": False,
        "join_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "claims_count": 0,
        "referrals_count": 0,
    }
    _save_users(users)

    # Award referral bonus to referrer
    if referred_by:
        referrer = users.get(str(referred_by))
        if referrer and not referrer.get("is_banned"):
            referrer["points"] = referrer.get("points", 0) + POINTS_PER_REFERRAL
            referrer["referrals_count"] = referrer.get("referrals_count", 0) + 1
            _save_users(users)
            # Notify referrer
            try:
                tg_send(int(referred_by),
                        f"\U0001f381 <b>Referral Bonus!</b>\n"
                        f"A new user joined with your referral link!\n"
                        f"\U0001f4af You earned <b>{POINTS_PER_REFERRAL} point(s)</b>\n"
                        f"\U0001f4b0 Total points: <b>{referrer['points']}</b>")
            except Exception:
                pass

    return users[uid]


def add_points(user_id, amount):
    """Add points to a user. Returns new balance or None."""
    users = _load_users()
    uid = str(user_id)
    if uid not in users:
        return None
    users[uid]["points"] = users[uid].get("points", 0) + amount
    _save_users(users)
    return users[uid]["points"]


def deduct_points(user_id, amount):
    """Deduct points from a user. Returns (success, new_balance)."""
    users = _load_users()
    uid = str(user_id)
    if uid not in users:
        return False, 0
    current = users[uid].get("points", 0)
    if current < amount:
        return False, current
    users[uid]["points"] = current - amount
    _save_users(users)
    return True, users[uid]["points"]


def generate_referral_code(users=None):
    """Generate a unique 6-char referral code."""
    if users is None:
        users = _load_users()
    existing = {u.get("referral_code") for u in users.values()}
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if code not in existing:
            return code


def get_user_by_referral_code(code):
    """Find user_id by referral code. Returns int user_id or None."""
    users = _load_users()
    for uid, data in users.items():
        if data.get("referral_code") == code:
            return int(uid)
    return None


def is_admin(user_id):
    """Check if user is admin."""
    return user_id in ADMIN_USER_IDS


def ban_user(user_id):
    """Ban a user."""
    users = _load_users()
    uid = str(user_id)
    if uid in users:
        users[uid]["is_banned"] = True
        _save_users(users)
        return True
    return False


def unban_user(user_id):
    """Unban a user."""
    users = _load_users()
    uid = str(user_id)
    if uid in users:
        users[uid]["is_banned"] = False
        _save_users(users)
        return True
    return False


def get_all_user_ids():
    """Get list of all user IDs (for broadcast)."""
    users = _load_users()
    return [int(uid) for uid in users.keys() if not users[uid].get("is_banned")]


# ============================================================
#  DEVICE FINGERPRINT TRACKING
# ============================================================

def _load_devices():
    """Load device fingerprint database.
    Returns {device_hash: [user_id_str, ...]}"""
    try:
        with open(DEVICES_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_devices(devices):
    with _sessions_lock:
        try:
            with open(DEVICES_FILE, "w") as f:
                json.dump(devices, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save devices: {e}")


def device_hash_from_assertion(x_assertion):
    """Create a stable hash from x_assertion for comparison.
    Since x_assertion changes per session, we use the UDID+advertising_id
    pattern. But since those are random too, we use a different approach:
    we track by the user's Telegram device info instead.
    """
    return hashlib.sha256(x_assertion.encode()).hexdigest()[:32]


def check_and_register_device(user_id, device):
    """Check if this device fingerprint is already used by another user.
    Returns (allowed, reason)."""
    if not device.x_assertion:
        return True, None

    devices = _load_devices()
    # We use a combination of brand+model+udid as the device fingerprint
    # since x_assertion includes phone which changes per login
    fingerprint = f"{device.brand}:{device.model}:{device.udid}"
    fp_hash = hashlib.sha256(fingerprint.encode()).hexdigest()[:32]

    users_list = devices.get(fp_hash, [])
    uid = str(user_id)

    if uid in users_list:
        # Same user, same device - OK
        return True, None

    if len(users_list) >= MAX_DEVICES_PER_USER and uid not in users_list:
        # This device is already used by another user
        other_users = [u for u in users_list if u != uid]
        if other_users:
            return False, f"This device is already linked to another account. Multiple accounts from same device not allowed."

    # Register device for this user
    if uid not in users_list:
        users_list.append(uid)
        devices[fp_hash] = users_list
        _save_devices(devices)

    return True, None


# ============================================================
#  FORCE-JOIN VERIFICATION (Channel + Group)
# ============================================================

# Cache for resolved chat IDs (filled on startup)
_resolved_channel_id = None
_resolved_group_id = None


def _resolve_channel_id():
    """Try to resolve the private channel's numeric chat_id.
    The bot must be an admin/member of the channel for this to work.
    Returns the chat_id or None.
    """
    global _resolved_channel_id
    if _resolved_channel_id:
        return _resolved_channel_id
    if CHANNEL_CHAT_ID:
        _resolved_channel_id = CHANNEL_CHAT_ID
        return _resolved_channel_id
    # Try to detect by checking if the bot has any recent chats
    # Unfortunately, Telegram API doesn't let us resolve invite links directly
    # The user must add the bot to the channel as admin, then we can detect it
    # via getUpdates or by the user sending a forwarded message from the channel
    return None


def _resolve_group_id():
    """Resolve the group's numeric chat_id via its public username."""
    global _resolved_group_id
    if _resolved_group_id:
        return _resolved_group_id
    if GROUP_CHAT_ID:
        _resolved_group_id = GROUP_CHAT_ID
        return _resolved_group_id
    if GROUP_USERNAME:
        try:
            r = requests.post(
                f"{TELEGRAM_API}/getChat",
                json={"chat_id": f"@{GROUP_USERNAME}"},
                timeout=10,
            )
            if r.status_code == 200 and r.json().get("ok"):
                _resolved_group_id = r.json()["result"]["id"]
                return _resolved_group_id
        except Exception as e:
            logger.warning(f"Failed to resolve group ID: {e}")
    return None


def _check_membership(chat_id, user_id):
    """Check if user_id is a member of chat_id.
    Returns True if member/admin/creator, False otherwise.
    """
    try:
        r = requests.post(
            f"{TELEGRAM_API}/getChatMember",
            json={"chat_id": chat_id, "user_id": user_id},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("ok"):
                status = data.get("result", {}).get("status", "")
                return status in ("creator", "administrator", "member", "restricted")
        return False
    except Exception as e:
        logger.warning(f"Membership check failed for {chat_id}: {e}")
        return False


def check_channel_join(user_id):
    """Check if user has joined ALL required chats (channel + group).
    Returns True only if all checks pass (or no chats configured).
    """
    channel_id = _resolve_channel_id()
    group_id = _resolve_group_id()

    # Check channel (private)
    if channel_id:
        if not _check_membership(channel_id, user_id):
            return False

    # Check group (public)
    if group_id:
        if not _check_membership(group_id, user_id):
            return False

    return True


def get_not_joined_list(user_id):
    """Return list of chats the user hasn't joined yet.
    Each item is (name, invite_link).
    """
    not_joined = []
    channel_id = _resolve_channel_id()
    group_id = _resolve_group_id()

    if channel_id and not _check_membership(channel_id, user_id):
        not_joined.append(("Channel", CHANNEL_INVITE_LINK))
    if group_id and not _check_membership(group_id, user_id):
        not_joined.append(("Group", GROUP_INVITE_LINK))

    return not_joined


def get_channel_join_message():
    """Return the force-join prompt message with buttons for channel + group."""
    msg = (
        "\U0001f6d1 <b>Join Required!</b>\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "To use this bot, you must join our Channel AND Group first!\n\n"
    )

    buttons = []

    # Channel button (private — use invite link)
    if CHANNEL_INVITE_LINK:
        msg += f"\U0001f4f0 <b>Channel:</b> Join here\n"
        buttons.append([{"text": "\U0001f4f0 Join Channel", "url": CHANNEL_INVITE_LINK}])

    # Group button (public — use invite link)
    if GROUP_INVITE_LINK:
        msg += f"\U0001f465 <b>Group:</b> Join here\n"
        buttons.append([{"text": "\U0001f465 Join Group", "url": GROUP_INVITE_LINK}])

    msg += "\n\U0001f447 Join both, then click verify below \U0001f447"
    buttons.append([{"text": "\u2705 I Joined - Verify", "callback_data": "check_join"}])

    return msg, buttons


# ============================================================
#  VOUCHER HISTORY
# ============================================================

def _load_history():
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_history_entry(entry: dict):
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
    try:
        r = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": buttons},
            },
            timeout=15,
        )
        if r.status_code != 200:
            logger.warning(f"sendMessage(buttons) failed {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"sendMessage(buttons) exception: {e}")


def tg_answer_callback(callback_query_id: int, text: str = ""):
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
            params={"offset": offset, "timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query", "channel_post", "chat_member"])},
            timeout=timeout + 10,
        )
        if r.status_code == 200:
            return r.json().get("result", [])
        logger.warning(f"getUpdates failed {r.status_code}: {r.text[:200]}")
        return None
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

    def base_headers(self, extra=None):
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

    def send_register_otp(self, email: str, password: str, first_name: str):
        """Send OTP for a NEW account registration."""
        body = {
            "email": email,
            "mobile": self.phone,
            "phoneCode": self.phone_code,
            "firstName": first_name,
            "password": password,
        }
        r = self.post("/v3/customers/register/sendOtp", body)
        if r.status_code == 200:
            data = r.json()
            res = data.get("result") or {}
            self.customer_type = "NEW" if res.get("isNewUser") else "EXISTING"
            return res
        return None

    def register(self, email: str, password: str, first_name: str, otp: str):
        """Register a NEW Lenskart account using phone + OTP + email + password."""
        body = {
            "mobile": self.phone,
            "email": email,
            "password": password,
            "firstName": first_name,
            "otp": otp,
            "phoneCode": self.phone_code,
        }
        r = self.post("/v3/customers/register", body)
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
#  ACCESS CONTROL
# ============================================================

def user_can_access(user_id):
    """Check if user can access the bot (exists, not banned, channel joined)."""
    user = get_user(user_id)
    if not user:
        return False, "not_registered"
    if user.get("is_banned"):
        return False, "banned"
    if not check_channel_join(user_id):
        return False, "not_joined"
    return True, None


def ensure_user(user_id, referred_by=None):
    """Get or create user. Returns user dict."""
    user = get_user(user_id)
    if not user:
        user = create_user(user_id, referred_by)
    return user


# ============================================================
#  MENU & COMMANDS
# ============================================================

def cmd_start(chat_id, user_id, referral_code=None):
    """Show the interactive menu with inline buttons."""
    # Create user if new
    referred_by = None
    if referral_code:
        referrer_id = get_user_by_referral_code(referral_code)
        if referrer_id and referrer_id != user_id:
            existing = get_user(user_id)
            if not existing:  # only award if truly new user
                referred_by = referrer_id

    user = ensure_user(user_id, referred_by)

    # Check ban
    if user.get("is_banned"):
        tg_send(chat_id, "\U0001f6ab You have been banned from using this bot.")
        return

    # Check channel join
    if not check_channel_join(user_id):
        msg, buttons = get_channel_join_message()
        tg_send_with_buttons(chat_id, msg, buttons)
        return

    # Clear pending flows
    with _sessions_lock:
        _pending_sessions.pop(chat_id, None)
        _email_pending.pop(chat_id, None)
        _menu_pending.pop(chat_id, None)

    points = user.get("points", 0)
    is_new = referred_by is not None

    welcome = ""
    if is_new:
        welcome = (
            "\U0001f389 <b>Welcome to Lenskart Run-For-Frame Bot!</b>\n"
            f"\U0001f4b0 You got <b>{POINTS_NEW_USER} free point(s)</b> to start!\n"
        )
        if referred_by:
            welcome += f"\U0001f91d You were invited by a friend!\n"
        welcome += "\n"
    else:
        welcome = ""

    menu_text = (
        f"{welcome}"
        "\U0001f916 <b>Lenskart Run-For-Frame Bot</b>\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f4b0 <b>Your Points:</b> {points}\n"
        f"\U0001f465 <b>Total Users:</b> {len(_load_users())}\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "Choose an option below \U0001f447\n\n"
        f"\U0001f4f1 <b>Phone + OTP</b> \u2014 Login with phone ({POINTS_COST_PER_CLAIM} pt)\n"
        f"\U0001f4e7 <b>Email Login</b> \u2014 Login or create account ({POINTS_COST_PER_CLAIM} pt)\n"
        f"\U0001f4b0 <b>My Points</b> \u2014 View points & referral link\n"
        f"\U0001f4dc <b>Voucher History</b> \u2014 View past rewards\n"
        f"\u274c <b>Cancel</b> \u2014 Cancel current flow"
    )
    buttons = [
        [{"text": "\U0001f4f1 Phone + OTP", "callback_data": "menu_phone"},
         {"text": "\U0001f4e7 Email Login", "callback_data": "menu_email"}],
        [{"text": "\U0001f4b0 My Points", "callback_data": "menu_points"},
         {"text": "\U0001f4dc History", "callback_data": "menu_history"}],
        [{"text": "\u274c Cancel", "callback_data": "menu_cancel"}],
    ]
    tg_send_with_buttons(chat_id, menu_text, buttons)


def cmd_menu_phone(chat_id, user_id):
    """User pressed Phone button — check access & points, then ask for phone."""
    ok, reason = user_can_access(user_id)
    if not ok:
        if reason == "banned":
            tg_send(chat_id, "\U0001f6ab You have been banned.")
        elif reason == "not_joined":
            msg, buttons = get_channel_join_message()
            tg_send_with_buttons(chat_id, msg, buttons)
        elif reason == "not_registered":
            tg_send(chat_id, "Please send /start first.")
        return

    user = get_user(user_id)
    if user.get("points", 0) < POINTS_COST_PER_CLAIM:
        tg_send(chat_id,
                f"\U0001f6ab Not enough points!\n"
                f"You need <b>{POINTS_COST_PER_CLAIM} point(s)</b> to claim a reward.\n"
                f"\U0001f4b0 Your points: <b>{user.get('points', 0)}</b>\n\n"
                f"\U0001f465 Invite friends with your referral link to earn more points!\n"
                f"Use /points to see your referral link.")
        return

    with _sessions_lock:
        _pending_sessions.pop(chat_id, None)
        _email_pending.pop(chat_id, None)
        _menu_pending[chat_id] = "phone"
    tg_send(chat_id,
            "\U0001f4f1 <b>Phone + OTP Login</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\U0001f4b0 Points to be deducted: <b>{POINTS_COST_PER_CLAIM}</b>\n\n"
            "Send your 10-digit phone number (without +91).\n"
            "Example: <code>9876543210</code>")


def cmd_menu_email(chat_id, user_id):
    """User pressed Email button — check access & points, then start email flow."""
    ok, reason = user_can_access(user_id)
    if not ok:
        if reason == "banned":
            tg_send(chat_id, "\U0001f6ab You have been banned.")
        elif reason == "not_joined":
            msg, buttons = get_channel_join_message()
            tg_send_with_buttons(chat_id, msg, buttons)
        elif reason == "not_registered":
            tg_send(chat_id, "Please send /start first.")
        return

    user = get_user(user_id)
    if user.get("points", 0) < POINTS_COST_PER_CLAIM:
        tg_send(chat_id,
                f"\U0001f6ab Not enough points!\n"
                f"You need <b>{POINTS_COST_PER_CLAIM} point(s)</b> to claim a reward.\n"
                f"\U0001f4b0 Your points: <b>{user.get('points', 0)}</b>\n\n"
                f"\U0001f465 Invite friends with your referral link to earn more points!\n"
                f"Use /points to see your referral link.")
        return

    with _sessions_lock:
        _pending_sessions.pop(chat_id, None)
        _menu_pending.pop(chat_id, None)
        _email_pending[chat_id] = {"stage": "email", "email": None, "device": None, "user_id": user_id}
    tg_send(chat_id,
            "\U0001f4e7 <b>Email + Password Login</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\U0001f4b0 Points to be deducted: <b>{POINTS_COST_PER_CLAIM}</b>\n\n"
            "Send your Lenskart account email address:")


def cmd_points(chat_id, user_id):
    """Show user's points and referral link."""
    user = get_user(user_id)
    if not user:
        tg_send(chat_id, "Please send /start first.")
        return

    points = user.get("points", 0)
    ref_code = user.get("referral_code", "??????")
    referrals = user.get("referrals_count", 0)
    claims = user.get("claims_count", 0)
    bot_info = get_bot_username()
    ref_link = f"https://t.me/{bot_info}?start=ref_{ref_code}"

    msg = (
        "\U0001f4b0 <b>My Points & Referral</b>\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f4b0 Current Points: <b>{points}</b>\n"
        f"\U0001f465 Friends Invited: <b>{referrals}</b>\n"
        f"\U0001f381 Rewards Claimed: <b>{claims}</b>\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\U0001f517 <b>Your Referral Link:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"\U0001f4af Share this link with friends!\n"
        f"Each friend who joins earns you <b>{POINTS_PER_REFERRAL} point(s)</b>\n"
        f"New friends also get <b>{POINTS_NEW_USER} free point(s)</b>"
    )
    buttons = [
        [{"text": "\U0001f4e4 Share Referral Link", "url": f"https://t.me/share/url?url={ref_link}&text=Join%20Lenskart%20Run-For-Frame%20Bot%20and%20get%20free%20rewards!"}],
        [{"text": "\U0001f3e0 Back to Menu", "callback_data": "menu_start"}],
    ]
    tg_send_with_buttons(chat_id, msg, buttons)


_bot_username_cache = None
def get_bot_username():
    """Get bot username (cached)."""
    global _bot_username_cache
    if _bot_username_cache:
        return _bot_username_cache
    try:
        r = requests.get(f"{TELEGRAM_API}/getMe", timeout=10).json()
        if r.get("ok"):
            _bot_username_cache = r["result"]["username"]
            return _bot_username_cache
    except Exception:
        pass
    return "lenskart_bot"  # fallback


def cmd_voucher_history(chat_id):
    history = _load_history()
    if not history:
        tg_send(chat_id,
                "\U0001f4dc <b>Voucher History</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\U0001f4ed No vouchers claimed yet.\n"
                "Claim a reward to see it here!")
        return

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


def _handle_signup_yes(chat_id):
    """User clicked 'Yes, create account' after login failed."""
    with _sessions_lock:
        state = _email_pending.get(chat_id)
    if not state or state.get("stage") != "signup_phone":
        tg_send(chat_id, "\u274c Session expired. Please /start again.")
        return
    tg_send(chat_id,
            "\U0001f195 <b>Create New Account</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            "\U0001f4f1 Send your 10-digit phone number for OTP verification.\n"
            "OTP will be sent to this number.\n"
            "Example: <code>9876543210</code>")


# ============================================================
#  EMAIL FLOW HANDLER
# ============================================================

def handle_email_step(chat_id, text, user_id):
    """Handle the email login conversation with auto-account-creation."""

    with _sessions_lock:
        state = _email_pending.get(chat_id)
    if not state:
        return False

    # ---- STAGE: email ----
    if state["stage"] == "email":
        if "@" not in text or "." not in text.split("@")[-1]:
            tg_send(chat_id, "\u26a0\ufe0f That doesn't look like a valid email. Try again or /cancel.")
            return True
        with _sessions_lock:
            _email_pending[chat_id]["email"] = text
            _email_pending[chat_id]["stage"] = "password"
        tg_send(chat_id, "\U0001f511 Send your password:")
        return True

    # ---- STAGE: password ----
    if state["stage"] == "password":
        email = state["email"]
        password = text
        with _sessions_lock:
            _email_pending[chat_id]["password"] = password
        tg_send(chat_id, "\U0001f510 Logging in, please wait...")

        def worker():
            random_phone = random.choice("6789") + "".join(random.choice("0123456789") for _ in range(9))
            device = LenskartFakeDevice(phone=random_phone)
            if not device.create_session():
                tg_send(chat_id, "\u274c Failed to start session. Try again.")
                with _sessions_lock:
                    _email_pending.pop(chat_id, None)
                return

            # Device verification
            allowed, reason = check_and_register_device(user_id, device)
            if not allowed:
                tg_send(chat_id, f"\U0001f6ab {reason}")
                with _sessions_lock:
                    _email_pending.pop(chat_id, None)
                return

            res, status = device.login_email(email, password)
            if status == 200 and device.auth_token:
                # ---- LOGIN SUCCESS ----
                _claim_and_report(chat_id, user_id, device, email, password, "email")
                with _sessions_lock:
                    _email_pending.pop(chat_id, None)
            else:
                # ---- LOGIN FAILED -> AUTO-CREATE ACCOUNT ----
                msg = res.get("message") if isinstance(res, dict) else "Login failed"
                with _sessions_lock:
                    _email_pending[chat_id]["device"] = device
                    _email_pending[chat_id]["random_phone"] = random_phone
                    _email_pending[chat_id]["stage"] = "signup_phone"
                buttons = [
                    [{"text": "\u2705 Yes, create account", "callback_data": "signup_yes"},
                     {"text": "\u274c No, cancel", "callback_data": "menu_cancel"}],
                ]
                tg_send_with_buttons(
                    chat_id,
                    f"\u274c Login failed: {msg}\n\n"
                    f"\U0001f4e7 Email: <code>{email}</code>\n"
                    f"\U0001f195 This email may not have a Lenskart account.\n\n"
                    f"Want to <b>create a new account</b> with this email?\n"
                    f"(You'll need a phone number for OTP verification)",
                    buttons,
                )

        threading.Thread(target=worker, daemon=True).start()
        return True

    # ---- STAGE: signup_phone ----
    if state["stage"] == "signup_phone":
        phone = "".join(ch for ch in text if ch.isdigit())
        if len(phone) != 10:
            tg_send(chat_id,
                    "\u26a0\ufe0f Please send a valid 10-digit phone number.\n"
                    "Example: <code>9876543210</code>\n"
                    "Or /cancel to abort.")
            return True
        phone = phone[-10:]
        email = state["email"]
        password = state["password"]
        device = state.get("device")
        if not device:
            device = LenskartFakeDevice(phone=phone)
            if not device.create_session():
                tg_send(chat_id, "\u274c Failed to start session. Try again or /cancel.")
                with _sessions_lock:
                    _email_pending.pop(chat_id, None)
                return True
        else:
            device.phone = phone
            device.x_assertion = device.generate_x_assertion()

        res = device.send_register_otp(email, password, "User")
        if not res:
            tg_send(chat_id, f"\u274c Could not send OTP to {phone}. Try another number or /cancel.")
            return True
        with _sessions_lock:
            _email_pending[chat_id]["phone"] = phone
            _email_pending[chat_id]["device"] = device
            _email_pending[chat_id]["stage"] = "signup_otp"
        tg_send(chat_id,
                f"\U0001f4e8 OTP sent to <code>{phone}</code>\n\n"
                f"Send the OTP you received:")
        return True

    # ---- STAGE: signup_otp ----
    if state["stage"] == "signup_otp":
        otp = "".join(ch for ch in text if ch.isdigit())
        if len(otp) < 4:
            tg_send(chat_id, "\u26a0\ufe0f Please send the correct OTP.\nOr /cancel to abort.")
            return True
        email = state["email"]
        password = state["password"]
        phone = state.get("phone", "")
        device = state.get("device")
        with _sessions_lock:
            _email_pending[chat_id]["otp"] = otp
            _email_pending[chat_id]["stage"] = "signup_firstname"
        tg_send(chat_id,
                "\U0001f464 Send your first name for the new account:\n"
                "(This will be your Lenskart account name)")
        return True

    # ---- STAGE: signup_firstname ----
    if state["stage"] == "signup_firstname":
        first_name = text.strip()[:30]
        if not first_name:
            tg_send(chat_id, "\u26a0\ufe0f Please send a valid name.")
            return True
        email = state["email"]
        password = state["password"]
        phone = state.get("phone", "")
        otp = state.get("otp", "")
        device = state.get("device")
        with _sessions_lock:
            _email_pending.pop(chat_id, None)
        tg_send(chat_id, "\U0001f195 Creating your account, please wait...")

        def worker():
            if not device:
                tg_send(chat_id, "\u274c Session expired. Please /start again.")
                return
            res, status = device.register(email, password, first_name, otp)
            if status == 200 and device.auth_token:
                _claim_and_report(chat_id, user_id, device, email, password, "signup", phone)
            else:
                msg = res.get("message") if isinstance(res, dict) else f"Registration failed (HTTP {status})"
                tg_send(chat_id, f"\u274c Account creation failed: {msg}")

        threading.Thread(target=worker, daemon=True).start()
        return True

    return False


# ============================================================
#  CLAIM & REPORT (shared helper)
# ============================================================

def _claim_and_report(chat_id, user_id, device, email, password, account_type, phone=None):
    """Claim reward and send the result message. Also deduct points & save history."""
    rres, rstatus = device.claim_reward(steps=30000)
    if rres.get("giftVoucher"):
        # Deduct points on successful claim
        success, new_balance = deduct_points(user_id, POINTS_COST_PER_CLAIM)

        # Increment claims count
        users = _load_users()
        uid = str(user_id)
        if uid in users:
            users[uid]["claims_count"] = users[uid].get("claims_count", 0) + 1
            _save_users(users)

        msg = (
            f"\U0001f389 <b>REWARD UNLOCKED</b> \u2014 {email}\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\U0001f3c6 Tier: <code>{rres.get('tier')}</code>\n"
            f"\U0001f381 Voucher: <code>{rres.get('giftVoucher')}</code>\n"
            f"\U0001f4ca Steps: {rres.get('steps')}\n"
        )
        expiry_str = ""
        if rres.get("giftVoucherExpiryDate"):
            exp = rres.get("giftVoucherExpiryDate")
            try:
                exp_dt = datetime.fromtimestamp(exp / 1000)
                msg += f"\u23f0 Expiry: {exp_dt.strftime('%d %b %Y')}\n"
                expiry_str = exp_dt.strftime("%d %b %Y")
            except Exception:
                msg += f"\u23f0 Expiry: {exp}\n"
        if account_type == "signup":
            msg += "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\U0001f4be New account + reward claimed!"
        else:
            msg += "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\U0001f4be Reward claimed!"

        if success:
            msg += f"\n\n\U0001f4b0 Points deducted: {POINTS_COST_PER_CLAIM} | Remaining: {new_balance}"
        else:
            msg += f"\n\n\U0001f4b0 Current points: {new_balance}"

        history_entry = {
            "account": email,
            "type": account_type,
            "tier": rres.get("tier"),
            "voucher": rres.get("giftVoucher"),
            "steps": rres.get("steps"),
            "timestamp": datetime.now().strftime("%d %b %Y, %I:%M %p"),
            "user_id": user_id,
        }
        if phone:
            history_entry["phone"] = phone
        if expiry_str:
            history_entry["expiry"] = expiry_str
        _save_history_entry(history_entry)
    elif rres.get("message"):
        msg = f"\u26a0\ufe0f {email}: {rres.get('message')}"
    elif rstatus != 200:
        msg = f"\u274c {email}: Claim failed (HTTP {rstatus})"
    else:
        msg = f"\u26a0\ufe0f {email}: Reward not unlocked."
    tg_send(chat_id, msg)


# ============================================================
#  PHONE FLOW HANDLERS
# ============================================================

def handle_phone(chat_id, text, user_id):
    phone = "".join(ch for ch in text if ch.isdigit())
    if len(phone) < 10:
        tg_send(chat_id, "\u26a0\ufe0f Please send a valid 10-digit phone number (without +91).")
        return
    phone = phone[-10:]

    def worker():
        device = LenskartFakeDevice(phone)
        if not device.create_session():
            tg_send(chat_id, f"\u274c {phone}: Failed to start session. Try again.")
            return

        # Device verification
        allowed, reason = check_and_register_device(user_id, device)
        if not allowed:
            tg_send(chat_id, f"\U0001f6ab {reason}")
            return

        res = device.send_otp()
        if res is None:
            tg_send(chat_id, f"\u274c {phone}: Could not send OTP. Check the number.")
            return
        with _sessions_lock:
            _pending_sessions[chat_id] = {"device": device, "phone": phone, "user_id": user_id}
        tg_send(chat_id, f"\U0001f511 OTP sent to {phone}!\nReply with the OTP digits.")

    threading.Thread(target=worker, daemon=True).start()


def handle_otp(chat_id, text, user_id):
    with _sessions_lock:
        session = _pending_sessions.get(chat_id)
    if not session:
        return

    code = "".join(ch for ch in text if ch.isdigit())
    if len(code) < 4:
        tg_send(chat_id, "\u26a0\ufe0f That doesn't look like an OTP. Send the digits only.")
        return

    device = session["device"]
    phone = session["phone"]
    otp_user_id = session.get("user_id", user_id)

    with _sessions_lock:
        _pending_sessions.pop(chat_id, None)

    def worker():
        if not device.verify_otp(code):
            tg_send(chat_id, f"\u274c {phone}: OTP verification failed.")
            return

        # Device verification after OTP verify
        allowed, reason = check_and_register_device(otp_user_id, device)
        if not allowed:
            tg_send(chat_id, f"\U0001f6ab {reason}")
            return

        res, status = device.claim_reward(steps=30000)

        if res.get("giftVoucher"):
            # Deduct points
            success, new_balance = deduct_points(otp_user_id, POINTS_COST_PER_CLAIM)

            # Increment claims count
            users = _load_users()
            uid = str(otp_user_id)
            if uid in users:
                users[uid]["claims_count"] = users[uid].get("claims_count", 0) + 1
                _save_users(users)

            msg = (
                f"\U0001f389 <b>REWARD UNLOCKED</b> \u2014 {phone}\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001f3c6 Tier: <code>{res.get('tier')}</code>\n"
                f"\U0001f381 Voucher: <code>{res.get('giftVoucher')}</code>\n"
                f"\U0001f4ca Steps: {res.get('steps')}\n"
            )
            if res.get("giftVoucherExpiryDate"):
                exp = res.get("giftVoucherExpiryDate")
                exp_dt = datetime.fromtimestamp(exp / 1000)
                msg += f"\u23f0 Expiry: {exp_dt.strftime('%d %b %Y')}\n"
            msg += "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\U0001f4be Reward claimed!"
            if success:
                msg += f"\n\n\U0001f4b0 Points deducted: {POINTS_COST_PER_CLAIM} | Remaining: {new_balance}"
            else:
                msg += f"\n\n\U0001f4b0 Current points: {new_balance}"

            history_entry = {
                "account": phone,
                "type": "phone",
                "tier": res.get("tier"),
                "voucher": res.get("giftVoucher"),
                "steps": res.get("steps"),
                "timestamp": datetime.now().strftime("%d %b %Y, %I:%M %p"),
                "user_id": otp_user_id,
            }
            if res.get("giftVoucherExpiryDate"):
                try:
                    history_entry["expiry"] = exp_dt.strftime("%d %b %Y")
                except Exception:
                    history_entry["expiry"] = ""
            _save_history_entry(history_entry)
        elif res.get("message"):
            msg = f"\u26a0\ufe0f {phone}: {res.get('message')}"
        elif status != 200:
            msg = f"\u274c {phone}: Claim failed (HTTP {status})"
        else:
            msg = f"\u26a0\ufe0f {phone}: Reward not unlocked."

        tg_send(chat_id, msg)

    threading.Thread(target=worker, daemon=True).start()


# ============================================================
#  ADMIN COMMANDS
# ============================================================

def handle_admin_command(chat_id, text, user_id):
    """Handle admin commands. Returns True if handled."""
    if not is_admin(user_id):
        return False

    parts = text.split()
    cmd = parts[0].lower() if parts else ""

    if cmd == "/ban" and len(parts) >= 2:
        target = int(parts[1])
        if ban_user(target):
            tg_send(chat_id, f"\u2705 User {target} has been banned.")
        else:
            tg_send(chat_id, f"\u274c User {target} not found.")
        return True

    elif cmd == "/unban" and len(parts) >= 2:
        target = int(parts[1])
        if unban_user(target):
            tg_send(chat_id, f"\u2705 User {target} has been unbanned.")
        else:
            tg_send(chat_id, f"\u274c User {target} not found.")
        return True

    elif cmd == "/addpoints" and len(parts) >= 3:
        target = int(parts[1])
        try:
            amount = int(parts[2])
        except ValueError:
            tg_send(chat_id, "\u274c Invalid amount.")
            return True
        new_bal = add_points(target, amount)
        if new_bal is not None:
            tg_send(chat_id, f"\u2705 Added {amount} points to user {target}. New balance: {new_bal}")
        else:
            tg_send(chat_id, f"\u274c User {target} not found. They need to /start first.")
        return True

    elif cmd == "/stats":
        users = _load_users()
        history = _load_history()
        total_users = len(users)
        total_banned = sum(1 for u in users.values() if u.get("is_banned"))
        total_claims = len(history)
        total_points = sum(u.get("points", 0) for u in users.values())
        total_referrals = sum(u.get("referrals_count", 0) for u in users.values())
        msg = (
            "\U0001f4ca <b>Bot Statistics</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\U0001f465 Total Users: <b>{total_users}</b>\n"
            f"\U0001f6ab Banned Users: <b>{total_banned}</b>\n"
            f"\U0001f381 Total Claims: <b>{total_claims}</b>\n"
            f"\U0001f4b0 Total Points in Circulation: <b>{total_points}</b>\n"
            f"\U0001f91d Total Referrals: <b>{total_referrals}</b>"
        )
        tg_send(chat_id, msg)
        return True

    elif cmd == "/broadcast" and len(parts) >= 2:
        message_text = " ".join(parts[1:])
        all_ids = get_all_user_ids()
        sent = 0
        failed = 0
        for uid in all_ids:
            try:
                tg_send(uid, f"\U0001f4e2 <b>Admin Broadcast</b>\n\n{message_text}")
                sent += 1
                time.sleep(0.05)  # rate limit
            except Exception:
                failed += 1
        tg_send(chat_id, f"\u2705 Broadcast sent to {sent} users. Failed: {failed}")
        return True

    elif cmd == "/userinfo" and len(parts) >= 2:
        target = int(parts[1])
        user = get_user(target)
        if user:
            msg = (
                f"\U0001f464 <b>User Info: {target}</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001f4b0 Points: <b>{user.get('points', 0)}</b>\n"
                f"\U0001f91d Referrals: <b>{user.get('referrals_count', 0)}</b>\n"
                f"\U0001f381 Claims: <b>{user.get('claims_count', 0)}</b>\n"
                f"\U0001f517 Referral Code: <code>{user.get('referral_code', 'N/A')}</code>\n"
                f"\U0001f6ab Banned: <b>{'Yes' if user.get('is_banned') else 'No'}</b>\n"
                f"\U0001f4c5 Joined: <code>{user.get('join_date', 'N/A')}</code>"
            )
        else:
            msg = f"\u274c User {target} not found."
        tg_send(chat_id, msg)
        return True

    return False


# ============================================================
#  MAIN LOOP  (long polling)
# ============================================================

def main():
    global _resolved_channel_id, _resolved_group_id, _bot_username_cache, BOT_TOKEN, TELEGRAM_API, _last_processed_update_id
    print("=" * 60)
    print("\U0001f916 LENSKART RUN-FOR-FRAME BOT (Multi-User + Points)")
    print(f"\U0001f511 Admin IDs: {ADMIN_USER_IDS}")
    print(f"\U0001f4f0 Channel: {CHANNEL_INVITE_LINK}" if CHANNEL_INVITE_LINK else "\U0001f4f0 Channel: None (disabled)")
    print(f"\U0001f465 Group: @{GROUP_USERNAME}" if GROUP_USERNAME else "\U0001f465 Group: None (disabled)")
    print("=" * 60)

    import os
    env_token = os.environ.get("BOT_TOKEN")
    if env_token:
        BOT_TOKEN = env_token
        TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

    try:
        me = requests.get(f"{TELEGRAM_API}/getMe", timeout=15).json()
        if me.get("ok"):
            _bot_username_cache = me["result"]["username"]
            print(f"\u2705 Bot connected: @{_bot_username_cache}")
        else:
            print(f"\u274c Bot token invalid: {me}")
            return
    except Exception as e:
        print(f"\u274c Cannot reach Telegram API: {e}")
        return

    # Resolve group chat ID
    gid = _resolve_group_id()
    if gid:
        print(f"\u2705 Group resolved: @{GROUP_USERNAME} (ID: {gid})")
    else:
        print(f"\u26a0\ufe0f Group not resolved — make sure bot is admin of @{GROUP_USERNAME}")

    # Try to resolve channel chat ID
    cid = _resolve_channel_id()
    if cid:
        print(f"\u2705 Channel resolved (ID: {cid})")
    else:
        print(f"\u26a0\ufe0f Channel not resolved — bot needs to be added as admin to the private channel")
        print(f"   Channel invite: {CHANNEL_INVITE_LINK}")
        print(f"   After adding bot as admin, send any message in the channel to auto-detect the ID")
        print(f"   Or manually set CHANNEL_CHAT_ID in the config section")

    # Flush pending updates
    offset = 0
    print("\U0001f9f9 Flushing pending updates...", flush=True)
    while True:
        stale = tg_get_updates(offset, timeout=0)
        if not stale:
            break
        offset = stale[-1]["update_id"] + 1
        print(f"   flushed {len(stale)} old update(s)", flush=True)
    print("\u2705 Bot is running... press Ctrl+C to stop.\n", flush=True)

    try:
        while True:
            updates = tg_get_updates(offset, timeout=30)
            if updates is None:
                time.sleep(3)
                continue
            for u in updates:
                offset = u["update_id"] + 1

                global _last_processed_update_id
                if u["update_id"] <= _last_processed_update_id:
                    continue
                _last_processed_update_id = u["update_id"]

                # ---- Auto-detect channel ID from channel posts ----
                channel_post = u.get("channel_post")
                if channel_post:
                    post_chat = channel_post.get("chat", {})
                    post_chat_id = post_chat.get("id")
                    post_type = post_chat.get("type")
                    if post_type == "channel" and post_chat_id:
                        if not _resolved_channel_id:
                            _resolved_channel_id = post_chat_id
                            logger.info(f"Auto-detected channel ID: {post_chat_id} (title: {post_chat.get('title', 'unknown')})")
                    continue

                # ---- Handle chat_member updates (bot added to channel/group) ----
                chat_member_update = u.get("chat_member") or u.get("my_chat_member")
                if chat_member_update:
                    cm_chat = chat_member_update.get("chat", {})
                    cm_chat_id = cm_chat.get("id")
                    cm_type = cm_chat.get("type")
                    if cm_type == "channel" and cm_chat_id:
                        if not _resolved_channel_id:
                            _resolved_channel_id = cm_chat_id
                            logger.info(f"Bot added to channel! ID: {cm_chat_id} (title: {cm_chat.get('title', 'unknown')})")
                    continue

                # ---- Handle callback_query ----
                cb = u.get("callback_query")
                if cb:
                    cb_user_id = cb.get("from", {}).get("id")
                    cb_chat_id = cb.get("message", {}).get("chat", {}).get("id")
                    cb_data = cb.get("data", "")
                    cb_id = cb.get("id")

                    tg_answer_callback(cb_id)

                    # Check ban for all callbacks
                    user = get_user(cb_user_id)
                    if user and user.get("is_banned"):
                        tg_answer_callback(cb_id, "\U0001f6ab You are banned")
                        tg_send(cb_chat_id, "\U0001f6ab You have been banned from using this bot.")
                        continue

                    if cb_data == "check_join":
                        # User claims they joined the channel - verify
                        if check_channel_join(cb_user_id):
                            tg_answer_callback(cb_id, "\u2705 Verified! Welcome!")
                            cmd_start(cb_chat_id, cb_user_id)
                        else:
                            tg_answer_callback(cb_id, "\u274c You haven't joined yet!")
                            not_joined = get_not_joined_list(cb_user_id)
                            missing_names = ", ".join(n for n, _ in not_joined)
                            tg_send(cb_chat_id,
                                    f"\u274c You haven't joined the {missing_names} yet!\n"
                                    f"Please join first, then click verify again.")
                        continue

                    if cb_data == "menu_start":
                        cmd_start(cb_chat_id, cb_user_id)
                    elif cb_data == "menu_phone":
                        cmd_menu_phone(cb_chat_id, cb_user_id)
                    elif cb_data == "menu_email":
                        cmd_menu_email(cb_chat_id, cb_user_id)
                    elif cb_data == "menu_points":
                        cmd_points(cb_chat_id, cb_user_id)
                    elif cb_data == "menu_history":
                        cmd_voucher_history(cb_chat_id)
                    elif cb_data == "signup_yes":
                        _handle_signup_yes(cb_chat_id)
                    elif cb_data == "menu_cancel":
                        cmd_cancel(cb_chat_id)
                    continue

                # ---- Handle regular message ----
                msg = u.get("message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]
                user_id = msg.get("from", {}).get("id")
                text = (msg.get("text") or "").strip()

                # Handle /start with referral param
                if text.startswith("/start"):
                    parts = text.split(None, 1)
                    referral_code = None
                    if len(parts) > 1:
                        param = parts[1].strip()
                        if param.startswith("ref_"):
                            referral_code = param[4:]
                    cmd_start(chat_id, user_id, referral_code)
                    continue

                # Admin commands (check first, before user access checks)
                if is_admin(user_id) and text.startswith("/") and not text.startswith("/start"):
                    if handle_admin_command(chat_id, text, user_id):
                        continue

                # Regular commands
                if text == "/cancel":
                    cmd_cancel(chat_id)
                    continue
                elif text == "/points":
                    cmd_points(chat_id, user_id)
                    continue
                elif text == "/history":
                    cmd_voucher_history(chat_id)
                    continue
                elif text.startswith("/"):
                    # Unknown command
                    user = get_user(user_id)
                    if user and user.get("is_banned"):
                        tg_send(chat_id, "\U0001f6ab You have been banned.")
                        continue
                    tg_send(chat_id,
                            "Unknown command.\n"
                            "Available: /start, /points, /history, /cancel")
                    continue

                # For non-command messages, check user access
                user = get_user(user_id)
                if not user:
                    # Auto-register on first interaction
                    user = ensure_user(user_id)
                    # Check channel join for new users
                    if not check_channel_join(user_id):
                        msg_text, buttons = get_channel_join_message()
                        tg_send_with_buttons(chat_id, msg_text, buttons)
                        continue
                    tg_send(chat_id,
                            f"\U0001f389 Welcome! You got {POINTS_NEW_USER} free point(s)!\n"
                            "Send /start to see the menu.")
                    continue

                if user.get("is_banned"):
                    tg_send(chat_id, "\U0001f6ab You have been banned from using this bot.")
                    continue

                if not check_channel_join(user_id):
                    msg_text, buttons = get_channel_join_message()
                    tg_send_with_buttons(chat_id, msg_text, buttons)
                    continue

                # Route to flow handlers
                with _sessions_lock:
                    pending_otp = chat_id in _pending_sessions
                    pending_email = chat_id in _email_pending
                    menu_state = _menu_pending.get(chat_id)

                if pending_email:
                    handle_email_step(chat_id, text, user_id)
                elif pending_otp:
                    handle_otp(chat_id, text, user_id)
                elif menu_state == "phone":
                    with _sessions_lock:
                        _menu_pending.pop(chat_id, None)
                    handle_phone(chat_id, text, user_id)
                else:
                    # No active flow — treat as phone number
                    handle_phone(chat_id, text, user_id)

    except KeyboardInterrupt:
        print("\n\U0001f44b Bot stopped.")
    except Exception as e:
        logger.exception(f"Polling loop crashed: {e}")
        raise


if __name__ == "__main__":
    import os
    if os.environ.get("NO_AUTO_RESTART") == "1":
        main()
    else:
        while True:
            try:
                main()
            except KeyboardInterrupt:
                print("\n\U0001f44b Bot stopped by user.")
                break
            except Exception as e:
                print(f"\n\u26a0\ufe0f Bot crashed: {e}")
                print("\U0001f501 Restarting in 5 seconds...")
                time.sleep(5)
