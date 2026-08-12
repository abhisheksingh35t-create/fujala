#!/usr/bin/env python3
"""
BLANK STORE — Referral Reward Bot with Anti-Fraud (Railway Ready)
------------------------------------------------------------------
  /start ref_<id> -> Join channels -> Math captcha -> Device verify
  1 referral = 1 💎 | 4 💎 = 1 Blinkit coupon 🎟

Anti-fraud layers:
  1. Math captcha (blocks bots)
  2. IP tracing (flags same-network multi-accounting for admin review)
  3. FingerprintJS device fingerprinting (blocks same-device multi-accounting)
  4. Signed Telegram WebApp initData verification (blocks spoofed verify-device calls)

Deploy on Railway:
  1. Push these files to a GitHub repo
  2. railway up  (or connect repo in Railway dashboard)
  3. Set env vars: BOT_TOKEN, ADMIN_IDS, MINI_APP_URL
  4. Bot auto-starts

SECURITY NOTE: BOT_TOKEN and ADMIN_IDS must be set as real environment
variables. This file intentionally has NO hardcoded token/admin fallback —
if you previously ran a version with a token baked into the source, treat
that token as burned and regenerate it via @BotFather immediately.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import random
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from flask import Flask, request, jsonify
import requests as http_requests

# ============================================================
# CONFIG — env vars (set in Railway dashboard)
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "blankk020")
STORE_NAME = os.getenv("STORE_NAME", "BLANK STORE")
# Set this to your Railway domain, e.g. https://myapp.up.railway.app
MINI_APP_URL = os.getenv("MINI_APP_URL", "")
PORT = int(os.getenv("PORT", 5000))
DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).resolve().parent)))
DIAMONDS_TO_REDEEM = int(os.getenv("DIAMONDS_TO_REDEEM", "4"))
CHANNELS = [
    {"username": "blankkdealz", "name": "BLANK DEALZ"},
    {"username": "earnwithsakx", "name": "EARN WITH SAKX"},
]
# How long (seconds) a "captcha passed, go verify your device" window stays
# valid before the mini-app verification link is considered stale.
MINI_APP_PENDING_TTL = 600
# Set to True to expose the raw ADMIN_IDS list via /myid and the "admins
# only" denial message. Handy while wiring up Railway env vars for the
# first time; leave False once the bot is live so random users can't see
# which Telegram IDs are admins.
DEBUG_EXPOSE_ADMIN_IDS = os.getenv("DEBUG_EXPOSE_ADMIN_IDS", "false").strip().lower() in ("1", "true", "yes")
# ============================================================

ADMIN_IDS = {int(x.strip()) for x in ADMIN_IDS_RAW.split() if x.strip()}
DATA_FILE = DATA_DIR / "refer_data.json"
_file_lock = threading.Lock()

(
    ADM_ADD_CODES,
    ADM_BROADCAST,
    ADM_SET_SUPPORT,
    ADM_SET_STORE,
    ADM_ADD_CH_UN,
    ADM_ADD_CH_NAME,
    WAIT_CAPTCHA,
) = range(7)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("refer-bot")

if not ADMIN_IDS:
    log.warning(
        "ADMIN_IDS is empty — no one will be able to open the admin panel. "
        "Set ADMIN_IDS in your environment (space-separated Telegram user IDs)."
    )

DEFAULT_DATA: dict[str, Any] = {
    "settings": {
        "store_name": STORE_NAME,
        "support_username": SUPPORT_USERNAME.lstrip("@"),
        "channels": json.loads(json.dumps(CHANNELS)),
        "diamonds_to_redeem": DIAMONDS_TO_REDEEM,
        "bot_username": "",
    },
    "users": {},
    "coupon_pool": [],
    "redemptions": [],
    "device_fingerprints": {},
    "seen_ips": {},
}


# =========================
# DATA I/O (thread-safe, atomic)
# =========================
def _load_data_unlocked() -> dict[str, Any]:
    """Load (and upgrade) refer_data.json. Caller must hold _file_lock."""
    if not DATA_FILE.exists():
        data = json.loads(json.dumps(DEFAULT_DATA))
        save_data_unlocked(data)
        return data
    with DATA_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)
    for k, v in DEFAULT_DATA.items():
        if k not in data:
            data[k] = json.loads(json.dumps(v))
    data.setdefault("settings", {})
    for sk, sv in DEFAULT_DATA["settings"].items():
        if sk not in data["settings"]:
            data["settings"][sk] = (
                json.loads(json.dumps(sv)) if isinstance(sv, (list, dict)) else sv
            )
    return data


def load_data() -> dict[str, Any]:
    """Read-only load. Do NOT mutate the result and call save_data() on it
    later — that read-modify-write pattern is not atomic across threads.
    For anything that reads then writes, use data_session() instead."""
    with _file_lock:
        return _load_data_unlocked()


def save_data(data: dict[str, Any]) -> None:
    with _file_lock:
        save_data_unlocked(data)


def save_data_unlocked(data: dict[str, Any]) -> None:
    """Write atomically: write to a temp file in the same dir, then rename over
    the real file. Prevents a crash mid-write from corrupting refer_data.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(DATA_DIR), prefix=".refer_data_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, DATA_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@contextmanager
def data_session():
    """Atomic read-modify-write: holds _file_lock across the ENTIRE
    load -> mutate -> save cycle, not just the individual load/save calls.

    Use this any time you're going to read data and then write it back
    based on what you read (crediting diamonds, popping a coupon, flipping
    `verified`, etc). Both the bot (async, single process) and the Flask
    webhook (threaded=True, same process) can run this concurrently, so
    without a single lock held for the whole cycle two requests can load
    the same snapshot and the second save silently clobbers the first
    (e.g. a lost diamond credit when two referrals verify at once).

    Usage:
        with data_session() as data:
            data["users"][uid]["diamonds"] += 1
        # saved automatically on clean exit; NOT saved if an exception
        # is raised inside the block.
    """
    with _file_lock:
        data = _load_data_unlocked()
        yield data
        save_data_unlocked(data)


# =========================
# HELPERS
# =========================
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_admin(user_id: int | None) -> bool:
    if not user_id:
        return False
    return int(user_id) in ADMIN_IDS


def get_user(data: dict, uid: int) -> dict | None:
    return data["users"].get(str(uid))


def parse_codes_blob(raw: str) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[\n,|]+", raw)
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        code = p.strip()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def referral_link(bot_username: str, user_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


def admin_denied_text(uid: int | None) -> str:
    if DEBUG_EXPOSE_ADMIN_IDS:
        configured = ", ".join(str(x) for x in sorted(ADMIN_IDS)) or "(empty)"
        return (
            "⛔ <b>Admins only</b>\n\n"
            f"Tumhara ID: <code>{uid}</code>\n"
            f"File ADMIN_IDS: <code>{configured}</code>\n\n"
            "1) /myid bhejo\n"
            "2) Railway env vars me ADMIN_IDS set karo\n"
            "3) Redeploy karo"
        )
    return (
        "⛔ <b>Admins only</b>\n\n"
        f"Tumhara ID: <code>{uid}</code>\n\n"
        "Agar tum owner ho: Railway env vars me apna ID ADMIN_IDS me add "
        "karke redeploy karo."
    )


def gen_captcha() -> tuple[str, int]:
    a = random.randint(1, 9)
    b = random.randint(1, 9)
    if random.random() < 0.5:
        return f"{a} + {b}", a + b
    if a < b:
        a, b = b, a
    return f"{a} - {b}", a - b


# =========================
# TELEGRAM WEBAPP INITDATA VALIDATION
# =========================
def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> dict | None:
    """Verify the signature Telegram attaches to WebApp initData.

    Returns the parsed key/value dict (with a validated `user` JSON string
    inside it) if the signature checks out and isn't stale, otherwise None.

    This is the ONLY way we should trust a user_id coming from the Mini App —
    never trust a `user_id` field the client sends us directly, since that's
    trivially spoofable by anyone calling the endpoint with curl/devtools.
    """
    if not init_data or not bot_token:
        return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except ValueError:
        return None
    if max_age_seconds and (time.time() - auth_date) > max_age_seconds:
        return None

    return parsed


# =========================
# KEYBOARDS — USER
# =========================
def join_channels_kb(data: dict) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for ch in data["settings"]["channels"]:
        rows.append([
            InlineKeyboardButton(
                f"➡️ Join {ch['name']}",
                url=f"https://t.me/{ch['username']}",
            )
        ])
    rows.append([InlineKeyboardButton("✅ I've Joined All", callback_data="verify:join")])
    return InlineKeyboardMarkup(rows)


def main_menu_kb(user_id: int | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("🔗 My Referral Link", callback_data="menu:reflink")],
        [InlineKeyboardButton("💎 My Diamonds", callback_data="menu:diamonds")],
        [InlineKeyboardButton("🎁 Redeem Coupon", callback_data="menu:redeem")],
        [
            InlineKeyboardButton("🎧 Support", callback_data="menu:support"),
            InlineKeyboardButton("📜 My Redemptions", callback_data="menu:myredem"),
        ],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="adm:home")])
    return InlineKeyboardMarkup(rows)


def back_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("« BACK", callback_data="menu:home")]]
    )


# =========================
# KEYBOARDS — ADMIN
# =========================
def admin_home_kb(data: dict) -> InlineKeyboardMarkup:
    codes_left = len(data.get("coupon_pool", []))
    n_users = len(data.get("users", {}))
    n_redem = len(data.get("redemptions", []))
    n_fraud = sum(1 for u in data.get("users", {}).values() if u.get("fraud_detected"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎟 Add Coupon Codes", callback_data="adm:addcodes")],
        [InlineKeyboardButton(f"📋 Codes Pool ({codes_left})", callback_data="adm:viewcodes")],
        [InlineKeyboardButton(f"👥 Users ({n_users})", callback_data="adm:users")],
        [InlineKeyboardButton(f"📜 Redemptions ({n_redem})", callback_data="adm:redem")],
        [InlineKeyboardButton(f"🚨 Fraud Log ({n_fraud})", callback_data="adm:fraud")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="adm:settings")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="adm:broadcast")],
        [InlineKeyboardButton("📊 Stats", callback_data="adm:stats")],
        [InlineKeyboardButton("🏠 User View", callback_data="menu:home")],
    ])


def admin_settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏪 Store Name", callback_data="adm:setstore")],
        [InlineKeyboardButton("🎧 Support Username", callback_data="adm:setsupport")],
        [InlineKeyboardButton("📡 Channels", callback_data="adm:channels")],
        [InlineKeyboardButton("« Admin Home", callback_data="adm:home")],
    ])


def admin_channels_kb(data: dict) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for ch in data["settings"]["channels"]:
        rows.append([
            InlineKeyboardButton(
                f"❌ @{ch['username']} ({ch['name']})",
                callback_data=f"adm:delch:{ch['username']}",
            )
        ])
    rows.append([InlineKeyboardButton("➕ Add Channel", callback_data="adm:addch")])
    rows.append([InlineKeyboardButton("« Settings", callback_data="adm:settings")])
    return InlineKeyboardMarkup(rows)


def cancel_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="adm:home")]]
    )


# =========================
# TEXT HELPERS
# =========================
def welcome_text(data: dict) -> str:
    s = data["settings"]
    return (
        f"👋 <b>WELCOME TO {html.escape(s['store_name'])}</b>\n\n"
        f"Refer friends & earn 💎 diamonds!\n"
        f"{s.get('diamonds_to_redeem', DIAMONDS_TO_REDEEM)} 💎 = 1 Blinkit Coupon 🎟\n\n"
        "To get started, <b>join our channels</b> below 👇"
    )


def main_menu_text(data: dict, u: dict) -> str:
    s = data["settings"]
    bot_un = s.get("bot_username", "")
    link = referral_link(bot_un, u["id"]) if bot_un else "(bot username not set)"
    needed = s.get("diamonds_to_redeem", DIAMONDS_TO_REDEEM)
    diamonds = u.get("diamonds", 0)
    progress = "🔵" * diamonds + "⚪" * max(0, needed - diamonds)
    return (
        f"🏠 <b>{html.escape(s['store_name'])}</b>\n\n"
        f"💎 Your Diamonds: <b>{diamonds}/{needed}</b>\n"
        f"{progress}\n"
        f"👥 Total Referrals: <b>{u.get('referral_count', 0)}</b>\n\n"
        f"🔗 Your Referral Link:\n<code>{html.escape(link)}</code>\n\n"
        f"Share this link! Each friend who joins = 1 💎\n"
        f"{needed} 💎 = 1 Blinkit Coupon 🎟"
    )


def support_text(data: dict) -> str:
    s = data["settings"]
    return (
        "🎧 <b>SUPPORT</b>\n\n"
        f"Need help? Contact: @{html.escape(s['support_username'])}\n\n"
        "Support hours: 10:00 AM – 10:00 PM IST"
    )


def admin_home_text(data: dict) -> str:
    s = data["settings"]
    n_users = len(data.get("users", {}))
    verified = sum(1 for u in data["users"].values() if u.get("verified"))
    total_diamonds = sum(u.get("diamonds", 0) for u in data["users"].values())
    total_refs = sum(u.get("referral_count", 0) for u in data["users"].values())
    n_redem = len(data.get("redemptions", []))
    codes_left = len(data.get("coupon_pool", []))
    n_fraud = sum(1 for u in data.get("users", {}).values() if u.get("fraud_detected"))
    n_fps = len(data.get("device_fingerprints", {}))
    ch_list = ", ".join(f"@{html.escape(c['username'])}" for c in s["channels"])
    dev_status = "✅ ON" if MINI_APP_URL else "❌ OFF (captcha only)"
    return (
        "🛠 <b>ADMIN PANEL</b>\n\n"
        f"🏪 Store: <b>{html.escape(s['store_name'])}</b>\n"
        f"🎧 Support: @{html.escape(s['support_username'])}\n"
        f"📡 Channels: {ch_list}\n"
        f"💎 Redeem cost: {s.get('diamonds_to_redeem', DIAMONDS_TO_REDEEM)} 💎\n"
        f"🛡 Device verify: {dev_status}\n\n"
        f"👥 Users: {n_users} ({verified} verified)\n"
        f"🔗 Total referrals: {total_refs}\n"
        f"💎 Diamonds in circulation: {total_diamonds}\n"
        f"🎟 Codes in pool: <b>{codes_left}</b>\n"
        f"📜 Redemptions: {n_redem}\n"
        f"🚨 Fraud blocked: {n_fraud}\n"
        f"📱 Devices tracked: {n_fps}\n\n"
        "<b>📌 ADMIN COMMANDS</b>\n"
        "/admin — yeh panel\n"
        "/myid — apna Telegram ID\n"
        "/cancel — wizard cancel\n"
        "/start — store menu\n"
    )


# =========================
# UI HELPERS
# =========================
async def safe_edit(query, context, text, reply_markup=None):
    try:
        await query.edit_message_text(
            text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        if query.message:
            try:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup,
                )
            except Exception:
                log.exception("safe_edit: fallback send_message also failed")
    except Exception:
        if query.message:
            try:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup,
                )
            except Exception:
                log.exception("safe_edit: fallback send_message also failed")


async def check_channel_membership(context, user_id, data):
    not_joined = []
    for ch in data["settings"]["channels"]:
        try:
            member = await context.bot.get_chat_member(f"@{ch['username']}", user_id)
            if member.status in ("left", "kicked"):
                not_joined.append(ch)
        except Exception as e:
            log.warning("Channel check @%s failed: %s", ch["username"], e)
            not_joined.append(ch)
    return not_joined


def clear_admin_draft(context):
    for k in list(context.user_data.keys()):
        if k.startswith("adm_"):
            context.user_data.pop(k, None)


# =========================
# USER COMMAND HANDLERS
# =========================
async def cmd_start(update, context):
    user = update.effective_user
    if not user:
        return ConversationHandler.END

    ref_id = None
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                ref_id = int(arg[4:])
            except ValueError:
                pass

    uid_str = str(user.id)

    with data_session() as data:
        if not data["settings"].get("bot_username"):
            try:
                me = await context.bot.get_me()
                data["settings"]["bot_username"] = me.username or ""
            except Exception:
                pass

        is_new = uid_str not in data["users"]
        if is_new:
            data["users"][uid_str] = {
                "id": user.id,
                "name": user.full_name,
                "username": user.username,
                "joined_at": now_iso(),
                "referred_by": ref_id if ref_id and str(ref_id) != uid_str else None,
                "verified": False,
                "referral_count": 0,
                "diamonds": 0,
                "redeemed_count": 0,
                "redemptions": [],
            }
        else:
            u = data["users"][uid_str]
            u["name"] = user.full_name
            u["username"] = user.username
            u["last_seen"] = now_iso()
            if not u.get("verified") and ref_id and not u.get("referred_by") \
                    and str(ref_id) != uid_str:
                u["referred_by"] = ref_id

        # Snapshot what we need for the reply — data/u go out of scope once
        # the `with` block exits and the session saves.
        u = data["users"][uid_str]
        verified = u.get("verified", False)
        reply_data = json.loads(json.dumps(data))
        reply_user = json.loads(json.dumps(u))

    if not verified:
        text = welcome_text(reply_data) + "\n\nAfter joining, tap <b>✅ I've Joined All</b>"
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=join_channels_kb(reply_data)
        )
        return ConversationHandler.END

    text = main_menu_text(reply_data, reply_user)
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=main_menu_kb(user.id)
    )
    if is_admin(user.id):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🛠 Admin? Tap below:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛠 OPEN ADMIN PANEL", callback_data="adm:home")]
            ]),
        )
    return ConversationHandler.END


async def cmd_myid(update, context):
    u = update.effective_user
    lines = [
        f"Your Telegram ID: <code>{u.id}</code>",
        f"Admin: <b>{'YES ✅' if is_admin(u.id) else 'NO ❌'}</b>",
    ]
    if DEBUG_EXPOSE_ADMIN_IDS:
        configured = ", ".join(str(x) for x in sorted(ADMIN_IDS))
        lines.append(f"File ADMIN_IDS: <code>{configured}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_cancel(update, context):
    clear_admin_draft(context)
    context.user_data.pop("captcha_answer", None)
    context.user_data.pop("captcha_uid", None)
    if update.effective_user and is_admin(update.effective_user.id):
        data = load_data()
        await update.message.reply_text(
            "Cancelled.\n" + admin_home_text(data),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_home_kb(data),
        )
    else:
        uid = update.effective_user.id if update.effective_user else None
        await update.message.reply_text("Cancelled.", reply_markup=main_menu_kb(uid))
    return ConversationHandler.END


async def cmd_admin(update, context) -> int:
    user = update.effective_user
    if not user or not is_admin(user.id):
        if update.message:
            await update.message.reply_text(
                admin_denied_text(user.id if user else None),
                parse_mode=ParseMode.HTML,
            )
        return ConversationHandler.END
    clear_admin_draft(context)
    data = load_data()
    if update.message:
        await update.message.reply_text(
            admin_home_text(data),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_home_kb(data),
        )
    return ConversationHandler.END


# =========================
# MAIN CALLBACK ROUTER
# =========================
async def on_callback(update, context):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass
    data = load_data()
    payload = query.data or ""
    user = update.effective_user
    uid = user.id if user else 0

    # ---- USER: HOME ----
    if payload == "menu:home":
        u = get_user(data, uid)
        if not u or not u.get("verified"):
            text = welcome_text(data) + "\n\nAfter joining, tap <b>✅ I've Joined All</b>"
            await safe_edit(query, context, text, join_channels_kb(data))
            return ConversationHandler.END
        await safe_edit(query, context, main_menu_text(data, u), main_menu_kb(uid))
        return ConversationHandler.END

    # ---- USER: VERIFY JOIN ----
    if payload == "verify:join":
        u = get_user(data, uid)
        if not u:
            await query.answer("Please /start first", show_alert=True)
            return ConversationHandler.END
        if u.get("verified"):
            await safe_edit(query, context, main_menu_text(data, u), main_menu_kb(uid))
            return ConversationHandler.END

        not_joined = await check_channel_membership(context, uid, data)
        if not_joined:
            names = ", ".join(f"@{c['username']}" for c in not_joined)
            await query.answer(f"❌ Not joined: {names}", show_alert=True)
            return ConversationHandler.END

        # ── ANTI-BOT: Math captcha ──
        question, answer = gen_captcha()
        context.user_data["captcha_answer"] = answer
        context.user_data["captcha_attempts"] = 0
        context.user_data["captcha_uid"] = uid
        text = (
            "🤖 <b>ANTI-BOT VERIFICATION</b>\n\n"
            "Solve this to prove you're human:\n\n"
            f"<b>{question} = ?</b>\n\n"
            "Type your answer below.\n/cancel to abort."
        )
        await safe_edit(query, context, text, back_home_kb())
        return WAIT_CAPTCHA

    # ---- USER: REFERRAL LINK ----
    if payload == "menu:reflink":
        u = get_user(data, uid)
        if not u or not u.get("verified"):
            await query.answer("Verify channels first", show_alert=True)
            return ConversationHandler.END
        bot_un = data["settings"].get("bot_username", "")
        link = referral_link(bot_un, uid) if bot_un else "(bot username not set)"
        text = (
            "🔗 <b>YOUR REFERRAL LINK</b>\n\n"
            f"<code>{html.escape(link)}</code>\n\n"
            "📤 Share this link with friends.\n"
            "Each friend who joins & verifies = +1 💎\n\n"
            f"Current referrals: <b>{u.get('referral_count', 0)}</b>\n"
            f"Current diamonds: <b>{u.get('diamonds', 0)}</b>"
        )
        await safe_edit(query, context, text, back_home_kb())
        return ConversationHandler.END

    # ---- USER: DIAMONDS ----
    if payload == "menu:diamonds":
        u = get_user(data, uid)
        if not u or not u.get("verified"):
            await query.answer("Verify channels first", show_alert=True)
            return ConversationHandler.END
        needed = data["settings"].get("diamonds_to_redeem", DIAMONDS_TO_REDEEM)
        diamonds = u.get("diamonds", 0)
        progress = "🔵" * diamonds + "⚪" * max(0, needed - diamonds)
        text = (
            f"💎 <b>MY DIAMONDS</b>\n\n"
            f"Diamonds: <b>{diamonds}/{needed}</b>\n"
            f"{progress}\n\n"
            f"👥 Total referrals: <b>{u.get('referral_count', 0)}</b>\n"
            f"🎁 Redeemed coupons: <b>{u.get('redeemed_count', 0)}</b>\n\n"
            + (f"✅ You can redeem a coupon!" if diamonds >= needed
               else f"🔒 Need <b>{needed - diamonds}</b> more 💎 to redeem")
        )
        await safe_edit(query, context, text, back_home_kb())
        return ConversationHandler.END

    # ---- USER: REDEEM ----
    if payload == "menu:redeem":
        redemption = None
        error_alert = None
        with data_session() as fresh:
            u = get_user(fresh, uid)
            if not u or not u.get("verified"):
                error_alert = "Verify channels first"
            else:
                needed = fresh["settings"].get("diamonds_to_redeem", DIAMONDS_TO_REDEEM)
                diamonds = u.get("diamonds", 0)
                if diamonds < needed:
                    error_alert = f"🔒 Need {needed} 💎. You have {diamonds}."
                else:
                    pool = fresh.get("coupon_pool", [])
                    if not pool:
                        error_alert = "🔴 Out of stock! Contact admin."
                    else:
                        code = pool.pop(0)
                        u["diamonds"] = diamonds - needed
                        u["redeemed_count"] = u.get("redeemed_count", 0) + 1
                        redemption = {
                            "user_id": uid,
                            "name": user.full_name,
                            "username": user.username,
                            "code": code,
                            "redeemed_at": now_iso(),
                            "diamonds_spent": needed,
                        }
                        u.setdefault("redemptions", []).append(redemption)
                        fresh["redemptions"].append(redemption)
            if not error_alert and redemption:
                snapshot_remaining = u["diamonds"]
                snapshot_pool_left = len(fresh["coupon_pool"])

        if error_alert:
            await query.answer(error_alert, show_alert=True)
            return ConversationHandler.END

        text = (
            f"🎁 <b>COUPON REDEEMED!</b>\n\n"
            f"💎 Spent: {redemption['diamonds_spent']} diamonds\n"
            f"💎 Remaining: <b>{snapshot_remaining}</b>\n\n"
            f"🎟 Your Blinkit Coupon Code:\n<code>{html.escape(redemption['code'])}</code>\n\n"
            "Use it on Blinkit. Happy shopping! 🛍"
        )
        await safe_edit(query, context, text, back_home_kb())

        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=aid,
                    text=(
                        f"🎁 <b>Redemption</b>\n"
                        f"User: {html.escape(user.full_name)} (@{html.escape(user.username or '-')}) "
                        f"<code>{uid}</code>\n"
                        f"Code: <code>{html.escape(redemption['code'])}</code>\n"
                        f"Diamonds spent: {redemption['diamonds_spent']}\n"
                        f"Pool left: <b>{snapshot_pool_left}</b>"
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🛠 Admin Panel", callback_data="adm:home")]
                    ]),
                )
            except Exception:
                pass
        return ConversationHandler.END

    # ---- USER: SUPPORT ----
    if payload == "menu:support":
        await safe_edit(query, context, support_text(data), back_home_kb())
        return ConversationHandler.END

    # ---- USER: MY REDEMPTIONS ----
    if payload == "menu:myredem":
        u = get_user(data, uid)
        if not u or not u.get("redemptions"):
            text = "📜 <b>MY REDEMPTIONS</b>\n\nNo coupons redeemed yet."
        else:
            lines = ["📜 <b>MY REDEMPTIONS</b>\n"]
            for r in sorted(u["redemptions"], key=lambda x: x.get("redeemed_at", ""), reverse=True)[:10]:
                lines.append(f"• <code>{html.escape(r['code'])}</code> — {r.get('redeemed_at', '-')[:10]}")
            text = "\n".join(lines)
        await safe_edit(query, context, text, back_home_kb())
        return ConversationHandler.END

    # ---- ADMIN ----
    if payload.startswith("adm:"):
        if not is_admin(uid):
            await query.answer("Admins only — /myid", show_alert=True)
            await safe_edit(query, context, admin_denied_text(uid), main_menu_kb(uid))
            return ConversationHandler.END
        return await admin_callback(update, context, data, payload)

    return ConversationHandler.END


# =========================
# CAPTCHA HANDLER
# =========================
async def handle_captcha(update, context):
    expected = context.user_data.get("captcha_answer")
    if expected is None:
        await update.message.reply_text("Session expired. /start")
        return ConversationHandler.END

    raw = (update.message.text or "").strip()
    try:
        user_answer = int(raw)
    except ValueError:
        await update.message.reply_text("Please type a number.")
        return WAIT_CAPTCHA

    uid = context.user_data.get("captcha_uid")
    if not uid:
        await update.message.reply_text("Session expired. /start")
        return ConversationHandler.END

    if user_answer == expected:
        context.user_data.pop("captcha_answer", None)
        context.user_data.pop("captcha_attempts", None)

        # ── If Mini App URL is set, redirect to device verification ──
        if MINI_APP_URL:
            user_missing = False
            with data_session() as data:
                u = get_user(data, uid)
                if not u:
                    user_missing = True
                else:
                    # Mark that this user has legitimately passed the captcha
                    # and is expected to open the Mini App next.
                    # /api/verify-device checks this flag (plus a TTL) before
                    # crediting anyone whose initData we couldn't
                    # cryptographically validate — this is what stops a
                    # random uid guess from hitting the endpoint directly.
                    u["captcha_passed"] = True
                    u["mini_app_pending"] = True
                    u["mini_app_pending_at"] = now_iso()

            if user_missing:
                await update.message.reply_text("Session expired. /start")
                return ConversationHandler.END

            mini_url = MINI_APP_URL.rstrip("/")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "📱 Verify Device",
                    web_app=WebAppInfo(url=f"{mini_url}?uid={uid}"),
                )],
                [InlineKeyboardButton("« Back", callback_data="menu:home")],
            ])
            await update.message.reply_text(
                "✅ <b>Captcha passed!</b>\n\n"
                "📱 Final step: tap below to verify your device.\n"
                "This prevents fake referrals from the same phone/PC.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
            return ConversationHandler.END

        # ── No Mini App — credit referrer directly after captcha ──
        already_verified = False
        user_missing = False
        ref_id = None
        ref_snapshot = None
        needed = DIAMONDS_TO_REDEEM
        reply_data = None
        reply_user = None

        with data_session() as data:
            u = get_user(data, uid)
            if not u:
                user_missing = True
            elif u.get("verified"):
                already_verified = True
            else:
                u["verified"] = True
                u["verified_at"] = now_iso()
                u["captcha_passed"] = True

                ref_id = u.get("referred_by")
                if ref_id and str(ref_id) != str(uid):
                    ref = data["users"].get(str(ref_id))
                    if ref:
                        ref["diamonds"] = ref.get("diamonds", 0) + 1
                        ref["referral_count"] = ref.get("referral_count", 0) + 1
                        needed = data["settings"].get("diamonds_to_redeem", DIAMONDS_TO_REDEEM)
                        ref_snapshot = json.loads(json.dumps(ref))
                    else:
                        ref_id = None

                reply_data = json.loads(json.dumps(data))
                reply_user = json.loads(json.dumps(u))

        if user_missing:
            await update.message.reply_text("Session expired. /start")
            return ConversationHandler.END
        if already_verified:
            await update.message.reply_text(
                "Already verified!", reply_markup=main_menu_kb(uid)
            )
            return ConversationHandler.END

        if ref_id and ref_snapshot:
            try:
                await context.bot.send_message(
                    chat_id=ref_id,
                    text=(
                        f"🎉 <b>New Referral!</b>\n\n"
                        f"{html.escape(reply_user.get('name') or 'Someone')} joined using your link.\n"
                        f"💎 +1 Diamond\n"
                        f"Total: <b>{ref_snapshot['diamonds']}/{needed}</b>"
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_menu_kb(ref_id),
                )
            except Exception as e:
                log.warning("Notify referrer %s failed: %s", ref_id, e)

        text = "✅ <b>Verification Complete!</b>\n\n" + main_menu_text(reply_data, reply_user)
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=main_menu_kb(uid)
        )
        return ConversationHandler.END

    else:
        attempts = context.user_data.get("captcha_attempts", 0) + 1
        context.user_data["captcha_attempts"] = attempts
        if attempts >= 3:
            context.user_data.pop("captcha_answer", None)
            context.user_data.pop("captcha_uid", None)
            await update.message.reply_text(
                "❌ Too many wrong answers. Use /start to try again."
            )
            return ConversationHandler.END
        question, new_answer = gen_captcha()
        context.user_data["captcha_answer"] = new_answer
        await update.message.reply_text(
            f"❌ Wrong! Attempt {attempts}/3.\n\n"
            f"Try again: <b>{question} = ?</b>",
            parse_mode=ParseMode.HTML,
        )
        return WAIT_CAPTCHA


# =========================
# ADMIN CALLBACKS
# =========================
async def admin_callback(update, context, data, payload):
    query = update.callback_query
    clear_admin_draft(context)

    if payload == "adm:home":
        await safe_edit(query, context, admin_home_text(data), admin_home_kb(data))
        return ConversationHandler.END

    if payload == "adm:addcodes":
        await safe_edit(
            query, context,
            (
                f"🎟 <b>ADD COUPON CODES</b>\n\n"
                f"Pool me abhi: <b>{len(data.get('coupon_pool', []))}</b> codes\n\n"
                "Saare Blinkit coupon codes <b>ek message</b> me bhejo.\n"
                "Har line pe 1 code, ya comma se separate.\n\n"
                "Example:\n"
                "<code>BLINKIT100\nBLINKIT200\nBLINKIT300</code>\n\n"
                "/cancel se band."
            ),
            cancel_admin_kb(),
        )
        return ADM_ADD_CODES

    if payload == "adm:viewcodes":
        codes = data.get("coupon_pool", [])
        if not codes:
            text = "📋 <b>CODES POOL</b>\n\nPool empty. Add codes karo."
        else:
            body = "\n".join(f"{i}. <code>{html.escape(c)}</code>" for i, c in enumerate(codes[:50], 1))
            extra = f"\n… +{len(codes)-50} more" if len(codes) > 50 else ""
            text = f"📋 <b>CODES POOL ({len(codes)})</b>\n\n{body}{extra}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧹 Clear All", callback_data="adm:clearcodes")],
            [InlineKeyboardButton("« Admin Home", callback_data="adm:home")],
        ])
        await safe_edit(query, context, text, kb)
        return ConversationHandler.END

    if payload == "adm:clearcodes":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 YES clear all", callback_data="adm:clearcodesyes"),
             InlineKeyboardButton("No", callback_data="adm:viewcodes")],
        ])
        await safe_edit(query, context,
            f"🧹 Delete all <b>{len(data.get('coupon_pool', []))}</b> codes?", kb)
        return ConversationHandler.END

    if payload == "adm:clearcodesyes":
        with data_session() as fresh:
            fresh["coupon_pool"] = []
            snapshot = json.loads(json.dumps(fresh))
        await safe_edit(query, context,
            "✅ Codes cleared.\n\n" + admin_home_text(snapshot), admin_home_kb(snapshot))
        return ConversationHandler.END

    if payload == "adm:users":
        users = data.get("users", {})
        if not users:
            text = "👥 <b>USERS</b>\n\nNo users yet."
        else:
            lines = ["👥 <b>USERS</b>\n"]
            sorted_users = sorted(users.values(),
                key=lambda x: x.get("diamonds", 0), reverse=True)
            for u in sorted_users[:30]:
                v = "✅" if u.get("verified") else "❌"
                lines.append(
                    f"{v} {html.escape(u.get('name', '?')[:20])} (@{html.escape(u.get('username') or '-')}) "
                    f"<code>{u.get('id')}</code>\n"
                    f"   💎{u.get('diamonds', 0)} | 🔗{u.get('referral_count', 0)} | "
                    f"🎁{u.get('redeemed_count', 0)}"
                )
            if len(users) > 30:
                lines.append(f"\n… +{len(users)-30} more")
            text = "\n".join(lines)
        await safe_edit(query, context, text,
            InlineKeyboardMarkup([[InlineKeyboardButton("« Admin Home", callback_data="adm:home")]]))
        return ConversationHandler.END

    if payload == "adm:redem":
        redems = data.get("redemptions", [])
        if not redems:
            text = "📜 <b>REDEMPTIONS</b>\n\nNo redemptions yet."
        else:
            lines = ["📜 <b>REDEMPTIONS</b>\n"]
            for r in sorted(redems, key=lambda x: x.get("redeemed_at", ""), reverse=True)[:20]:
                lines.append(
                    f"• {html.escape(r.get('name', '?')[:18])} (@{html.escape(r.get('username') or '-')}) "
                    f"<code>{r.get('user_id')}</code>\n"
                    f"  Code: <code>{html.escape(r.get('code', ''))}</code> | "
                    f"{r.get('redeemed_at', '-')[:10]}\n"
                    f"  Spent: {r.get('diamonds_spent', 0)} 💎"
                )
            text = "\n".join(lines)
        await safe_edit(query, context, text,
            InlineKeyboardMarkup([[InlineKeyboardButton("« Admin Home", callback_data="adm:home")]]))
        return ConversationHandler.END

    if payload == "adm:fraud":
        frauds = [u for u in data.get("users", {}).values() if u.get("fraud_detected")]
        if not frauds:
            text = "🚨 <b>FRAUD LOG</b>\n\nNo fraud detected yet. ✅"
        else:
            lines = ["🚨 <b>FRAUD LOG</b>\n"]
            for u in frauds[:20]:
                lines.append(
                    f"• {html.escape(u.get('name', '?'))} <code>{u.get('id')}</code>\n"
                    f"  Reason: {html.escape(u.get('fraud_reason', '?'))}\n"
                    f"  IP: <code>{html.escape(u.get('fraud_ip', '?'))}</code>\n"
                    f"  Matches: <code>{html.escape(str(u.get('fraud_existing_user', '?')))}</code>\n"
                )
            text = "\n".join(lines)
        await safe_edit(query, context, text,
            InlineKeyboardMarkup([[InlineKeyboardButton("« Admin Home", callback_data="adm:home")]]))
        return ConversationHandler.END

    if payload == "adm:stats":
        n_users = len(data.get("users", {}))
        verified = sum(1 for u in data["users"].values() if u.get("verified"))
        total_diamonds = sum(u.get("diamonds", 0) for u in data["users"].values())
        total_refs = sum(u.get("referral_count", 0) for u in data["users"].values())
        n_redem = len(data.get("redemptions", []))
        codes_left = len(data.get("coupon_pool", []))
        n_fraud = sum(1 for u in data.get("users", {}).values() if u.get("fraud_detected"))
        n_fps = len(data.get("device_fingerprints", {}))
        n_ips = len(data.get("seen_ips", {}))
        text = (
            "📊 <b>STATS</b>\n\n"
            f"👥 Total users: {n_users}\n"
            f"✅ Verified: {verified}\n"
            f"❌ Unverified: {n_users - verified}\n"
            f"🔗 Total referrals: {total_refs}\n"
            f"💎 Diamonds in circulation: {total_diamonds}\n"
            f"🎟 Codes in pool: {codes_left}\n"
            f"📜 Total redemptions: {n_redem}\n"
            f"🚨 Fraud blocked: {n_fraud}\n"
            f"📱 Devices tracked: {n_fps}\n"
            f"🌐 IPs tracked: {n_ips}\n"
        )
        await safe_edit(query, context, text,
            InlineKeyboardMarkup([[InlineKeyboardButton("« Admin Home", callback_data="adm:home")]]))
        return ConversationHandler.END

    if payload == "adm:settings":
        s = data["settings"]
        ch_list = "\n".join(f"• @{html.escape(c['username'])} ({html.escape(c['name'])})" for c in s["channels"])
        text = (
            "⚙️ <b>SETTINGS</b>\n\n"
            f"🏪 Store: <b>{html.escape(s['store_name'])}</b>\n"
            f"🎧 Support: @{html.escape(s['support_username'])}\n"
            f"💎 Redeem cost: {s.get('diamonds_to_redeem', DIAMONDS_TO_REDEEM)} 💎\n"
            f"🤖 Bot: @{html.escape(s.get('bot_username') or '?')}\n\n"
            f"📡 Channels:\n{ch_list}"
        )
        await safe_edit(query, context, text, admin_settings_kb())
        return ConversationHandler.END

    if payload == "adm:channels":
        await safe_edit(query, context,
            "📡 <b>CHANNELS</b>\n\nTap to remove, or add new:",
            admin_channels_kb(data))
        return ConversationHandler.END

    if payload == "adm:addch":
        await safe_edit(query, context,
            "➕ <b>ADD CHANNEL</b>\n\n"
            "Step 1/2 — Channel <b>username</b> bhejo (bina @):\n"
            "Example: <code>mychannel</code>",
            cancel_admin_kb())
        return ADM_ADD_CH_UN

    if payload.startswith("adm:delch:"):
        target_username = payload.split(":", 2)[2]
        removed_username = None
        blocked = False
        with data_session() as fresh:
            channels = fresh["settings"]["channels"]
            if len(channels) <= 1:
                blocked = True
            else:
                for i, ch in enumerate(channels):
                    if ch["username"] == target_username:
                        removed_username = channels.pop(i)["username"]
                        break
            snapshot = json.loads(json.dumps(fresh))
        if blocked:
            await query.answer(
                "⚠️ Can't remove the last channel — at least one is required "
                "so users have something to verify against.",
                show_alert=True,
            )
            return ConversationHandler.END
        if removed_username:
            await safe_edit(query, context,
                f"✅ Removed @{html.escape(removed_username)}\n\n📡 <b>CHANNELS</b>",
                admin_channels_kb(snapshot))
        else:
            await query.answer("Already removed.", show_alert=True)
        return ConversationHandler.END

    if payload == "adm:setsupport":
        await safe_edit(query, context,
            "🎧 Support username bhejo <b>bina @</b>:\nExample: <code>my_support</code>",
            cancel_admin_kb())
        return ADM_SET_SUPPORT

    if payload == "adm:setstore":
        await safe_edit(query, context,
            "🏪 Store name bhejo:\nExample: <code>BLANK STORE</code>",
            cancel_admin_kb())
        return ADM_SET_STORE

    if payload == "adm:broadcast":
        await safe_edit(query, context,
            "📢 Broadcast message likho (saare verified users ko jayega):\n\n/cancel se band.",
            cancel_admin_kb())
        return ADM_BROADCAST

    return ConversationHandler.END


# =========================
# ADMIN TEXT CONVERSATIONS
# =========================
async def adm_add_codes(update, context):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    raw = update.message.text or ""
    codes = parse_codes_blob(raw)
    if not codes:
        await update.message.reply_text("Koi code nahi mila. Dobara bhejo.")
        return ADM_ADD_CODES

    with data_session() as data:
        if not isinstance(data.get("coupon_pool"), list):
            data["coupon_pool"] = []
        existing = set(data["coupon_pool"])
        added, skipped = 0, 0
        for c in codes:
            if c in existing:
                skipped += 1
                continue
            existing.add(c)
            data["coupon_pool"].append(c)
            added += 1
        pool_total = len(data["coupon_pool"])
        snapshot = json.loads(json.dumps(data))

    clear_admin_draft(context)
    await update.message.reply_text(
        f"✅ <b>Codes added</b>\nNew: <b>{added}</b> | Duplicate skip: <b>{skipped}</b>\n"
        f"Pool total: <b>{pool_total}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_home_kb(snapshot),
    )
    return ConversationHandler.END


async def adm_set_support(update, context):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    uname = (update.message.text or "").strip().lstrip("@")
    if len(uname) < 3:
        await update.message.reply_text("Valid username bhejo")
        return ADM_SET_SUPPORT
    with data_session() as data:
        data["settings"]["support_username"] = uname
        snapshot = json.loads(json.dumps(data))
    clear_admin_draft(context)
    await update.message.reply_text(f"✅ Support: @{html.escape(uname)}", parse_mode=ParseMode.HTML, reply_markup=admin_home_kb(snapshot))
    return ConversationHandler.END


async def adm_set_store(update, context):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    name = (update.message.text or "").strip()
    if len(name) < 2:
        await update.message.reply_text("Name bhejo")
        return ADM_SET_STORE
    with data_session() as data:
        data["settings"]["store_name"] = name
        snapshot = json.loads(json.dumps(data))
    clear_admin_draft(context)
    await update.message.reply_text(
        f"✅ Store name: <b>{html.escape(name)}</b>", parse_mode=ParseMode.HTML,
        reply_markup=admin_home_kb(snapshot))
    return ConversationHandler.END


async def adm_add_ch_un(update, context):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    uname = (update.message.text or "").strip().lstrip("@")
    if len(uname) < 3:
        await update.message.reply_text("Valid username bhejo")
        return ADM_ADD_CH_UN
    context.user_data["adm_new_ch_un"] = uname
    await update.message.reply_text(
        f"Channel: <b>@{html.escape(uname)}</b>\n\n"
        "Step 2/2 — Display <b>name</b> bhejo:\n"
        "Example: <code>MY CHANNEL</code>",
        parse_mode=ParseMode.HTML, reply_markup=cancel_admin_kb())
    return ADM_ADD_CH_NAME


async def adm_add_ch_name(update, context):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    uname = context.user_data.get("adm_new_ch_un")
    name = (update.message.text or "").strip()
    if not uname or not name:
        await update.message.reply_text("Session expired. /admin")
        return ConversationHandler.END
    with data_session() as data:
        data["settings"]["channels"].append({"username": uname, "name": name})
        snapshot = json.loads(json.dumps(data))
    clear_admin_draft(context)
    await update.message.reply_text(
        f"✅ Channel added: @{html.escape(uname)} ({html.escape(name)})", parse_mode=ParseMode.HTML,
        reply_markup=admin_channels_kb(snapshot))
    return ConversationHandler.END


async def adm_broadcast(update, context):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Message khali hai.")
        return ADM_BROADCAST
    data = load_data()
    ok = fail = 0
    for uid_str, u in data.get("users", {}).items():
        if not u.get("verified"):
            continue
        try:
            await context.bot.send_message(chat_id=int(uid_str), text=text)
            ok += 1
        except Exception:
            fail += 1
    clear_admin_draft(context)
    await update.message.reply_text(
        f"📢 Broadcast done. ok={ok} fail={fail}", reply_markup=admin_home_kb(data))
    return ConversationHandler.END


async def fallback_text(update, context):
    uid = update.effective_user.id if update.effective_user else None
    if is_admin(uid):
        data = load_data()
        await update.message.reply_text(
            "Admin: /admin | Store: /start", reply_markup=admin_home_kb(data))
    else:
        await update.message.reply_text("Use /start to open the menu.")


# =========================
# FLASK WEBHOOK SERVER (device + IP verification)
# =========================
VERIFY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Device Verification</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;background:#0f0f0f;color:#fff}
.card{text-align:center;padding:2rem;max-width:400px;width:90%}
.spinner{width:48px;height:48px;border:4px solid #333;border-top:4px solid #00e676;border-radius:50%;animation:spin 1s linear infinite;margin:1rem auto}
@keyframes spin{100%{transform:rotate(360deg)}}
.ok{color:#00e676;font-size:3rem}
.bad{color:#ff5252;font-size:3rem}
.msg{margin-top:1rem;font-size:1.1rem;line-height:1.5}
.sub{margin-top:0.5rem;color:#888;font-size:.9rem}
</style>
</head>
<body>
<div class="card">
<div id="icon"><div class="spinner"></div></div>
<div class="msg" id="status">Checking your device...</div>
<div class="sub" id="sub"></div>
</div>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script type="module">
import FingerprintJS from 'https://cdn.jsdelivr.net/npm/@fingerprintjs/fingerprintjs@4/dist/fp.min.js';
async function run(){
  const tg=window.Telegram?.WebApp;tg?.ready();tg?.expand();
  const s=document.getElementById('status'),sub=document.getElementById('sub'),ic=document.getElementById('icon');
  try{
    const fp=await FingerprintJS.load();const r=await fp.get();const fingerprint=r.visitorId;
    const userId=tg?.initDataUnsafe?.user?.id||new URLSearchParams(location.search).get('uid');
    if(!userId){ic.innerHTML='<div class="bad">⚠️</div>';s.textContent='Could not identify your Telegram account.';return;}
    const resp=await fetch('/api/verify-device',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fingerprint:fingerprint,user_id:userId,init_data:tg?.initData||''})});
    const data=await resp.json();
    if(data.ok){ic.innerHTML='<div class="ok">✅</div>';s.textContent='Device verified! You can close this page.';sub.textContent='Your referral reward has been credited.';}
    else{ic.innerHTML='<div class="bad">🚫</div>';s.textContent=data.error||'Verification failed.';sub.textContent='This device has already been used for a referral.';}
    if(tg)setTimeout(()=>tg.close(),3000);
  }catch(e){ic.innerHTML='<div class="bad">⚠️</div>';s.textContent='Error: '+e.message;}
}
run();
</script>
</body>
</html>"""


def _tg_send(chat_id, text):
    """Send a message via raw Bot API (used by Flask webhook thread)."""
    try:
        http_requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


flask_app = Flask(__name__)


@flask_app.route("/")
def serve_verify_page():
    return VERIFY_HTML, 200, {"Content-Type": "text/html"}


@flask_app.route("/health")
def health():
    return jsonify({"status": "ok"})


@flask_app.route("/api/verify-device", methods=["POST"])
def verify_device():
    body = request.get_json(force=True, silent=True) or {}
    fingerprint = (body.get("fingerprint") or "").strip()
    raw_init_data = body.get("init_data") or ""
    claimed_uid = body.get("user_id")

    if not fingerprint:
        return jsonify({"ok": False, "error": "Missing data"}), 400

    # ── Step 1: figure out WHO is actually making this request ──
    # Never trust `claimed_uid` on its own — it's a value the browser sent us
    # and anyone can fake it. Prefer the cryptographically-signed Telegram
    # WebApp initData; fall back to the claimed uid ONLY if that account has
    # an active, non-expired "mini_app_pending" flag set by the bot itself
    # (i.e. they genuinely just passed the captcha in chat).
    verified_uid = None
    parsed = validate_init_data(raw_init_data, BOT_TOKEN)
    if parsed:
        try:
            user_json = json.loads(parsed.get("user", "{}"))
            verified_uid = user_json.get("id")
        except (ValueError, TypeError):
            verified_uid = None

    # Everything below — the duplicate-device/IP check, the credit, and the
    # save — happens inside ONE data_session() so a second concurrent
    # verify-device call (e.g. two referred friends finishing at the same
    # second) can't read the same pre-write snapshot and clobber this one.
    result = {"ok": False, "error": "Missing data"}
    status_code = 400
    notify_admin_fraud = None
    notify_user_fraud = False
    notify_admin_ip_flag = None
    notify_success = None
    notify_admin_success = None
    notify_ref = None

    with data_session() as data:
        data.setdefault("device_fingerprints", {})
        data.setdefault("seen_ips", {})

        if verified_uid is not None:
            user_id = verified_uid
        elif claimed_uid is not None:
            candidate = data.get("users", {}).get(str(claimed_uid))
            pending_at = candidate.get("mini_app_pending_at") if candidate else None
            is_pending = bool(candidate and candidate.get("mini_app_pending") and pending_at)
            if is_pending:
                try:
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(pending_at)).total_seconds()
                except ValueError:
                    age = MINI_APP_PENDING_TTL + 1
                is_pending = age <= MINI_APP_PENDING_TTL
            if not is_pending:
                log.warning("Rejected unauthenticated verify-device call for uid=%s", claimed_uid)
                result = {"ok": False, "error": "Unauthorized or expired — reopen the link from the bot."}
                status_code = 403
                user_id = None
            else:
                user_id = claimed_uid
        else:
            user_id = None

        if user_id is not None:
            # Capture IP (works behind Railway's proxy)
            ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            if not ip:
                ip = request.headers.get("X-Real-IP", "").strip()
            if not ip:
                ip = request.remote_addr or "unknown"

            uid_str = str(user_id)
            user = data.get("users", {}).get(uid_str)
            if not user:
                result = {"ok": False, "error": "User not found. /start the bot first."}
                status_code = 200
            elif user.get("verified"):
                result = {"ok": True, "error": None}
                status_code = 200
            elif not user.get("captcha_passed"):
                result = {"ok": False, "error": "Complete the captcha in the bot first."}
                status_code = 403
            else:
                # ── Check for duplicate device / IP ──
                # Only a matching DEVICE FINGERPRINT auto-blocks — that's the
                # strong signal. A matching IP alone is common and legitimate
                # on shared networks / mobile carrier CGNAT (very common in
                # India), so we only log + flag it for admin review instead
                # of hard-blocking on IP alone.
                dup_device = data["device_fingerprints"].get(fingerprint)
                dup_ip = data["seen_ips"].get(ip)

                if dup_device:
                    user["fraud_detected"] = True
                    user["fraud_reason"] = "duplicate_device"
                    user["fraud_fingerprint"] = fingerprint
                    user["fraud_ip"] = ip
                    user["fraud_existing_user"] = dup_device
                    user.pop("mini_app_pending", None)

                    notify_admin_fraud = {
                        "name": user.get("name", "?"), "uid": uid_str,
                        "fingerprint": fingerprint, "ip": ip, "dup_device": dup_device,
                    }
                    notify_user_fraud = user_id
                    result = {"ok": False, "error": "Duplicate device detected. Fake referral blocked."}
                    status_code = 200
                else:
                    if dup_ip and dup_ip != uid_str:
                        user["fraud_ip_flag"] = True
                        user["fraud_ip"] = ip
                        user["fraud_ip_matches"] = dup_ip
                        notify_admin_ip_flag = {"name": user.get("name", "?"), "uid": uid_str, "ip": ip, "dup_ip": dup_ip}

                    # ── New device — verify user + credit referrer ──
                    data["device_fingerprints"][fingerprint] = uid_str
                    data["seen_ips"][ip] = uid_str
                    user["verified"] = True
                    user["verified_at"] = now_iso()
                    user["device_fingerprint"] = fingerprint
                    user["ip_address"] = ip
                    user.pop("mini_app_pending", None)
                    user.pop("mini_app_pending_at", None)

                    ref_id = user.get("referred_by")
                    if ref_id and str(ref_id) != uid_str:
                        ref = data["users"].get(str(ref_id))
                        if ref:
                            ref["diamonds"] = ref.get("diamonds", 0) + 1
                            ref["referral_count"] = ref.get("referral_count", 0) + 1
                            needed = data.get("settings", {}).get("diamonds_to_redeem", DIAMONDS_TO_REDEEM)
                            notify_ref = {"ref_id": ref_id, "name": user.get("name", "Someone"),
                                          "diamonds": ref["diamonds"], "needed": needed}

                    result = {"ok": True, "error": None}
                    status_code = 200
                    notify_success = user_id
                    notify_admin_success = {
                        "name": user.get("name", "?"), "uid": uid_str, "ip": ip,
                        "fingerprint": fingerprint, "ref_id": ref_id,
                    }

    # Notifications happen AFTER the lock is released — sending Telegram
    # messages is slow I/O and shouldn't be done while holding the file lock.
    if notify_admin_fraud:
        n = notify_admin_fraud
        for aid in ADMIN_IDS:
            _tg_send(aid, (
                f"🚨 <b>FRAUD DETECTED</b>\n\n"
                f"User: {html.escape(n['name'])} <code>{n['uid']}</code>\n"
                f"Fingerprint: <code>{html.escape(n['fingerprint'][:16])}…</code>\n"
                f"IP: <code>{html.escape(n['ip'])}</code>\n"
                f"Matches existing user: <code>{html.escape(str(n['dup_device']))}</code>\n"
                f"Reason: same device"
            ))
    if notify_user_fraud:
        _tg_send(notify_user_fraud, (
            "🚫 <b>Verification Failed</b>\n\n"
            "This device has already been used for a referral.\n"
            "Fake referrals are not allowed."
        ))
    if notify_admin_ip_flag:
        n = notify_admin_ip_flag
        for aid in ADMIN_IDS:
            _tg_send(aid, (
                f"ℹ️ <b>Same IP as another user (not auto-blocked)</b>\n\n"
                f"User: {html.escape(n['name'])} <code>{n['uid']}</code>\n"
                f"IP: <code>{html.escape(n['ip'])}</code>\n"
                f"Also used by: <code>{html.escape(str(n['dup_ip']))}</code>"
            ))
    if notify_ref:
        n = notify_ref
        _tg_send(n["ref_id"], (
            f"🎉 <b>New Referral!</b>\n\n"
            f"{html.escape(n['name'])} joined using your link.\n"
            f"💎 +1 Diamond\n"
            f"Total: <b>{n['diamonds']}/{n['needed']}</b>"
        ))
    if notify_success:
        _tg_send(notify_success, (
            "✅ <b>Verification Complete!</b>\n\n"
            "Your device has been verified. You can now use the bot.\n"
            "Share your referral link to earn more diamonds! 💎"
        ))
    if notify_admin_success:
        n = notify_admin_success
        for aid in ADMIN_IDS:
            _tg_send(aid, (
                f"✅ <b>New verified user</b>\n"
                f"User: {html.escape(n['name'])} <code>{n['uid']}</code>\n"
                f"IP: <code>{html.escape(n['ip'])}</code>\n"
                f"Fingerprint: <code>{html.escape(n['fingerprint'][:16])}…</code>\n"
                f"Referred by: <code>{n['ref_id'] or 'none'}</code>"
            ))

    return jsonify(result), status_code


# =========================
# APP BUILD
# =========================
async def post_init(app):
    me = await app.bot.get_me()
    with data_session() as data:
        data["settings"]["bot_username"] = me.username or ""
    log.info("Bot username: @%s", me.username)
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Open menu / referral"),
            BotCommand("myid", "Show my Telegram ID"),
            BotCommand("admin", "Admin panel"),
            BotCommand("cancel", "Cancel"),
        ])
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)


def build_app() -> Application:
    token = (os.getenv("BOT_TOKEN") or BOT_TOKEN or "").strip()
    if not token or ":" not in token:
        raise SystemExit(
            "★ BOT_TOKEN set nahi hai (or looks invalid). "
            "Set it in Railway env vars — get a fresh one from @BotFather if "
            "the old one was ever committed to source control."
        )

    print(f"Admin IDs: {sorted(ADMIN_IDS) or '(none set!)'}")
    print(f"Store: {STORE_NAME}")
    print(f"Channels: {[c['username'] for c in CHANNELS]}")
    print(f"Diamonds to redeem: {DIAMONDS_TO_REDEEM}")
    print(f"Mini App URL: {MINI_APP_URL or '(captcha only)'}")
    print(f"Port: {PORT}")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("myid", cmd_myid), group=0)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("admin", cmd_admin),
            CallbackQueryHandler(on_callback),
        ],
        states={
            WAIT_CAPTCHA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_captcha),
                CommandHandler("cancel", cmd_cancel),
                CallbackQueryHandler(on_callback),
            ],
            ADM_ADD_CODES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_codes),
                CommandHandler("cancel", cmd_cancel),
                CallbackQueryHandler(on_callback),
            ],
            ADM_SET_SUPPORT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_set_support),
                CommandHandler("cancel", cmd_cancel),
                CallbackQueryHandler(on_callback),
            ],
            ADM_SET_STORE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_set_store),
                CommandHandler("cancel", cmd_cancel),
                CallbackQueryHandler(on_callback),
            ],
            ADM_ADD_CH_UN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_ch_un),
                CommandHandler("cancel", cmd_cancel),
                CallbackQueryHandler(on_callback),
            ],
            ADM_ADD_CH_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_ch_name),
                CommandHandler("cancel", cmd_cancel),
                CallbackQueryHandler(on_callback),
            ],
            ADM_BROADCAST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_broadcast),
                CommandHandler("cancel", cmd_cancel),
                CallbackQueryHandler(on_callback),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start", cmd_start),
            CommandHandler("admin", cmd_admin),
            MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text),
        ],
        allow_reentry=True,
        per_message=False,
        name="refer_conv",
        persistent=False,
    )
    app.add_handler(conv, group=1)
    app.post_init = post_init
    return app


def run_flask():
    """Run Flask in a background thread (for Railway health check + webhook)."""
    flask_app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


def main():
    # Start Flask in background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    log.info("Flask server started on port %d", PORT)

    # Start PTB polling in main thread
    app = build_app()
    log.info("Referral bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
