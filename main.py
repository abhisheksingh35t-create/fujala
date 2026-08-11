#!/usr/bin/env python3
"""
BLANK STORE — Referral Reward Bot with Anti-Fraud (Railway Ready)
------------------------------------------------------------------
  /start ref_<id> -> Join channels -> Math captcha -> Device verify
  1 referral = 1 💎 | 4 💎 = 1 Blinkit coupon 🎟

Anti-fraud layers:
  1. Math captcha (blocks bots)
  2. IP tracing (blocks same-network multi-accounting)
  3. FingerprintJS device fingerprinting (blocks same-device multi-accounting)

Deploy on Railway:
  1. Push these 3 files to a GitHub repo
  2. railway up  (or connect repo in Railway dashboard)
  3. Set env vars: BOT_TOKEN, ADMIN_IDS, MINI_APP_URL
  4. Bot auto-starts
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
BOT_TOKEN = os.getenv("BOT_TOKEN", "8902001047:AAFHTTsGNJ3ILC927wBGZgfTGcaJiVP7UZM")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "6894923643 1446058092")
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
# DATA I/O (thread-safe)
# =========================
def load_data() -> dict[str, Any]:
    with _file_lock:
        if not DATA_FILE.exists():
            save_data_unlocked(DEFAULT_DATA)
            return json.loads(json.dumps(DEFAULT_DATA))
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


def save_data(data: dict[str, Any]) -> None:
    with _file_lock:
        save_data_unlocked(data)


def save_data_unlocked(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


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
    configured = ", ".join(str(x) for x in sorted(ADMIN_IDS)) or "(empty)"
    return (
        "⛔ <b>Admins only</b>\n\n"
        f"Tumhara ID: <code>{uid}</code>\n"
        f"File ADMIN_IDS: <code>{configured}</code>\n\n"
        "1) /myid bhejo\n"
        "2) Railway env vars me ADMIN_IDS set karo\n"
        "3) Redeploy karo"
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
    for i, ch in enumerate(data["settings"]["channels"]):
        rows.append([
            InlineKeyboardButton(
                f"❌ @{ch['username']} ({ch['name']})",
                callback_data=f"adm:delch:{i}",
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
        f"👋 <b>WELCOME TO {s['store_name']}</b>\n\n"
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
        f"🏠 <b>{s['store_name']}</b>\n\n"
        f"💎 Your Diamonds: <b>{diamonds}/{needed}</b>\n"
        f"{progress}\n"
        f"👥 Total Referrals: <b>{u.get('referral_count', 0)}</b>\n\n"
        f"🔗 Your Referral Link:\n<code>{link}</code>\n\n"
        f"Share this link! Each friend who joins = 1 💎\n"
        f"{needed} 💎 = 1 Blinkit Coupon 🎟"
    )


def support_text(data: dict) -> str:
    s = data["settings"]
    return (
        "🎧 <b>SUPPORT</b>\n\n"
        f"Need help? Contact: @{s['support_username']}\n\n"
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
    ch_list = ", ".join(f"@{c['username']}" for c in s["channels"])
    dev_status = "✅ ON" if MINI_APP_URL else "❌ OFF (captcha only)"
    return (
        "🛠 <b>ADMIN PANEL</b>\n\n"
        f"🏪 Store: <b>{s['store_name']}</b>\n"
        f"🎧 Support: @{s['support_username']}\n"
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
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup,
            )
    except Exception:
        if query.message:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup,
            )


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


def credit_referrer(data, user_id, user_name, context_bot=None):
    """Credit the referrer with +1 diamond. Returns True if credited."""
    u = get_user(data, user_id)
    if not u:
        return False
    ref_id = u.get("referred_by")
    if not ref_id or str(ref_id) == str(user_id):
        return False
    ref = data["users"].get(str(ref_id))
    if not ref:
        return False
    ref["diamonds"] = ref.get("diamonds", 0) + 1
    ref["referral_count"] = ref.get("referral_count", 0) + 1
    return True


# =========================
# USER COMMAND HANDLERS
# =========================
async def cmd_start(update, context):
    user = update.effective_user
    if not user:
        return ConversationHandler.END
    data = load_data()

    if not data["settings"].get("bot_username"):
        try:
            me = await context.bot.get_me()
            data["settings"]["bot_username"] = me.username or ""
            save_data(data)
        except Exception:
            pass

    ref_id = None
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                ref_id = int(arg[4:])
            except ValueError:
                pass

    uid_str = str(user.id)
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

    save_data(data)
    u = data["users"][uid_str]

    if not u.get("verified"):
        text = welcome_text(data) + "\n\nAfter joining, tap <b>✅ I've Joined All</b>"
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=join_channels_kb(data)
        )
        return ConversationHandler.END

    text = main_menu_text(data, u)
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
    configured = ", ".join(str(x) for x in sorted(ADMIN_IDS))
    await update.message.reply_text(
        f"Your Telegram ID: <code>{u.id}</code>\n"
        f"Admin: <b>{'YES ✅' if is_admin(u.id) else 'NO ❌'}</b>\n"
        f"File ADMIN_IDS: <code>{configured}</code>",
        parse_mode=ParseMode.HTML,
    )


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
            f"<code>{link}</code>\n\n"
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
        u = get_user(data, uid)
        if not u or not u.get("verified"):
            await query.answer("Verify channels first", show_alert=True)
            return ConversationHandler.END
        needed = data["settings"].get("diamonds_to_redeem", DIAMONDS_TO_REDEEM)
        diamonds = u.get("diamonds", 0)

        if diamonds < needed:
            await query.answer(
                f"🔒 Need {needed} 💎. You have {diamonds}.", show_alert=True
            )
            return ConversationHandler.END

        pool = data.get("coupon_pool", [])
        if not pool:
            await query.answer("🔴 Out of stock! Contact admin.", show_alert=True)
            return ConversationHandler.END

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
        data["redemptions"].append(redemption)
        save_data(data)

        text = (
            f"🎁 <b>COUPON REDEEMED!</b>\n\n"
            f"💎 Spent: {needed} diamonds\n"
            f"💎 Remaining: <b>{u['diamonds']}</b>\n\n"
            f"🎟 Your Blinkit Coupon Code:\n<code>{code}</code>\n\n"
            "Use it on Blinkit. Happy shopping! 🛍"
        )
        await safe_edit(query, context, text, back_home_kb())

        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=aid,
                    text=(
                        f"🎁 <b>Redemption</b>\n"
                        f"User: {user.full_name} (@{user.username}) <code>{uid}</code>\n"
                        f"Code: <code>{code}</code>\n"
                        f"Diamonds spent: {needed}\n"
                        f"Pool left: <b>{len(data['coupon_pool'])}</b>"
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
                lines.append(f"• <code>{r['code']}</code> — {r.get('redeemed_at', '-')[:10]}")
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
        data = load_data()
        u = get_user(data, uid)
        if not u:
            await update.message.reply_text("Session expired. /start")
            return ConversationHandler.END
        if u.get("verified"):
            await update.message.reply_text(
                "Already verified!", reply_markup=main_menu_kb(uid)
            )
            return ConversationHandler.END

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
                try:
                    await context.bot.send_message(
                        chat_id=ref_id,
                        text=(
                            f"🎉 <b>New Referral!</b>\n\n"
                            f"{u.get('name', 'Someone')} joined using your link.\n"
                            f"💎 +1 Diamond\n"
                            f"Total: <b>{ref['diamonds']}/{needed}</b>"
                        ),
                        parse_mode=ParseMode.HTML,
                        reply_markup=main_menu_kb(ref_id),
                    )
                except Exception as e:
                    log.warning("Notify referrer %s failed: %s", ref_id, e)

        save_data(data)
        text = "✅ <b>Verification Complete!</b>\n\n" + main_menu_text(data, u)
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
            body = "\n".join(f"{i}. <code>{c}</code>" for i, c in enumerate(codes[:50], 1))
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
        data["coupon_pool"] = []
        save_data(data)
        await safe_edit(query, context,
            "✅ Codes cleared.\n\n" + admin_home_text(data), admin_home_kb(data))
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
                    f"{v} {u.get('name', '?')[:20]} (@{u.get('username', '-')}) "
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
                    f"• {r.get('name', '?')[:18]} (@{r.get('username', '-')}) "
                    f"<code>{r.get('user_id')}</code>\n"
                    f"  Code: <code>{r.get('code')}</code> | "
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
                    f"• {u.get('name', '?')} <code>{u.get('id')}</code>\n"
                    f"  Reason: {u.get('fraud_reason', '?')}\n"
                    f"  IP: <code>{u.get('fraud_ip', '?')}</code>\n"
                    f"  Matches: <code>{u.get('fraud_existing_user', '?')}</code>\n"
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
        ch_list = "\n".join(f"• @{c['username']} ({c['name']})" for c in s["channels"])
        text = (
            "⚙️ <b>SETTINGS</b>\n\n"
            f"🏪 Store: <b>{s['store_name']}</b>\n"
            f"🎧 Support: @{s['support_username']}\n"
            f"💎 Redeem cost: {s.get('diamonds_to_redeem', DIAMONDS_TO_REDEEM)} 💎\n"
            f"🤖 Bot: @{s.get('bot_username', '?')}\n\n"
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
        idx = int(payload.split(":")[2])
        if 0 <= idx < len(data["settings"]["channels"]):
            removed = data["settings"]["channels"].pop(idx)
            save_data(data)
            await safe_edit(query, context,
                f"✅ Removed @{removed['username']}\n\n📡 <b>CHANNELS</b>",
                admin_channels_kb(data))
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
    data = load_data()
    existing = set(data.get("coupon_pool", []))
    added, skipped = 0, 0
    if not isinstance(data.get("coupon_pool"), list):
        data["coupon_pool"] = []
    for c in codes:
        if c in existing:
            skipped += 1
            continue
        existing.add(c)
        data["coupon_pool"].append(c)
        added += 1
    save_data(data)
    clear_admin_draft(context)
    await update.message.reply_text(
        f"✅ <b>Codes added</b>\nNew: <b>{added}</b> | Duplicate skip: <b>{skipped}</b>\n"
        f"Pool total: <b>{len(data['coupon_pool'])}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_home_kb(data),
    )
    return ConversationHandler.END


async def adm_set_support(update, context):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    uname = (update.message.text or "").strip().lstrip("@")
    if len(uname) < 3:
        await update.message.reply_text("Valid username bhejo")
        return ADM_SET_SUPPORT
    data = load_data()
    data["settings"]["support_username"] = uname
    save_data(data)
    clear_admin_draft(context)
    await update.message.reply_text(f"✅ Support: @{uname}", reply_markup=admin_home_kb(data))
    return ConversationHandler.END


async def adm_set_store(update, context):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    name = (update.message.text or "").strip()
    if len(name) < 2:
        await update.message.reply_text("Name bhejo")
        return ADM_SET_STORE
    data = load_data()
    data["settings"]["store_name"] = name
    save_data(data)
    clear_admin_draft(context)
    await update.message.reply_text(
        f"✅ Store name: <b>{name}</b>", parse_mode=ParseMode.HTML,
        reply_markup=admin_home_kb(data))
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
        f"Channel: <b>@{uname}</b>\n\n"
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
    data = load_data()
    data["settings"]["channels"].append({"username": uname, "name": name})
    save_data(data)
    clear_admin_draft(context)
    await update.message.reply_text(
        f"✅ Channel added: @{uname} ({name})", parse_mode=ParseMode.HTML,
        reply_markup=admin_channels_kb(data))
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
    user_id = body.get("user_id")
    if not fingerprint or not user_id:
        return jsonify({"ok": False, "error": "Missing data"})

    # Capture IP (works behind Railway's proxy)
    ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if not ip:
        ip = request.headers.get("X-Real-IP", "").strip()
    if not ip:
        ip = request.remote_addr or "unknown"

    data = load_data()
    data.setdefault("device_fingerprints", {})
    data.setdefault("seen_ips", {})

    uid_str = str(user_id)
    user = data.get("users", {}).get(uid_str)
    if not user:
        return jsonify({"ok": False, "error": "User not found. /start the bot first."})
    if user.get("verified"):
        return jsonify({"ok": True, "error": None})

    # ── Check for duplicate device or IP ──
    dup_device = data["device_fingerprints"].get(fingerprint)
    dup_ip = data["seen_ips"].get(ip)

    if dup_device or dup_ip:
        user["fraud_detected"] = True
        user["fraud_reason"] = "duplicate_device" if dup_device else "duplicate_ip"
        user["fraud_fingerprint"] = fingerprint
        user["fraud_ip"] = ip
        user["fraud_existing_user"] = dup_device or dup_ip
        save_data(data)

        for aid in ADMIN_IDS:
            _tg_send(aid, (
                f"🚨 <b>FRAUD DETECTED</b>\n\n"
                f"User: {user.get('name', '?')} <code>{uid_str}</code>\n"
                f"Fingerprint: <code>{fingerprint[:16]}…</code>\n"
                f"IP: <code>{ip}</code>\n"
                f"Matches existing user: <code>{dup_device or dup_ip}</code>\n"
                f"Reason: {'same device' if dup_device else 'same IP'}"
            ))
        _tg_send(user_id, (
            "🚫 <b>Verification Failed</b>\n\n"
            "This device has already been used for a referral.\n"
            "Fake referrals are not allowed."
        ))
        return jsonify({"ok": False, "error": "Duplicate device detected. Fake referral blocked."})

    # ── New device — verify user + credit referrer ──
    data["device_fingerprints"][fingerprint] = uid_str
    data["seen_ips"][ip] = uid_str
    user["verified"] = True
    user["verified_at"] = now_iso()
    user["captcha_passed"] = True
    user["device_fingerprint"] = fingerprint
    user["ip_address"] = ip

    ref_id = user.get("referred_by")
    if ref_id and str(ref_id) != uid_str:
        ref = data["users"].get(str(ref_id))
        if ref:
            ref["diamonds"] = ref.get("diamonds", 0) + 1
            ref["referral_count"] = ref.get("referral_count", 0) + 1
            needed = data.get("settings", {}).get("diamonds_to_redeem", DIAMONDS_TO_REDEEM)
            _tg_send(ref_id, (
                f"🎉 <b>New Referral!</b>\n\n"
                f"{user.get('name', 'Someone')} joined using your link.\n"
                f"💎 +1 Diamond\n"
                f"Total: <b>{ref['diamonds']}/{needed}</b>"
            ))

    save_data(data)
    _tg_send(user_id, (
        "✅ <b>Verification Complete!</b>\n\n"
        "Your device has been verified. You can now use the bot.\n"
        "Share your referral link to earn more diamonds! 💎"
    ))
    for aid in ADMIN_IDS:
        _tg_send(aid, (
            f"✅ <b>New verified user</b>\n"
            f"User: {user.get('name', '?')} <code>{uid_str}</code>\n"
            f"IP: <code>{ip}</code>\n"
            f"Fingerprint: <code>{fingerprint[:16]}…</code>\n"
            f"Referred by: <code>{ref_id or 'none'}</code>"
        ))
    return jsonify({"ok": True, "error": None})


# =========================
# APP BUILD
# =========================
async def post_init(app):
    me = await app.bot.get_me()
    data = load_data()
    data["settings"]["bot_username"] = me.username or ""
    save_data(data)
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
        raise SystemExit("★ BOT_TOKEN set nahi hai. Railway env vars me set karo.")

    print(f"Admin IDs: {sorted(ADMIN_IDS)}")
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
