#!/usr/bin/env python3
"""
UJALA STORE — Referral Reward Bot (bug-fixed)
---------------------------------
  /start (or /start ref_<id>) -> Join channels -> Verify -> Get referral link
  1 referral = 1 💎 diamond
  4 diamonds = redeem 1 Ujala coupon 🎟

Admin (/admin):
  • Add Ujala coupon codes (bulk pool)
  • View users / referrals / diamonds
  • View redemptions
  • Broadcast
  • Manage channels / store / support

Setup:
  1. pip install python-telegram-bot[job-queue]==21.6
  2. Top pe BOT_TOKEN + ADMIN_IDS edit karo
  3. python refer_bot.py
  4. Bot ko dono channels me ADMIN banao
  5. /admin se coupon codes pool me daalo
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
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

# ============================================================
# ★★★ SIRF YAHAN EDIT KARO — phir: python refer_bot.py
# ============================================================
BOT_TOKEN = ""  # leave blank — set BOT_TOKEN in Railway's Variables tab instead
ADMIN_IDS_RAW = "6894923643 1446058092 "
SUPPORT_USERNAME = "blankk020"
STORE_NAME = "UJALA STORE"
CHANNELS = [
    {"username": "blankkdealz", "name": "BLANK DEALZ"},
    {"username": "earnwithsakx", "name": "EARN WITH SAKX"},
]
DIAMONDS_TO_REDEEM = 3
MAX_REDEMPTIONS_PER_DAY = 2
# ============================================================

ADMIN_IDS = {int(x.strip()) for x in ADMIN_IDS_RAW.split() if x.strip()}

# DATA_DIR: where refer_data.json + backups/ live. On Railway, set this to
# your volume's mount path (e.g. /data) via an environment variable, so
# data survives redeploys/restarts. Falls back to the script's own folder
# for local runs where no volume is involved.
BASE_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).resolve().parent)))
BASE_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = BASE_DIR / "refer_data.json"

(
    ADM_ADD_CODES,
    ADM_BROADCAST,
    ADM_SET_SUPPORT,
    ADM_SET_STORE,
    ADM_ADD_CH_UN,
    ADM_ADD_CH_NAME,
    CAPTCHA_ANSWER,
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
}


def load_data() -> dict[str, Any]:
    if not DATA_FILE.exists():
        save_data(DEFAULT_DATA)
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
    """Atomic write: write to a temp file in the same dir, then os.replace()
    over the real file. This means a crash/kill mid-write leaves the old
    refer_data.json intact instead of a half-written/corrupted file."""
    tmp_path = DATA_FILE.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, DATA_FILE)


BACKUP_DIR = BASE_DIR / "backups"
BACKUP_KEEP_DAYS = 14


def backup_data_file() -> None:
    """Copy refer_data.json into backups/ once a day, named by date.
    Keeps the last BACKUP_KEEP_DAYS days and deletes older ones."""
    if not DATA_FILE.exists():
        return
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest = BACKUP_DIR / f"refer_data_{stamp}.json"
    try:
        with DATA_FILE.open("rb") as src, dest.open("wb") as dst:
            dst.write(src.read())
        log.info("Backup written: %s", dest.name)
    except Exception as e:
        log.warning("Backup failed: %s", e)
        return
    cutoff = time.time() - BACKUP_KEEP_DAYS * 86400
    for f in BACKUP_DIR.glob("refer_data_*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except Exception:
            pass


async def daily_backup_job(context):
    backup_data_file()


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


def redemptions_today(u: dict) -> int:
    """Count how many coupons this user has redeemed since UTC midnight today."""
    today = datetime.now(timezone.utc).date()
    count = 0
    for r in u.get("redemptions", []):
        ts = r.get("redeemed_at", "")
        try:
            r_date = datetime.fromisoformat(ts).date()
        except (ValueError, TypeError):
            continue
        if r_date == today:
            count += 1
    return count


def admin_denied_text(uid: int | None) -> str:
    configured = ", ".join(str(x) for x in sorted(ADMIN_IDS)) or "(empty)"
    return (
        "⛔ <b>Admins only</b>\n\n"
        f"Tumhara ID: <code>{uid}</code>\n"
        f"File ADMIN_IDS: <code>{configured}</code>\n\n"
        "1) /myid bhejo\n"
        "2) File TOP pe ADMIN_IDS_RAW edit karo\n"
        "3) Bot band karke dubara chalao: <code>python refer_bot.py</code>"
    )


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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎟 Add Coupon Codes", callback_data="adm:addcodes")],
        [InlineKeyboardButton(f"📋 Codes Pool ({codes_left})", callback_data="adm:viewcodes")],
        [InlineKeyboardButton(f"👥 Users ({n_users})", callback_data="adm:users")],
        [InlineKeyboardButton(f"📜 Redemptions ({n_redem})", callback_data="adm:redem")],
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
        f"{s.get('diamonds_to_redeem', DIAMONDS_TO_REDEEM)} 💎 = 1 Ujala Coupon 🎟\n\n"
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
        f"{needed} 💎 = 1 Ujala Coupon 🎟"
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
    ch_list = ", ".join(f"@{c['username']}" for c in s["channels"])
    return (
        "🛠 <b>ADMIN PANEL</b>\n\n"
        f"🏪 Store: <b>{s['store_name']}</b>\n"
        f"🎧 Support: @{s['support_username']}\n"
        f"📡 Channels: {ch_list}\n"
        f"💎 Redeem cost: {s.get('diamonds_to_redeem', DIAMONDS_TO_REDEEM)} 💎\n\n"
        f"👥 Users: {n_users} ({verified} verified)\n"
        f"🔗 Total referrals: {total_refs}\n"
        f"💎 Diamonds in circulation: {total_diamonds}\n"
        f"🎟 Codes in pool: <b>{codes_left}</b>\n"
        f"📜 Redemptions: {n_redem}\n\n"
        "<b>📌 ADMIN COMMANDS</b>\n"
        "/admin — yeh panel\n"
        "/myid — apna Telegram ID\n"
        "/cancel — wizard cancel\n"
        "/start — store menu\n\n"
        "Neeche buttons se manage karo."
    )


# =========================
# UI HELPERS  (FIXED: None-safe)
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


# =========================
# CAPTCHA (anti-bot)
# =========================
def make_captcha(context) -> str:
    """Generate a simple math captcha, store expected answer in user_data, return prompt text."""
    a, b = random.randint(1, 9), random.randint(1, 9)
    op = random.choice(["+", "-", "x"])
    if op == "+":
        ans = a + b
    elif op == "-":
        a, b = max(a, b), min(a, b)  # avoid negative
        ans = a - b
    else:
        ans = a * b
    context.user_data["captcha_answer"] = str(ans)
    context.user_data["captcha_tries"] = 0
    return f"🤖 <b>Verify you're human</b>\n\nSolve: <b>{a} {op} {b} = ?</b>\n\nReply with the number."


def check_captcha(context, text: str) -> bool:
    expected = context.user_data.get("captcha_answer")
    if expected is None:
        return False
    return (text or "").strip() == expected


def clear_captcha(context):
    context.user_data.pop("captcha_answer", None)
    context.user_data.pop("captcha_tries", None)


# =========================
# DEVICE / MULTI-ACCOUNT ABUSE CHECK
# =========================
# NOTE: Telegram Bot API doesn't expose real hardware/device IDs (privacy
# restriction on Telegram's side), so true "device fingerprinting" isn't
# possible from a bot. Instead we flag suspicious referral patterns —
# many fresh, no-username accounts verifying under the same referrer in a
# short window — and notify admins instead of silently blocking (avoids
# false positives on real users).
SUSPICIOUS_WINDOW_SECONDS = 120
SUSPICIOUS_COUNT_THRESHOLD = 3


def flag_suspicious_referral(data: dict, ref_id: int, new_user) -> bool:
    """Return True if this referral looks like multi-account farming."""
    ref = data["users"].get(str(ref_id))
    if not ref:
        return False
    recent = ref.setdefault("_recent_ref_ts", [])
    now = time.time()
    recent[:] = [t for t in recent if now - t < SUSPICIOUS_WINDOW_SECONDS]
    recent.append(now)
    no_username = not new_user.username
    return no_username and len(recent) >= SUSPICIOUS_COUNT_THRESHOLD



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


async def finalize_verification_core(context, data, uid, user):
    """Actually mark verified + award referral diamond. No message sending here
    except the referrer notification (kept from original behavior)."""
    u = get_user(data, uid)
    if not u:
        return
    u["verified"] = True
    u["verified_at"] = now_iso()
    u.pop("_pending_since", None)
    u.pop("_pending_delay", None)

    ref_id = u.get("referred_by")
    if ref_id and str(ref_id) != str(uid):
        ref = data["users"].get(str(ref_id))
        if ref:
            if flag_suspicious_referral(data, ref_id, user):
                for admin_id in ADMIN_IDS:
                    try:
                        await context.bot.send_message(
                            chat_id=admin_id,
                            text=(
                                "⚠️ <b>Suspicious referral pattern</b>\n\n"
                                f"Referrer ID: <code>{ref_id}</code>\n"
                                f"New user: {user.full_name} (@{user.username or '—'})\n"
                                f"Multiple no-username joins in a short window.\n"
                                "Diamond still awarded — review manually if needed."
                            ),
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass
            ref["diamonds"] = ref.get("diamonds", 0) + 1
            ref["referral_count"] = ref.get("referral_count", 0) + 1
            needed = data["settings"].get("diamonds_to_redeem", DIAMONDS_TO_REDEEM)
            try:
                await context.bot.send_message(
                    chat_id=ref_id,
                    text=(
                        f"🎉 <b>New Referral!</b>\n\n"
                        f"{user.full_name} joined using your link.\n"
                        f"💎 +1 Diamond\n"
                        f"Total: <b>{ref['diamonds']}/{needed}</b>"
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_menu_kb(ref_id),
                )
            except Exception as e:
                log.warning("Notify referrer %s failed: %s", ref_id, e)

    save_data(data)


async def finalize_verification(update, context, data, uid, user):
    """Called right after captcha passes. Verifies and credits the referral
    diamond immediately — no delay. Abuse protection is handled by the
    daily 2-coupon redemption cap plus the suspicious-burst admin alert
    in finalize_verification_core, not by a waiting period."""
    u = get_user(data, uid)
    if not u:
        return
    clear_captcha(context)

    await finalize_verification_core(context, data, uid, user)
    text = "✅ <b>Verification Complete!</b>\n\n" + main_menu_text(data, u)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_menu_kb(uid))


async def captcha_answer_handler(update, context):
    user = update.effective_user
    if not user:
        return ConversationHandler.END
    uid = user.id
    if check_captcha(context, update.message.text or ""):
        data = load_data()
        await finalize_verification(update, context, data, uid, user)
        return ConversationHandler.END

    tries = context.user_data.get("captcha_tries", 0) + 1
    context.user_data["captcha_tries"] = tries
    if tries >= 3:
        prompt = make_captcha(context)
        await update.message.reply_text(
            "❌ Galat jawab, 3 tries ho gaye. Naya captcha:\n\n" + prompt,
            parse_mode=ParseMode.HTML,
        )
        return CAPTCHA_ANSWER
    await update.message.reply_text(
        f"❌ Galat jawab ({tries}/3). Dobara try karo — number bhejo."
    )
    return CAPTCHA_ANSWER


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
    clear_captcha(context)
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

        # Channels joined -> now show CAPTCHA before finalizing verification
        prompt = make_captcha(context)
        if query.message:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=prompt,
                parse_mode=ParseMode.HTML,
            )
        return CAPTCHA_ANSWER

    # ---- USER: CAPTCHA HANDLED via captcha_answer_handler (text) ----
    if payload == "captcha:refresh":
        prompt = make_captcha(context)
        if query.message:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=prompt,
                parse_mode=ParseMode.HTML,
            )
        return CAPTCHA_ANSWER

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
            f"🎁 Redeemed coupons: <b>{u.get('redeemed_count', 0)}</b>\n"
            f"📅 Redeemed today: <b>{redemptions_today(u)}/{MAX_REDEMPTIONS_PER_DAY}</b>\n\n"
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

        done_today = redemptions_today(u)
        if done_today >= MAX_REDEMPTIONS_PER_DAY:
            await query.answer(
                f"🔒 Daily limit reached ({MAX_REDEMPTIONS_PER_DAY}/day). Try again tomorrow.",
                show_alert=True,
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
            f"🎟 Your Ujala Coupon Code:\n<code>{code}</code>\n\n"
            "Use it on Ujala. Happy shopping! 🛍"
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
                "Saare Ujala coupon codes <b>ek message</b> me bhejo.\n"
                "Har line pe 1 code, ya comma se separate.\n\n"
                "Example:\n"
                "<code>UJALA100\nUJALA200\nUJALA300</code>\n\n"
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

    if payload == "adm:stats":
        n_users = len(data.get("users", {}))
        verified = sum(1 for u in data["users"].values() if u.get("verified"))
        total_diamonds = sum(u.get("diamonds", 0) for u in data["users"].values())
        total_refs = sum(u.get("referral_count", 0) for u in data["users"].values())
        n_redem = len(data.get("redemptions", []))
        codes_left = len(data.get("coupon_pool", []))
        text = (
            "📊 <b>STATS</b>\n\n"
            f"👥 Total users: {n_users}\n"
            f"✅ Verified: {verified}\n"
            f"❌ Unverified: {n_users - verified}\n"
            f"🔗 Total referrals: {total_refs}\n"
            f"💎 Diamonds in circulation: {total_diamonds}\n"
            f"🎟 Codes in pool: {codes_left}\n"
            f"📜 Total redemptions: {n_redem}\n"
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
            "🏪 Store name bhejo:\nExample: <code>UJALA STORE</code>",
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
# APP BUILD  (FIXED: no duplicate handlers)
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

    # DAILY BACKUP JOB
    # Runs once a day, keeps a rolling window of refer_data.json snapshots
    # under backups/. Also take one backup right now on startup.
    backup_data_file()
    if app.job_queue is not None:
        app.job_queue.run_repeating(daily_backup_job, interval=86400, first=86400)


def build_app() -> Application:
    token = (os.getenv("BOT_TOKEN") or BOT_TOKEN or "").strip()
    if not token or ":" not in token:
        raise SystemExit("★ BOT_TOKEN set nahi hai. File TOP pe edit karo.")

    print(f"Admin IDs: {sorted(ADMIN_IDS)}")
    print(f"Store: {STORE_NAME}")
    print(f"Channels: {[c['username'] for c in CHANNELS]}")
    print(f"Diamonds to redeem: {DIAMONDS_TO_REDEEM}")

    app = Application.builder().token(token).build()

    # /myid always available (outside conversation)
    app.add_handler(CommandHandler("myid", cmd_myid), group=0)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("admin", cmd_admin),
            CallbackQueryHandler(on_callback),
        ],
        states={
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
            CAPTCHA_ANSWER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, captcha_answer_handler),
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
    # FIX: Removed group 2 (duplicate CallbackQueryHandler) and group 3
    # (duplicate MessageHandler) that caused every callback and text message
    # to be processed twice.
    app.post_init = post_init
    return app


def main():
    app = build_app()
    log.info("Referral bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
