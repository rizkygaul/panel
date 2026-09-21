"""
OTP Bot - Versi Render (Webhook Mode)
Baca konfigurasi dari Environment Variables Render.
"""

import asyncio
import hashlib
import httpx
import html
import ipaddress
import re
import json
import os
import io
import random
import sys
import time
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone

from flask import Flask, request, jsonify

from telegram.ext import (
    CommandHandler, CallbackQueryHandler, MessageHandler,
    ConversationHandler, filters, ApplicationBuilder
)
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update
)
from telegram.error import BadRequest
try:
    from telegram import CopyTextButton
    _HAS_COPY_BTN = True
except ImportError:
    _HAS_COPY_BTN = False

# ══════════════════════════════════════════════════════════════
#  ★ KONFIGURASI (DARI ENVIRONMENT VARIABLES RENDER) ★
# ══════════════════════════════════════════════════════════════

def _require_env(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        sys.exit(f"❌ Environment variable '{name}' belum diisi.")
    return val

BOT_TOKEN      = _require_env("8655005886:AAEHsq_IRGEfCCpbiHA-PvMAaMuiYRy1qDg")
CHAT_ID        = _require_env("-1003879729631")
ADMIN_ID       = int(_require_env("8573344923"))
AUGESTEL_API_KEY = os.getenv("sk_live_dEwXVL6wPi12iqLPtjwBgsuI4SpDXhPk2J3nysAd", "").strip()
WEBHOOK_SECRET   = os.getenv("AUGESTEL_WEBHOOK_SECRET", "").strip()

# Render: port dari env PORT
WEBHOOK_PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_HOST = "0.0.0.0"

# Render: URL otomatis
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
WEBHOOK_PATH = "/webhooks/augestel"
WEBHOOK_PUBLIC_URL = RENDER_URL + WEBHOOK_PATH if RENDER_URL else ""

# Mode: webhook saja (polling off)
WEBHOOK_ENABLED = True
POLLING_ENABLED = False

# File state
SENT_IDS_FILE = "sent_ids.json"
LOG_FILE      = "otp_log.txt"
STATS_FILE    = "stats.json"
SETTINGS_FILE = "bot_settings.json"
ACCOUNTS_FILE = "account.json"
MAX_SENT_IDS  = 100_000

# Konstanta
PANEL_BASE = "https://augestel.com/api/v1/iprn"
POLL_INTERVAL_MIN = 5
POLL_INTERVAL_MAX = 300
WIB = timezone(timedelta(hours=7), "WIB")
WEBHOOK_MAX_BODY_BYTES = 1024 * 1024
WEBHOOK_QUEUE_MAXSIZE = 1000
WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 15 * 60
WEBHOOK_WORKER_RETRIES = 3
WEBHOOK_WORKER_BACKOFF_SECONDS = 2

# ══════════════════════════════════════════════════════════════
#  ★ LOGGING SEDERHANA ★
# ══════════════════════════════════════════════════════════════

def _log(msg: str):
    print(f"[{datetime.now(WIB).strftime('%H:%M:%S')}] {msg}", flush=True)

# ══════════════════════════════════════════════════════════════
#  ★ OTP HELPERS ★
# ══════════════════════════════════════════════════════════════

def detect_service(sender, message):
    t = f"{sender} {message}".upper()
    if "WHATSAPP" in t or "WA CODE" in t: return "WS"
    if "TELEGRAM" in t or "TG CODE" in t: return "TG"
    if "FACEBOOK" in t or "META" in t:    return "FB"
    if "GOOGLE" in t or "G-" in t:        return "GO"
    if "INSTAGRAM" in t:                   return "IG"
    if "TIKTOK" in t:                      return "TT"
    if "TWITTER" in t:                     return "TW"
    if "SNAPCHAT" in t:                    return "SC"
    if "DISCORD" in t:                     return "DC"
    if "MICROSOFT" in t or "OUTLOOK" in t: return "MS"
    if "APPLE" in t or "ICLOUD" in t:      return "AP"
    if "BITGET" in t:                      return "BG"
    if "WECHAT" in t:                      return "WC"
    if "IMO" in t:                         return "IMO"
    if "NETFLIX" in t:                     return "NF"
    if "SHOPEE" in t:                      return "SP"
    if "LAZADA" in t:                      return "LA"
    if "TINDER" in t:                      return "TN"
    if "XIAOMI" in t or "MI ACCOUNT" in t: return "MI"
    return sender[:2].upper() if sender else "OT"

def extract_otp(message):
    m = re.search(r'\b(\d{3}-\d{3})\b', message)
    if m: return m.group(1).replace('-', '')
    for pat in [r'(?i)(?:code|otp|is|kode|adalah|:)\s*(\d{4,8})', r'\b(\d{4,8})\b']:
        m = re.search(pat, message)
        if m: return m.group(1)
    for n in re.findall(r'\d+', message):
        if 4 <= len(n) <= 8: return n
    return "N/A"

def get_country_info(phone_number):
    clean = re.sub(r'\D', '', str(phone_number))
    return 'Unknown', '🌎', 'UN'

# ══════════════════════════════════════════════════════════════
#  ★ SENT IDS ★
# ══════════════════════════════════════════════════════════════

_sent_ids_cache = None

def _load_sent_ids():
    global _sent_ids_cache
    if _sent_ids_cache is not None:
        return _sent_ids_cache
    if os.path.exists(SENT_IDS_FILE):
        try:
            with open(SENT_IDS_FILE) as f:
                _sent_ids_cache = set(json.load(f))
                _log(f"Loaded {len(_sent_ids_cache)} sent IDs")
                return _sent_ids_cache
        except Exception as e:
            _log(f"Gagal baca sent_ids.json: {e}")
    _sent_ids_cache = set()
    return _sent_ids_cache

def _persist_sent_id(sms_id: str):
    global _sent_ids_cache
    if _sent_ids_cache is None:
        _sent_ids_cache = set()
    _sent_ids_cache.add(sms_id)
    if len(_sent_ids_cache) > MAX_SENT_IDS:
        excess = len(_sent_ids_cache) - MAX_SENT_IDS
        to_remove = list(_sent_ids_cache)[:excess]
        _sent_ids_cache -= set(to_remove)
    try:
        with open(SENT_IDS_FILE, "w") as f:
            json.dump(list(_sent_ids_cache), f)
    except Exception as e:
        _log(f"Gagal simpan sent_ids.json: {e}")

def _stable_sms_id(source, number, message, received_at):
    stable = f"{source}|{number}|{message}|{received_at}"
    return hashlib.sha256(stable.encode()).hexdigest()

def _incoming_dedup_keys(row):
    source = str(row.get("source", "")).strip()
    number = re.sub(r"[^0-9]", "", str(row.get("number", "")))
    message = str(row.get("message", "")).strip()
    received_at = str(row.get("received_at", "")).strip()
    keys = set()
    if source and number and message:
        keys.add(_stable_sms_id(source, number, message, received_at))
    return keys

def _webhook_row(payload):
    if str(payload.get("event", "")).strip().lower() != "message.received":
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    source = str(data.get("source", "")).strip()
    message = str(data.get("message", "")).strip()
    number = str(data.get("number") or data.get("recipient") or "").strip()
    if not source or not message or not number:
        return None
    return {
        "source": source,
        "number": number,
        "message": message,
        "range_name": str(data.get("range_name") or data.get("range") or "").strip(),
        "received_at": str(data.get("received_at") or payload.get("timestamp") or "").strip(),
    }

# ══════════════════════════════════════════════════════════════
#  ★ TELEGRAM SEND ★
# ══════════════════════════════════════════════════════════════

async def send_otp_to_telegram(app, source, number, message, received_at):
    otp = extract_otp(message)
    service = detect_service(source, message)
    if not otp or otp == "N/A":
        _log(f"Tidak ada OTP di pesan dari {source}")
        return False
    text = (
        f"📱 <b>OTP DETECTED</b>\n\n"
        f"<b>{service}</b>\n"
        f"📞 <code>{html.escape(number)}</code>\n"
        f"🕐 {html.escape(received_at)}\n\n"
        f"<blockquote>{html.escape(message[:300])}</blockquote>\n\n"
        f"🔑 <b>OTP: {html.escape(otp)}</b>"
    )
    try:
        await app.bot.send_message(
            chat_id=CHAT_ID, text=text,
            parse_mode="HTML", disable_web_page_preview=True
        )
        _log(f"✅ OTP terkirim: {otp} dari {number}")
        return True
    except Exception as e:
        _log(f"❌ Gagal kirim Telegram: {e}")
        return False

# ══════════════════════════════════════════════════════════════
#  ★ FLASK APP (Webhook Receiver) ★
# ══════════════════════════════════════════════════════════════

flask_app = Flask(__name__)
telegram_app = None  # diisi saat startup

def verify_signature(raw_body, signature_header, secret):
    if not secret:
        return True
    if not signature_header:
        return False
    provided = signature_header.strip()
    if provided.lower().startswith("sha256="):
        provided = provided.split("=", 1)[1].strip()
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided.lower(), expected.lower())

@flask_app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "OTP Bot aktif",
        "webhook": WEBHOOK_PATH,
        "telegram": "connected" if telegram_app else "starting"
    })

@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})

@flask_app.route(WEBHOOK_PATH, methods=["POST"])
def augestel_webhook():
    raw_body = request.get_data()
    signature = request.headers.get("X-Augestel-Signature", "")
    if not verify_signature(raw_body, signature, WEBHOOK_SECRET):
        _log("Signature tidak valid!")
        return jsonify({"error": "invalid signature"}), 401
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        _log(f"JSON error: {e}")
        return jsonify({"error": "invalid json"}), 400
    if data.get("event") == "message.received":
        sms = data.get("data", {})
        source = sms.get("source", "Unknown")
        number = sms.get("number", "Unknown")
        message = sms.get("message", "")
        received_at = sms.get("received_at", "")
        sms_id = _stable_sms_id(source, number, message, received_at)
        sent_ids = _load_sent_ids()
        if sms_id in sent_ids:
            _log(f"Duplikat, skip: {sms_id[:16]}")
            return jsonify({"ok": True, "status": "duplicate"}), 200
        sent_ids.add(sms_id)
        _persist_sent_id(sms_id)
        if telegram_app:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(
                    send_otp_to_telegram(
                        telegram_app, source, number, message, received_at
                    )
                )
                loop.close()
            except Exception as e:
                _log(f"Error kirim OTP: {e}")
    return jsonify({"ok": True}), 200

# ══════════════════════════════════════════════════════════════
#  ★ TELEGRAM BOT HANDLERS ★
# ══════════════════════════════════════════════════════════════

def is_admin(user_id):
    return user_id == ADMIN_ID

async def cmd_start(update: Update, ctx):
    await update.message.reply_text(
        f"🤖 Bot OTP aktif!\n\nUser ID: <code>{update.effective_user.id}</code>",
        parse_mode="HTML"
    )

async def cmd_admin(update: Update, ctx):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return
    sent_ids = _load_sent_ids()
    text = (
        f"🛠 <b>Admin Dashboard</b>\n\n"
        f"📊 Total OTP diproses: <b>{len(sent_ids)}</b>\n"
        f"🌐 Webhook: <code>{WEBHOOK_PUBLIC_URL}</code>\n"
        f"💚 Status: <b>Aktif</b>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_test(update: Update, ctx):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return
    await update.message.reply_text("📤 Mengirim OTP test...")
    dummy_source = "WhatsApp"
    dummy_number = "6281234567890"
    dummy_message = "Your WhatsApp code is 263-414. Don't share this code."
    dummy_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    ok = await send_otp_to_telegram(
        ctx.application, dummy_source, dummy_number, dummy_message, dummy_time
    )
    if ok:
        await update.message.reply_text("✅ OTP test terkirim ke grup!")
    else:
        await update.message.reply_text("❌ Gagal kirim OTP test.")

# ══════════════════════════════════════════════════════════════
#  ★ STARTUP ★
# ══════════════════════════════════════════════════════════════

async def setup_telegram():
    global telegram_app
    if not BOT_TOKEN:
        _log("BOT_TOKEN belum diisi!")
        return None
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("test", cmd_test))
    await app.initialize()
    await app.start()
    if WEBHOOK_PUBLIC_URL:
        await app.bot.set_webhook(
            url=WEBHOOK_PUBLIC_URL,
            drop_pending_updates=True
        )
        _log(f"✅ Webhook Telegram diset: {WEBHOOK_PUBLIC_URL}")
    telegram_app = app
    return app

def run_telegram_in_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(setup_telegram())
    loop.run_forever()

def main():
    _log("🚀 Starting OTP Bot (Render mode)...")
    _log(f"   Port: {WEBHOOK_PORT}")
    _log(f"   Webhook URL: {WEBHOOK_PUBLIC_URL or 'belum diset'}")

    import threading
    tg_thread = threading.Thread(target=run_telegram_in_thread, daemon=True)
    tg_thread.start()

    time.sleep(3)

    _log(f"🌐 Flask listening on port {WEBHOOK_PORT}")
    flask_app.run(host=WEBHOOK_HOST, port=WEBHOOK_PORT)

if __name__ == "__main__":
    main()
