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
from dotenv import load_dotenv
from aiohttp import web
import hmac

DOTENV_FILE = os.getenv("DOTENV_FILE", ".env").strip() or ".env"
load_dotenv(dotenv_path=DOTENV_FILE)  # baca file .env di direktori yang sama dengan main.py
from datetime import datetime, timedelta, timezone
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

def _copy_btn(text: str, label: str, **api_kwargs) -> InlineKeyboardButton:
    """Buat tombol copy-text jika versi PTB mendukung, otherwise tombol biasa."""
    if _HAS_COPY_BTN:
        return InlineKeyboardButton(label, copy_text=CopyTextButton(text), **api_kwargs)
    # Fallback: tombol tanpa fungsi copy (tidak crash di PTB lama)
    return InlineKeyboardButton(label, callback_data="noop")

# ─── Styled buttons (warna tombol asli Telegram, Bot API 9.4+) ─────────────────
# PENTING: Telegram HANYA mendukung 3 nilai warna resmi: "primary" (biru),
# "success" (hijau), "danger" (merah). Tidak ada "secondary" atau "warning" --
# mengirim nilai selain 3 itu akan ditolak Telegram dengan error
# 'invalid button style specified' dan MEMBATALKAN SELURUH keyboard (bukan cuma
# satu tombol). Karena kode lama di project ini banyak memakai "secondary" dan
# "warning", kita petakan otomatis ke warna resmi terdekat supaya tidak perlu
# mengubah setiap pemanggilan btn() satu per satu:
#   secondary -> primary (biru, dipakai untuk menu/manajemen)
#   warning   -> danger  (merah/oranye-ish, dipakai untuk peringatan)
BUTTON_STYLES = {"primary", "success", "danger"}  # satu-satunya nilai valid di Telegram

# Koreksi typo + pemetaan nama lama -> nama resmi yang didukung Telegram
_STYLE_TYPO_MAP = {
    "prymary": "primary", "primery": "primary", "primary ": "primary",
    "secondary": "primary", "seconday": "primary", "secondry": "primary",
    "succes": "success", "succses": "success", "sucess": "success",
    "dangar": "danger", "dangerr": "danger",
    "warning": "danger", "warnning": "danger", "warining": "danger",
}

def _normalize_style(style, default="primary"):
    """Normalisasi nama style ke salah satu dari 3 nilai resmi Telegram
    (primary/success/danger). Nilai lain (typo, 'secondary', 'warning') otomatis
    dipetakan ke warna terdekat lewat _STYLE_TYPO_MAP. Fallback ke `default`."""
    if not style:
        return default
    s = str(style).strip().lower()
    if s in BUTTON_STYLES:
        return s
    return _STYLE_TYPO_MAP.get(s, default)

def _style_kwargs(style, icon=None):
    """Bangun api_kwargs untuk styling tombol asli Telegram (Bot API 9.4+).
    style=None -> tombol standar tanpa warna khusus."""
    api_kwargs = {}
    if style:
        api_kwargs["style"] = _normalize_style(style)
    if icon:
        api_kwargs["icon_custom_emoji_id"] = icon
    return api_kwargs

def btn(label, callback_data=None, url=None, style=None, icon=None):
    """Buat InlineKeyboardButton dengan warna asli Telegram
    (style: primary/success/danger -- 'secondary' & 'warning' otomatis dipetakan).

    Contoh:
        btn("Hapus", callback_data="adm_del_numbers", style="danger")
        btn("Buka Link", url="https://t.me/xxx", style="primary")
    """
    kwargs = _style_kwargs(style, icon)
    if callback_data is not None:
        return InlineKeyboardButton(label, callback_data=callback_data, api_kwargs=kwargs or None)
    if url is not None:
        return InlineKeyboardButton(label, url=url, api_kwargs=kwargs or None)
    raise ValueError("btn() butuh callback_data atau url")

# ─── Deteksi emoji premium yang ditempel LANGSUNG (bukan lewat ID) ─────────────
# Kalau admin tempel emoji premium (custom emoji) langsung ke chat, teks pesan
# yang diterima bot HANYA berisi karakter placeholder-nya (mis. "⭐"), sedangkan
# ID emoji premium yang sesungguhnya ada di message.entities (type="custom_emoji").
# Fungsi di bawah ini membaca entities tsb supaya paste-langsung & ID manual
# sama-sama didukung, tanpa admin harus cari ID secara manual.

def _utf16_slice(text: str, offset: int, length: int) -> str:
    """Ambil substring sesuai offset/length ala Telegram (dihitung dalam UTF-16
    code unit, BUKAN index karakter Python biasa -- perlu utk emoji astral)."""
    b = text.encode("utf-16-le")
    return b[offset * 2:(offset + length) * 2].decode("utf-16-le")

def _custom_emoji_map(message) -> dict:
    """dict {karakter_emoji: custom_emoji_id} dari entities pesan (kosong jika
    admin tidak menempel emoji premium apa pun di pesan ini)."""
    text = message.text or ""
    out = {}
    for e in (message.entities or []):
        if e.type == "custom_emoji":
            ch = _utf16_slice(text, e.offset, e.length)
            if ch:
                out[ch] = e.custom_emoji_id
    return out

def _wrap_custom_emoji_html(text: str, emoji_map: dict) -> str:
    """Bungkus tiap kemunculan karakter emoji premium (dari emoji_map) dengan
    tag <tg-emoji emoji-id="..."> supaya tampil sebagai emoji premium beneran
    saat pesan dikirim dengan parse_mode HTML."""
    if not emoji_map or not text:
        return text
    out = text
    for ch, eid in emoji_map.items():
        out = out.replace(ch, f'<tg-emoji emoji-id="{eid}">{ch}</tg-emoji>')
    return out

def _nested_emoji_in_code_warning(rendered_html: str) -> str | None:
    """Telegram TIDAK mengizinkan tag HTML bersarang di dalam <code>/<pre> --
    kalau ada <tg-emoji> (emoji premium) yang kebungkus di situ, Telegram diam-
    diam menolak render-nya dan cuma nampilin emoji fallback biasa. Fungsi ini
    scan hasil render template (final HTML, bukan template mentah) dan kasih
    peringatan jelas kalau pola bermasalah ini ketemu."""
    for tag in ("code", "pre"):
        for m in re.finditer(rf"<{tag}\b[^>]*>(.*?)</{tag}>", rendered_html, re.S):
            if "<tg-emoji" in m.group(1):
                return (
                    f"⚠️ <b>Emoji premium tidak akan muncul!</b> Template kamu "
                    f"membungkus emoji premium di dalam tag <code>&lt;{tag}&gt;</code>. "
                    f"Telegram tidak mengizinkan tag apa pun bersarang di dalam "
                    f"<code>&lt;{tag}&gt;</code>, jadi emoji premium otomatis gagal "
                    f"tampil (jadi emoji biasa saja).\n"
                    f"<b>Perbaikan:</b> keluarkan placeholder yang berisi emoji "
                    f"premium (<code>{{number}}</code>/<code>{{number_masked}}</code>/"
                    f"<code>{{flag}}</code>/<code>{{svc_icon}}</code>) dari dalam tag "
                    f"<code>&lt;{tag}&gt;</code> — pakai <code>&lt;b&gt;</code> atau "
                    f"tanpa tag sama sekali di situ."
                )
    return None

from telegram.ext import ContextTypes

# ─── Konfigurasi (dari environment variable / file .env) ───────────────────────
# JANGAN hardcode token/API key di sini. Isi nilainya di file .env (lihat
# .env.example) yang TIDAK ikut di-commit ke Git (sudah ada di .gitignore).

def _require_env(name: str) -> str:
    """Ambil env var wajib. Kalau kosong, hentikan bot dengan pesan error yang
    jelas -- daripada crash aneh di tengah jalan saat token/key dipakai."""
    val = os.getenv(name, "").strip()
    if not val:
        sys.exit(
            f"❌ Environment variable '{name}' belum diisi.\n"
            f"   Buat file .env (contoh: .env.example) lalu isi {name}=... di dalamnya."
        )
    return val

BOT_TOKEN      = _require_env("BOT_TOKEN")
CHAT_ID        = _require_env("CHAT_ID")
ADMIN_ID       = int(_require_env("ADMIN_ID"))     # Owner utama
ACCOUNTS_FILE  = os.getenv("ACCOUNTS_FILE", "account.json")
SETTINGS_FILE  = os.getenv("SETTINGS_FILE", "bot_settings.json")
AUGESTEL_API_KEY = os.getenv("AUGESTEL_API_KEY", "").strip()

def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

WEBHOOK_ENABLED = _env_bool("WEBHOOK_ENABLED", True)
POLLING_ENABLED = _env_bool("POLLING_ENABLED", True)
WEBHOOK_HOST = (
    os.getenv("WEBHOOK_HOST")
    or os.getenv("WEBHOOK_IP")
    or "0.0.0.0"
).strip()
try:
    WEBHOOK_PORT = int(os.getenv("PORT") or os.getenv("WEBHOOK_PORT", "8080"))
except ValueError:
    WEBHOOK_PORT = 8080
WEBHOOK_PORT = max(1, min(WEBHOOK_PORT, 65535))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhooks/augestel").strip() or "/webhooks/augestel"
if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH
WEBHOOK_PATH = WEBHOOK_PATH.rstrip("/") or "/webhooks/augestel"
WEBHOOK_PUBLIC_URL = (
    os.getenv("WEBHOOK_PUBLIC_URL")
    or os.getenv("WEBHOOK_URL")
    or ""
).strip().rstrip("/")
# Pterodactyl/KataBump biasanya menyediakan IP dan port allocation secara
# terpisah. Jika URL lengkap tidak diisi, rakit URL webhook dari nilai tersebut.
# Urutan fallback dibuat longgar agar tetap kompatibel dengan egg/container
# yang memakai nama environment berbeda.
PUBLIC_IP = (
    os.getenv("PUBLIC_IP")
    or os.getenv("PTERODACTYL_SERVER_IP")
    or os.getenv("SERVER_IP")
    or os.getenv("P_SERVER_IP")
    or ""
).strip()
PUBLIC_PORT = (
    os.getenv("PUBLIC_PORT")
    or os.getenv("PTERODACTYL_SERVER_PORT")
    or os.getenv("SERVER_PORT")
    or os.getenv("P_SERVER_PORT")
    or os.getenv("PORT")
    or ""
).strip()
WEBHOOK_SECRET = os.getenv("AUGESTEL_WEBHOOK_SECRET", "").strip()
WEBHOOK_ACCOUNT_NAME = os.getenv("WEBHOOK_ACCOUNT_NAME", "").strip()
WEBHOOK_MAX_BODY_BYTES = 1024 * 1024
WEBHOOK_QUEUE_MAXSIZE = 1000
WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 15 * 60
WEBHOOK_WORKER_RETRIES = 3
WEBHOOK_WORKER_BACKOFF_SECONDS = 2
try:
    # Optional extra spacing between API requests. Keep this at zero so the
    # account's configured poll_interval is the actual polling cadence.
    API_MIN_REQUEST_GAP_SECONDS = max(
        0.0, float(os.getenv("API_MIN_REQUEST_GAP_SECONDS", "0"))
    )
except (TypeError, ValueError):
    API_MIN_REQUEST_GAP_SECONDS = 0.0


def _write_env_values(values: dict[str, str]) -> None:
    """Update only non-secret configuration keys in the configured .env file."""
    try:
        with open(DOTENV_FILE, encoding="utf-8") as env_file:
            lines = env_file.readlines()
    except FileNotFoundError:
        lines = []

    for key, value in values.items():
        pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=.*$", re.MULTILINE)
        replacement = f"{key}={value}\n"
        replaced = False
        for index, line in enumerate(lines):
            if pattern.match(line.rstrip("\n")):
                lines[index] = replacement
                replaced = True
                break
        if not replaced:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(replacement)

    parent = os.path.dirname(os.path.abspath(DOTENV_FILE))
    os.makedirs(parent, exist_ok=True)
    temp_file = f"{DOTENV_FILE}.tmp"
    with open(temp_file, "w", encoding="utf-8") as env_file:
        env_file.writelines(lines)
    os.replace(temp_file, DOTENV_FILE)


def _validate_webhook_host(value: str) -> tuple[bool, str]:
    """Validate an IP address or a bindable hostname."""
    host = value.strip()
    if not host or len(host) > 253 or any(char.isspace() for char in host):
        return False, "IP/host tidak boleh kosong atau mengandung spasi."
    if "/" in host or "://" in host:
        return False, "Masukkan IP/host saja, bukan URL lengkap."
    if host.lower() in {"localhost", "0.0.0.0", "::"}:
        return True, host
    try:
        ipaddress.ip_address(host)
        return True, host
    except ValueError:
        pass
    if re.fullmatch(
        r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
        host,
    ):
        return True, host
    return False, "IP/host tidak valid."


def _validate_webhook_path(value: str) -> tuple[bool, str]:
    path = value.strip()
    if not path:
        return False, "Path URL tidak boleh kosong."
    if not path.startswith("/"):
        path = "/" + path
    if any(char.isspace() for char in path) or "?" in path or "#" in path:
        return False, "Path tidak boleh mengandung spasi, query, atau fragment."
    return True, path.rstrip("/") or "/"


def _validate_webhook_port(value: str) -> tuple[bool, int]:
    if not value.strip().isdigit():
        return False, 0
    port = int(value.strip())
    if not 1 <= port <= 65535:
        return False, 0
    return True, port


def _validate_webhook_public_url(value: str) -> tuple[bool, str]:
    url = value.strip().rstrip("/")
    if not url:
        return True, ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False, "Public URL harus berupa alamat http:// atau https:// yang valid."
    if parsed.query or parsed.fragment:
        return False, "Public URL tidak boleh memiliki query atau fragment."
    return True, url


def _build_allocation_public_url() -> str:
    """Build an HTTP public URL from a Pterodactyl IP/port allocation."""
    host = PUBLIC_IP.strip()
    port = PUBLIC_PORT.strip()
    if not host or not port.isdigit():
        return ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}{WEBHOOK_PATH}"


if not WEBHOOK_PUBLIC_URL:
    WEBHOOK_PUBLIC_URL = _build_allocation_public_url()

# Augestel IPRN API base. Endpoint dokumentasinya berada di bawah /iprn.
PANEL_BASE = "https://augestel.com/api/v1/iprn"
POLL_INTERVAL_MIN = 5
POLL_INTERVAL_MAX = 300
WIB = timezone(timedelta(hours=7), "WIB")

def now_wib() -> datetime:
    """Return the current time in Western Indonesian Time (UTC+7)."""
    return datetime.now(WIB)

# ─── Conversation states (admin dashboard) ──────────────────────────────────────
(
    ADMIN_MAIN,
    ALLOC_RANGE,
    ALLOC_QTY,
    RATECARD_SEARCH,
    ACCESS_SEARCH,
    TRAFFIC_SEARCH,
    NUM_SEARCH,
    ACC_ADD_NAME,
    ACC_ADD_KEY,
    ACC_ADD_WEBHOOK_SECRET,
    ACC_EDIT_KEY,
    ACC_EDIT_WEBHOOK_SECRET,
    FORMAT_INPUT,
    POLL_INTERVAL_INPUT,
    MASK_EMOJI_INPUT,
    WEBHOOK_HOST_INPUT,
    WEBHOOK_PORT_INPUT,
    WEBHOOK_PATH_INPUT,
    WEBHOOK_PUBLIC_URL_INPUT,
    DEL_NUMS_RANGE,
    ALLOC_CUSTOM_QTY,
) = range(21)

# ─── Country data ────────────────────────────────────────────────────────────────
# Katalog ini mengikuti ISO 3166-1 alpha-2: 195 negara berdaulat ditambah
# wilayah/entitas yang memang memiliki kode ISO dan bendera sendiri. Dengan
# begitu menu negara dan fallback emoji tidak berhenti pada negara yang punya
# kode telepon unik saja.
#
# Kolom: ISO alpha-2 | nama resmi yang ringkas | kode/prefix telepon E.164.
# Prefix kosong berarti entitas tersebut tidak memiliki nomor telepon publik
# tersendiri. Prefix yang berbagi kode diprioritaskan berdasarkan prefix terpanjang
# di get_country_info().
_COUNTRY_CATALOG_ROWS = """
AF|Afghanistan|93
AX|Åland Islands|35818
AL|Albania|355
DZ|Algeria|213
AS|American Samoa|1684
AD|Andorra|376
AO|Angola|244
AI|Anguilla|1264
AQ|Antarctica|
AG|Antigua and Barbuda|1268
AR|Argentina|54
AM|Armenia|374
AW|Aruba|297
AU|Australia|61
AT|Austria|43
AZ|Azerbaijan|994
BS|Bahamas|1242
BH|Bahrain|973
BD|Bangladesh|880
BB|Barbados|1246
BY|Belarus|375
BE|Belgium|32
BZ|Belize|501
BJ|Benin|229
BM|Bermuda|1441
BT|Bhutan|975
BO|Bolivia|591
BQ|Caribbean Netherlands|5997
BA|Bosnia and Herzegovina|387
BW|Botswana|267
BV|Bouvet Island|
BR|Brazil|55
IO|British Indian Ocean Territory|246
BN|Brunei|673
BG|Bulgaria|359
BF|Burkina Faso|226
BI|Burundi|257
CV|Cabo Verde|238
KH|Cambodia|855
CM|Cameroon|237
CA|Canada|1204
KY|Cayman Islands|1345
CF|Central African Republic|236
TD|Chad|235
CL|Chile|56
CN|China|86
CX|Christmas Island|6189164
CC|Cocos (Keeling) Islands|6189162
CO|Colombia|57
KM|Comoros|269
CG|Congo|242
CD|Democratic Republic of the Congo|243
CK|Cook Islands|682
CR|Costa Rica|506
CI|Côte d’Ivoire|225
HR|Croatia|385
CU|Cuba|53
CW|Curaçao|5999
CY|Cyprus|357
CZ|Czechia|420
DK|Denmark|45
DJ|Djibouti|253
DM|Dominica|1767
DO|Dominican Republic|1809
EC|Ecuador|593
EG|Egypt|20
SV|El Salvador|503
GQ|Equatorial Guinea|240
ER|Eritrea|291
EE|Estonia|372
SZ|Eswatini|268
ET|Ethiopia|251
FK|Falkland Islands|500
FO|Faroe Islands|298
FJ|Fiji|679
FI|Finland|358
FR|France|33
GF|French Guiana|594
PF|French Polynesia|689
TF|French Southern Territories|
GA|Gabon|241
GM|Gambia|220
GE|Georgia|995
DE|Germany|49
GH|Ghana|233
GI|Gibraltar|350
GR|Greece|30
GL|Greenland|299
GD|Grenada|1473
GP|Guadeloupe|590
GU|Guam|1671
GT|Guatemala|502
GG|Guernsey|441481
GN|Guinea|224
GW|Guinea-Bissau|245
GY|Guyana|592
HT|Haiti|509
HM|Heard Island and McDonald Islands|
VA|Holy See (Vatican City)|379
HN|Honduras|504
HK|Hong Kong|852
HU|Hungary|36
IS|Iceland|354
IN|India|91
ID|Indonesia|62
IR|Iran|98
IQ|Iraq|964
IE|Ireland|353
IM|Isle of Man|441624
IL|Israel|972
IT|Italy|39
JM|Jamaica|1876
JP|Japan|81
JE|Jersey|441534
JO|Jordan|962
KZ|Kazakhstan|76,77
KE|Kenya|254
KI|Kiribati|686
KP|North Korea|850
KR|South Korea|82
KW|Kuwait|965
KG|Kyrgyzstan|996
LA|Laos|856
LV|Latvia|371
LB|Lebanon|961
LS|Lesotho|266
LR|Liberia|231
LY|Libya|218
LI|Liechtenstein|423
LT|Lithuania|370
LU|Luxembourg|352
MO|Macao|853
MG|Madagascar|261
MW|Malawi|265
MY|Malaysia|60
MV|Maldives|960
ML|Mali|223
MT|Malta|356
MH|Marshall Islands|692
MQ|Martinique|596
MR|Mauritania|222
MU|Mauritius|230
YT|Mayotte|262269
MX|Mexico|52
FM|Micronesia|691
MD|Moldova|373
MC|Monaco|377
MN|Mongolia|976
ME|Montenegro|382
MS|Montserrat|1664
MA|Morocco|212
MZ|Mozambique|258
MM|Myanmar|95
NA|Namibia|264
NR|Nauru|674
NP|Nepal|977
NL|Netherlands|31
NC|New Caledonia|687
NZ|New Zealand|64
NI|Nicaragua|505
NE|Niger|227
NG|Nigeria|234
NU|Niue|683
NF|Norfolk Island|6723
MK|North Macedonia|389
MP|Northern Mariana Islands|1670
NO|Norway|47
OM|Oman|968
PK|Pakistan|92
PW|Palau|680
PS|Palestine|970
PA|Panama|507
PG|Papua New Guinea|675
PY|Paraguay|595
PE|Peru|51
PH|Philippines|63
PN|Pitcairn|64
PL|Poland|48
PT|Portugal|351
PR|Puerto Rico|1787
QA|Qatar|974
RE|Réunion|262262
RO|Romania|40
RU|Russia|7
RW|Rwanda|250
BL|Saint Barthélemy|590
SH|Saint Helena|290
KN|Saint Kitts and Nevis|1869
LC|Saint Lucia|1758
MF|Saint Martin|590
PM|Saint Pierre and Miquelon|508
VC|Saint Vincent and the Grenadines|1784
WS|Samoa|685
SM|San Marino|378
ST|Sao Tome and Principe|239
SA|Saudi Arabia|966
SN|Senegal|221
RS|Serbia|381
SC|Seychelles|248
SL|Sierra Leone|232
SG|Singapore|65
SX|Sint Maarten|1721
SK|Slovakia|421
SI|Slovenia|386
SB|Solomon Islands|677
SO|Somalia|252
ZA|South Africa|27
GS|South Georgia and South Sandwich Islands|
SS|South Sudan|211
ES|Spain|34
LK|Sri Lanka|94
SD|Sudan|249
SR|Suriname|597
SJ|Svalbard and Jan Mayen|4779
SE|Sweden|46
CH|Switzerland|41
SY|Syria|963
TW|Taiwan|886
TJ|Tajikistan|992
TZ|Tanzania|255
TH|Thailand|66
TL|Timor-Leste|670
TG|Togo|228
TK|Tokelau|690
TO|Tonga|676
TT|Trinidad and Tobago|1868
TN|Tunisia|216
TR|Türkiye|90
TM|Turkmenistan|993
TC|Turks and Caicos Islands|1649
TV|Tuvalu|688
UG|Uganda|256
UA|Ukraine|380
AE|United Arab Emirates|971
GB|United Kingdom|44
US|United States|1
UM|United States Minor Outlying Islands|
UY|Uruguay|598
UZ|Uzbekistan|998
VU|Vanuatu|678
VE|Venezuela|58
VN|Viet Nam|84
VG|British Virgin Islands|1284
VI|U.S. Virgin Islands|1340
WF|Wallis and Futuna|681
EH|Western Sahara|212
YE|Yemen|967
ZM|Zambia|260
ZW|Zimbabwe|263
""".strip().splitlines()


def _flag_from_iso(iso: str) -> str:
    """Generate the standard Unicode flag emoji for an ISO alpha-2 code."""
    return "".join(chr(0x1F1E6 + ord(char) - ord("A")) for char in iso.upper())


def _load_country_flags_from_data_file() -> dict[str, str]:
    """Read valid flag emoji from the uploaded data-negara file when present.

    The uploaded file is a data fragment rather than an importable Python
    module, so it is intentionally parsed as text. Invalid/truncated entries
    are ignored and receive the ISO-generated flag below.
    """
    data_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "data_negara__1786941194381.txt",
    )
    try:
        with open(data_path, encoding="utf-8") as data_file:
            raw = data_file.read()
    except (OSError, UnicodeError):
        return {}

    flags = {}
    pattern = re.compile(
        r'"flag"\s*:\s*"(?P<flag>[^"]+)"\s*,\s*"iso"\s*:\s*"(?P<iso>[A-Z]{2})"'
    )
    for match in pattern.finditer(raw):
        flag = match.group("flag")
        if len(flag) == 2 and all(
            0x1F1E6 <= ord(char) <= 0x1F1FF for char in flag
        ):
            flags[match.group("iso")] = flag
    return flags


_COUNTRY_FLAGS_FROM_DATA = _load_country_flags_from_data_file()
COUNTRY_DIRECTORY = {}
COUNTRY_CODES = {}

for _row in _COUNTRY_CATALOG_ROWS:
    _iso, _name, _prefixes = _row.split("|", 2)
    _flag = _COUNTRY_FLAGS_FROM_DATA.get(_iso) or _flag_from_iso(_iso)
    COUNTRY_DIRECTORY[_iso] = {
        "name": _name,
        "flag": _flag,
        "iso": _iso,
    }
    for _prefix in filter(None, _prefixes.split(",")):
        # Keep the first entry for shared codes; more specific prefixes are
        # checked first by get_country_info(), so NANP overlays remain useful.
        COUNTRY_CODES.setdefault(
            _prefix,
            (_name, _flag, _iso),
        )

# Shared country calling code fallbacks. These are deliberately explicit:
# +1 and +7 alone cannot identify every country without the following digits.
COUNTRY_CODES["1"] = (
    COUNTRY_DIRECTORY["US"]["name"],
    COUNTRY_DIRECTORY["US"]["flag"],
    "US",
)
COUNTRY_CODES["7"] = (
    COUNTRY_DIRECTORY["RU"]["name"],
    COUNTRY_DIRECTORY["RU"]["flag"],
    "RU",
)

SERVICE_ASSETS = {
    "WS": {"premium_id": "5334998226636390258"}, "TG": {"premium_id": "5330237710655306682"},
    "FB": {"premium_id": "5323261730283863478"}, "GO": {"premium_id": "5359758030198031389"},
    "IG": {"premium_id": "5319160079465857105"}, "TT": {"premium_id": "5327982530702359565"},
    "TW": {"premium_id": "5330337435500951363"}, "SC": {"premium_id": "5330248916224983855"},
    "DC": {"premium_id": "5325612636467903082"}, "MS": {"premium_id": "5370857634440170316"},
    "AP": {"premium_id": "5334955749409834455"}, "BG": {"premium_id": "5373265917092316632"},
    "WC": {"premium_id": "5332524123610430820"}, "IMO": {"premium_id": "5334954057192719331"},
    "NF": {"premium_id": "5323261730283863478"}, "SP": {"premium_id": "5197645099495862838"},
    "LA": {"premium_id": "5229011542011299168"}, "TN": {"premium_id": "5458603043203327669"},
    "MI": {"premium_id": "5217824874487101321"}, "OT": {"premium_id": "5197645099495862838"},
}

FLAG_EMOJI_PREMIUM = {
    "AE": "5296750159886044083", "AF": "5296313086834131752", "AL": "5296613073119888465",
    "AM": "5294424778692643941", "AR": "5296543610613811920", "AT": "5296542940598914203",
    "AU": "5294188899088744731", "AZ": "5300875858225946690", "BA": "5294273286606175399",
    "BD": "5294438230530212180", "BE": "5294332763313292517", "BG": "5294329219965272288",
    "BH": "5296574792076378414", "BN": "5293996845331138180", "BO": "5296756013926466928",
    "BR": "5296467808736004436", "BY": "5294208398240269676", "CA": "5294096424147896760",
    "CH": "5294416459340989591", "CL": "5296514988951750110", "CM": "5294445540564551759",
    "CN": "5294541717767213404", "CO": "5294111658396895748", "CZ": "5294325002307388969",
    "DE": "5296711651209267465", "DK": "5294392875675567914", "DZ": "5294387425362068411",
    "EE": "5296631361090635271", "EG": "5294235727117172303", "ES": "5294124040787608668",
    "ET": "5296664814590904151", "FI": "5294245382203657313", "FR": "5294338402605354650",
    "GB": "5294197287159876240", "GE": "5296748025287295377", "GH": "5294212194991359368",
    "GR": "5294030651018735906", "HR": "5296728624920020374", "HU": "5294297883883884281",
    "ID": "5294260771071476936", "IE": "5294445119657754986", "IL": "5294435906952907122",
    "IN": "5294495881876229590", "IQ": "5296720490251961770", "IR": "5294443526224889744",
    "IT": "5294096883709398908", "JP": "5296431112535426565", "KE": "5296643232380240561",
    "KG": "5296272894530175743", "KH": "5294475326162751271", "KR": "5293985257509375078",
    "KW": "5294449350200545798", "KZ": "5294048281859475822", "LB": "5294526453453439797",
    "LK": "5294116309846475631", "LT": "5296529823768790124", "LU": "5294282945987631258",
    "LV": "5294434485318733105", "LY": "5293995067214678723", "MA": "5296322948079044611",
    "MD": "5296516174362724142", "MN": "5293985068530813794", "MX": "5296789308512946569",
    "MY": "5294132501873182584", "NG": "5294062227618286918", "NL": "5294199065276335626",
    "NO": "5296763869421650829", "NP": "5296678678745334353", "NZ": "5294233523798954545",
    "OM": "5294203716725918231", "PH": "5296542068720551671", "PK": "5294165538761622390",
    "PL": "5294220982494447717", "PT": "5294442353698820401", "QA": "5296274745661082738",
    "RO": "5296461881681135191", "RS": "5294178423663510137", "RU": "5296288442311788486",
    "RW": "5294178784440765714", "SA": "5296440716082300772", "SD": "5294282658224816336",
    "SE": "5294412516561013997", "SG": "5296652067127967695", "SK": "5296572940945476108",
    "SN": "5296684816253603762", "SO": "5296337366284255780", "TH": "5294112929707215629",
    "TJ": "5294223963201752433", "TM": "5294468643193635490", "TR": "5278402355950277602",
    "TZ": "5294310596987079349", "UA": "5301210178480259258", "UG": "5294025488468036779",
    "UN": "5366352372660462802", "US": "5294355805812834147", "UZ": "5294243788770794963",
    "VE": "5276346818962147941", "VN": "5294101771382179258", "YE": "5294141521304506242",
    "ZA": "5294057760852296487", "ZM": "5296573559420766393", "ZW": "5294073497612471552",
}

C_GREEN = '\033[92m'; C_RED = '\033[91m'; C_CYAN = '\033[96m'; C_YELLOW = '\033[93m'; C_RESET = '\033[0m'

# ─── File paths ──────────────────────────────────────────────────────────────────
LOG_FILE   = "otp_log.txt"
STATS_FILE = "stats.json"

# ═══════════════════════════════════════════════════════════════════════════════════
# 📝 LOG FILE — simpan semua OTP ke otp_log.txt
# ═══════════════════════════════════════════════════════════════════════════════════

def log_otp(acc_name: str, sender: str, range_name: str, template: str, otp: str, message: str):
    """Tulis baris log ke otp_log.txt."""
    ts   = now_wib().strftime("%Y-%m-%d %H:%M:%S WIB")
    line = f"[{ts}] [{acc_name}] {sender} | {range_name} | {template} | OTP: {otp} | {message}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"{C_YELLOW}[LOG] Gagal tulis log: {e}{C_RESET}")

# ═══════════════════════════════════════════════════════════════════════════════════
# 📊 STATISTIK HARIAN
# ═══════════════════════════════════════════════════════════════════════════════════

def _load_stats() -> dict:
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE) as f:
                return json.load(f)
        except: pass
    return {}

def _save_stats(data: dict):
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"{C_YELLOW}[STATS] Gagal simpan: {e}{C_RESET}")

def record_stat(sender: str, country: str):
    """Catat 1 OTP masuk ke statistik hari ini."""
    today = now_wib().strftime("%Y-%m-%d")
    data  = _load_stats()
    day   = data.setdefault(today, {"total": 0, "by_sender": {}, "by_country": {}})
    day["total"] += 1
    day["by_sender"][sender]   = day["by_sender"].get(sender, 0) + 1
    day["by_country"][country] = day["by_country"].get(country, 0) + 1
    _save_stats(data)

def build_stats_text(date_str: str | None = None) -> str:
    """Buat teks laporan statistik untuk tanggal tertentu (default hari ini)."""
    today = date_str or now_wib().strftime("%Y-%m-%d")
    data  = _load_stats()
    day   = data.get(today)
    if not day:
        return f"📊 <b>Statistik {today}</b>\n\nBelum ada data OTP hari ini."

    total = day.get("total", 0)
    by_sender  = sorted(day.get("by_sender", {}).items(),  key=lambda x: -x[1])
    by_country = sorted(day.get("by_country", {}).items(), key=lambda x: -x[1])

    lines = [f"📊 <b>Statistik OTP — {today}</b>\n", f"📨 Total OTP : <b>{total}</b>\n"]

    if by_sender:
        lines.append("📤 <b>Top Sender:</b>")
        for s, c in by_sender[:10]:
            bar = "▓" * min(c, 20)
            lines.append(f"   {bar} <code>{s}</code> — {c}")

    if by_country:
        lines.append("\n🌍 <b>Top Negara:</b>")
        for cc, c in by_country[:10]:
            lines.append(f"   🔹 {cc} — {c}")

    return "\n".join(lines)

async def send_daily_stats(app):
    """Kirim laporan harian ke CHAT_ID utama."""
    text = build_stats_text()
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")
        print(f"{C_GREEN}[STATS] Laporan harian terkirim{C_RESET}")
    except Exception as e:
        print(f"{C_YELLOW}[STATS] Gagal kirim laporan: {e}{C_RESET}")

async def daily_stats_loop(app):
    """Task background: kirim statistik setiap tengah malam (00:00 WIB / UTC+7)."""
    print(f"{C_GREEN}[STATS] Daily stats loop aktif{C_RESET}")
    while True:
        now     = now_wib()
        # Hitung detik sampai 00:00 berikutnya
        tomorrow = now.replace(hour=0, minute=0, second=5, microsecond=0)
        from datetime import timedelta
        if tomorrow <= now:
            tomorrow += timedelta(days=1)
        wait_sec = (tomorrow - now).total_seconds()
        await asyncio.sleep(wait_sec)
        await send_daily_stats(app)

# ═══════════════════════════════════════════════════════════════════════════════════
# ✏️ FORMAT PESAN CUSTOM
# ═══════════════════════════════════════════════════════════════════════════════════

DEFAULT_FORMAT = "premium"   # gunakan format premium bawaan

def get_custom_format() -> str:
    """Ambil template format dari settings. Kosong = pakai premium default."""
    s = load_settings()
    return s.get("otp_format", DEFAULT_FORMAT)

def apply_custom_format(template: str, *, flag: str, country: str, service: str,
                        number: str, otp: str, sender: str, range_name: str, message: str) -> str:
    """
    Terapkan template custom. Placeholder yang didukung:
      {flag}     → emoji bendera negara
      {country}  → kode ISO negara (PK, ID, dll)
      {service}  → nama layanan (Apple, Google, WA, dll)
      {number}   → nomor yang dimasked (+9230✨1234)
      {otp}      → kode OTP
      {sender}   → source/sender name
      {range}    → nama range (Pakistan - Jazz)
      {message}  → isi pesan lengkap
      {time}     → waktu sekarang
    """
    return (template
            .replace("{flag}",    flag)
            .replace("{country}", country)
            .replace("{service}", service)
            .replace("{number}",  number)
            .replace("{otp}",     otp)
            .replace("{sender}",  sender)
            .replace("{range}",   range_name)
            .replace("{message}", message)
            .replace("{time}",    now_wib().strftime("%H:%M:%S WIB")))

FORMAT_HELP = (
    "✏️ <b>Format Pesan Custom</b>\n\n"
    "Placeholder yang bisa dipakai:\n"
    "  <code>{flag}</code>    — 🇵🇰 emoji bendera\n"
    "  <code>{country}</code> — PK, ID, US, dll\n"
    "  <code>{service}</code> — Apple, Google, WA\n"
    "  <code>{number}</code>  — nomor (masked)\n"
    "  <code>{otp}</code>     — kode OTP\n"
    "  <code>{sender}</code>  — nama sender\n"
    "  <code>{range}</code>   — nama range\n"
    "  <code>{message}</code> — isi pesan\n"
    "  <code>{time}</code>    — waktu kirim\n\n"
    "Ketik template <b>teks biasa</b> seperti contoh di atas, <b>atau</b> tempel "
    "<b>JSON</b> untuk mengatur teks + tombol sekaligus (lihat <code>/formatjson</code> "
    "untuk contoh lengkap). Ketik <code>reset</code> untuk kembali ke format premium bawaan.\n\n"
    "✨ <b>Emoji premium:</b> tempel emoji premium LANGSUNG di mana pun dalam "
    "template (teks biasa maupun JSON) — ID-nya otomatis terdeteksi & tersimpan, "
    "tidak perlu cari ID manual.\n"
    "⚠️ JANGAN bungkus placeholder yang berisi emoji premium "
    "(<code>{flag}</code>/<code>{service}</code>/<code>{number}</code>/"
    "<code>{number_masked}</code>/<code>{svc_icon}</code>) di dalam tag "
    "<code>&lt;code&gt;</code> atau <code>&lt;pre&gt;</code> — Telegram tidak "
    "izinkan tag bersarang di situ, emoji premium jadi gagal tampil.\n\n"
    "<b>Contoh teks:</b>\n"
    "<code>{flag} {country} | {service} | OTP: {otp}\n{message}</code>"
)

# ─── Template JSON (teks + tombol custom) ───────────────────────────────────────
# Memungkinkan admin mengatur teks pesan DAN susunan/warna tombol sekaligus lewat
# satu JSON, contoh:
# {
#   "text": "{flag} <b>{iso}</b> | {svc_icon} | <b>{number_masked}</b>",
#   "buttons": [
#     {"type": "otp",  "label": "🔑 {otp}", "value": "{otp}", "style": "success", "icon": "5465443379917629504"},
#     {"type": "sep"},
#     {"type": "link", "label": "☎️ Number", "value": "https://t.me/xxx", "style": "primary"},
#     {"type": "link", "label": "🔔 Chanel",  "value": "https://t.me/yyy", "style": "primary"}
#   ]
# }
# Tipe tombol yang didukung:
#   "otp"  → tombol copy-text berisi kode OTP
#   "copy" → tombol copy-text generik (pakai field "value" bebas, mis. {message})
#   "link" → tombol yang membuka URL (field "value" = link)
#   "sep"  → bukan tombol, hanya penanda pindah ke baris baru
# Field warna & emoji per tombol (fitur asli Telegram Bot API 9.4+, BUKAN emoji teks):
#   "style" → warna latar tombol. HANYA 3 nilai resmi Telegram yang didukung:
#             "primary" (biru), "success" (hijau), "danger" (merah).
#             Nilai lain (mis. "secondary"/"warning"/typo) otomatis dipetakan ke
#             warna terdekat, tidak akan membuat request ditolak Telegram.
#   "icon"  → ID emoji premium/custom emoji Telegram (field resmi:
#             icon_custom_emoji_id) yang tampil sebagai ikon di depan teks tombol.
#             Ambil ID-nya lewat bot @tg lain (mis. forward emoji premium ke bot
#             pengecek ID) -- harus berupa custom emoji document ID yang valid.
# Semua field "label"/"value"/"text" boleh memakai placeholder yang sama seperti
# format teks biasa, ditambah: {iso} (alias {country}), {svc} (kode layanan mentah,
# mis. "TG"), {svc_icon} (ikon layanan/emoji premium), {number_masked} (alias {number}).

def get_otp_json_template() -> dict | None:
    """Ambil template JSON (teks+tombol) dari settings. None jika belum diset."""
    s = load_settings()
    tpl = s.get("otp_json_template")
    return tpl if isinstance(tpl, dict) else None

def build_otp_placeholders(*, flag_html: str, flag_raw: str, c_iso: str, svc_icon_html: str,
                            short_cli: str, source: str, masked_html: str, number_raw: str,
                            otp: str, range_name: str, message: str) -> tuple[dict, dict]:
    """Bangun 2 dict placeholder: satu untuk teks (HTML, sudah di-escape), satu untuk
    label/value tombol (plain text, TIDAK boleh mengandung tag HTML)."""
    now = now_wib().strftime("%H:%M:%S WIB")
    text_ph = {
        "flag": flag_html, "iso": html.escape(c_iso), "country": html.escape(c_iso),
        "svc_icon": svc_icon_html, "svc": html.escape(short_cli), "service": html.escape(source),
        "number_masked": masked_html, "number": masked_html,
        "otp": html.escape(otp), "sender": html.escape(source),
        "range": html.escape(range_name), "message": html.escape(message), "time": now,
    }
    raw_ph = {
        "flag": flag_raw, "iso": c_iso, "country": c_iso,
        "svc_icon": short_cli, "svc": short_cli, "service": source,
        "number_masked": number_raw, "number": number_raw,
        "otp": otp, "sender": source,
        "range": range_name, "message": message, "time": now,
    }
    return text_ph, raw_ph

def _apply_placeholders(template: str, ph: dict) -> str:
    out = template
    for key, val in ph.items():
        out = out.replace("{" + key + "}", val)
    return out

def render_otp_json_template(tpl: dict, text_ph: dict, raw_ph: dict):
    """Render template JSON menjadi (text, InlineKeyboardMarkup|None).
    Baris baru pada tombol ditandai dengan {"type": "sep"}.
    Setiap tombol mendukung "style" (warna asli tombol Telegram: primary/success/
    danger) dan "icon" (ID emoji premium/custom emoji yang tampil di depan teks
    tombol, field resmi Telegram: icon_custom_emoji_id)."""
    text = _apply_placeholders(str(tpl.get("text", "")), text_ph)
    rows, current = [], []
    for b in tpl.get("buttons", []) or []:
        if not isinstance(b, dict):
            continue
        btype = str(b.get("type", "link")).lower()
        if btype == "sep":
            if current:
                rows.append(current); current = []
            continue
        label = _apply_placeholders(str(b.get("label", "")), raw_ph)
        style = _normalize_style(b.get("style"))
        # "icon" atau "icon_custom_emoji_id" -- ID emoji premium (angka/string dari
        # Telegram Premium sticker/emoji pack). Boleh pakai placeholder juga.
        icon_raw = b.get("icon") or b.get("icon_custom_emoji_id")
        icon = _apply_placeholders(str(icon_raw), raw_ph).strip() if icon_raw else None
        value = _apply_placeholders(str(b.get("value", "")), raw_ph)
        if btype == "otp":
            if not value:
                value = raw_ph.get("otp", "")
            current.append(_copy_btn(value, label, api_kwargs=_style_kwargs(style, icon)))
        elif btype == "copy":
            current.append(_copy_btn(value, label, api_kwargs=_style_kwargs(style, icon)))
        elif btype == "link":
            current.append(btn(label, url=value, style=style, icon=icon))
        # tipe tak dikenal -> diabaikan (tidak bikin crash)
    if current:
        rows.append(current)
    markup = InlineKeyboardMarkup(rows) if rows else None
    return text, markup

# ═══════════════════════════════════════════════════════════════════════════════════
# 🚨 ALERT — Notifikasi akun API tidak aktif
# ═══════════════════════════════════════════════════════════════════════════════════

ALERT_THRESHOLD = 3        # Berapa kali gagal berturut-turut sebelum kirim alert
ALERT_COOLDOWN  = 300      # Detik jeda antar alert agar tidak spam (5 menit)

# State per akun: { acc_name: { "fail_count": int, "alerted": bool, "last_alert_ts": float } }
_account_health: dict = {}
_worker_tasks: dict[str, asyncio.Task] = {}
_worker_wake_events: dict[str, asyncio.Event] = {}
_worker_config_events: dict[str, asyncio.Event] = {}
_worker_runtime: dict[str, dict] = {}
_worker_app = None
_api_last_request_at: dict[str, float] = {}
_api_throttle_locks: dict[str, asyncio.Lock] = {}
_api_rate_limited_until: dict[str, float] = {}
_incoming_sms_lock = asyncio.Lock()
_incoming_sms_inflight: set[str] = set()
_webhook_queue: asyncio.Queue[tuple[dict, str]] = asyncio.Queue(
    maxsize=WEBHOOK_QUEUE_MAXSIZE
)
_webhook_worker_task: asyncio.Task | None = None

def _health(acc_name: str) -> dict:
    """Ambil atau buat state health untuk akun tertentu."""
    if acc_name not in _account_health:
        _account_health[acc_name] = {"fail_count": 0, "alerted": False, "last_alert_ts": 0.0}
    return _account_health[acc_name]

def _account_status(name: str) -> tuple[str, dict]:
    state = _worker_runtime.setdefault(name, {
        "status": "stopped",
        "last_error": "",
        "started_at": "",
        "rate_limited_until": 0.0,
        "last_poll_at": 0.0,
        "last_success_at": 0.0,
        "next_poll_at": 0.0,
        "last_latency_ms": 0,
        "last_http_status": 0,
        "success_count": 0,
        "error_count": 0,
    })
    task = _worker_tasks.get(name)
    if task and task.done() and state["status"] == "running":
        state["status"] = "stopped"
    return state["status"], state

def _format_when(timestamp: float) -> str:
    if not timestamp:
        return "belum ada"
    elapsed = max(0, int(time.time() - timestamp))
    if elapsed < 5:
        return "baru saja"
    if elapsed < 60:
        return f"{elapsed} dtk lalu"
    if elapsed < 3600:
        return f"{elapsed // 60} mnt lalu"
    return datetime.fromtimestamp(timestamp, WIB).strftime("%d %b %H:%M WIB")

def _format_countdown(timestamp: float) -> str:
    if not timestamp:
        return "tidak dijadwalkan"
    remaining = int(timestamp - time.time())
    if remaining <= 0:
        return "segera"
    if remaining < 60:
        return f"{remaining} dtk lagi"
    return f"{remaining // 60} mnt {remaining % 60} dtk lagi"

def _account_today_otp_count(account_name: str) -> int:
    """Read today's per-account count from the existing durable OTP log."""
    if not os.path.exists(LOG_FILE):
        return 0
    today = now_wib().strftime("%Y-%m-%d")
    marker = f"] [{account_name}]"
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if line.startswith(f"[{today}") and marker in line)
    except OSError:
        return 0

def _status_label(name: str, state: dict, api_key: str = "") -> str:
    status = state.get("status", "stopped")
    if status == "rate_limited":
        wait = _rate_limit_wait(name, api_key)
        return f"⏳ Rate limited ({wait} dtk)" if wait else "🟢 Running"
    return {
        "running": "🟢 Running",
        "starting": "🟡 Starting",
        "stopping": "🟠 Stopping",
        "error": "🔴 Error",
        "stopped": "⚪ Stopped",
    }.get(status, f"⚪ {status.title()}")

def _runtime_for_account(account: dict) -> dict:
    """Return runtime metrics without putting credentials into runtime state."""
    return _account_status(str(account.get("name", "")))[1]

def _api_key_id(api_key: str) -> str:
    """In-memory key used for throttling; never log or expose the key."""
    return api_key.strip()

async def _wait_for_api_slot(api_key: str) -> None:
    """Honor a server cooldown without overriding the configured poll interval.

    The worker's poll_interval controls normal polling cadence. An optional
    API_MIN_REQUEST_GAP_SECONDS can add spacing for installations that need it;
    it defaults to zero because Augestel's 65-second delay must not be forced
    on every account.
    """
    key_id = _api_key_id(api_key)
    lock = _api_throttle_locks.setdefault(key_id, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        next_allowed = max(
            _api_last_request_at.get(key_id, 0.0) + API_MIN_REQUEST_GAP_SECONDS,
            _api_rate_limited_until.get(key_id, 0.0),
        )
        if next_allowed > now:
            await asyncio.sleep(next_allowed - now)
        _api_last_request_at[key_id] = time.monotonic()

def _mark_rate_limit(account_name: str, retry_after: int | float | None, api_key: str = "") -> int:
    """Remember an API cooldown so polling and dashboard calls share it."""
    try:
        seconds = max(1, int(float(retry_after or 60)))
    except (TypeError, ValueError):
        seconds = 60
    state = _account_status(account_name)[1]
    state["rate_limited_until"] = max(
        float(state.get("rate_limited_until", 0.0)),
        time.monotonic() + seconds,
    )
    if api_key:
        key_id = _api_key_id(api_key)
        _api_rate_limited_until[key_id] = max(
            _api_rate_limited_until.get(key_id, 0.0),
            time.monotonic() + seconds,
        )
    return seconds

def _rate_limit_wait(account_name: str, api_key: str = "") -> int:
    account_until = float(_account_status(account_name)[1].get("rate_limited_until", 0.0))
    key_id = _api_key_id(api_key) if api_key else ""
    key_until = _api_rate_limited_until.get(key_id, 0.0) if key_id else 0.0
    # Jangan tampilkan interval polling normal sebagai "rate limited".
    # Countdown hanya berasal dari cooldown HTTP 429/server.
    throttle_until = (
        _api_last_request_at.get(key_id, 0.0) + API_MIN_REQUEST_GAP_SECONDS
        if key_id
        else 0.0
    )
    remaining = max(account_until, key_until, throttle_until) - time.monotonic()
    return max(0, int(remaining + 0.999))

def get_active_account() -> dict | None:
    """Account used by dashboard/API buttons, independent from worker state."""
    accounts = load_accounts()
    if not accounts:
        return None
    active_name = str(load_settings().get("active_account", "")).strip()
    if active_name:
        for account in accounts:
            if account.get("name") == active_name:
                return account
    for account in accounts:
        if account.get("api_key"):
            return account
    return accounts[0]

async def _notify_admin(app, text: str):
    """Kirim DM ke ADMIN_ID. Juga kirim ke extra_admins."""
    targets = [ADMIN_ID] + load_settings().get("extra_admins", [])
    for uid in targets:
        try:
            await app.bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
        except Exception as e:
            print(f"{C_YELLOW}[ALERT] Gagal DM ke {uid}: {e}{C_RESET}")

async def on_poll_success(app, acc_name: str):
    """Dipanggil setiap polling berhasil (status 200). Reset counter & kirim notif pulih."""
    import time
    h = _health(acc_name)
    if h["alerted"]:
        # Akun baru saja pulih setelah sebelumnya di-alert
        h["alerted"]   = False
        h["fail_count"] = 0
        print(f"{C_GREEN}[ALERT] [{acc_name}] Pulih kembali ✅{C_RESET}")
        await _notify_admin(
            app,
            f"✅ <b>Akun Pulih</b>\n\n"
            f"Akun <code>{acc_name}</code> kembali aktif dan berhasil polling."
        )
    else:
        h["fail_count"] = 0

async def on_poll_failure(app, acc_name: str, reason: str):
    """Dipanggil setiap polling gagal. Kirim alert ke admin jika melebihi threshold."""
    import time
    h = _health(acc_name)
    h["fail_count"] += 1

    count     = h["fail_count"]
    alerted   = h["alerted"]
    last_ts   = h["last_alert_ts"]
    now       = time.time()
    cooldown_ok = (now - last_ts) >= ALERT_COOLDOWN

    if count >= ALERT_THRESHOLD and (not alerted or cooldown_ok):
        h["alerted"]       = True
        h["last_alert_ts"] = now
        print(f"{C_RED}[ALERT] [{acc_name}] Gagal {count}x → kirim DM ke admin{C_RESET}")
        await _notify_admin(
            app,
            f"🚨 <b>Akun API Tidak Aktif!</b>\n\n"
            f"📛 Akun  : <code>{acc_name}</code>\n"
            f"❌ Gagal : <b>{count}x</b> berturut-turut\n"
            f"⚠️ Error : <code>{reason}</code>\n\n"
            f"Periksa API key atau koneksi Augestel segera."
        )

# ─── Persistent sent-IDs (survive restart) ──────────────────────────────────────

SENT_IDS_FILE = "sent_ids.json"
MAX_SENT_IDS  = 100_000          # jaga ukuran file tetap wajar

_sent_ids_cache: set | None = None   # None = file belum ada (pertama kali jalan)

def _load_sent_ids_once() -> set | None:
    """
    Baca sent_ids.json sekali. Return:
      - set of str  → file ada, gunakan isinya
      - None        → file belum ada (first-ever run, jangan forward apapun)
    """
    global _sent_ids_cache
    if _sent_ids_cache is not None:
        return _sent_ids_cache
    if os.path.exists(SENT_IDS_FILE):
        try:
            with open(SENT_IDS_FILE) as f:
                _sent_ids_cache = set(json.load(f))
                print(f"[INFO] Loaded {len(_sent_ids_cache):,} sent IDs dari {SENT_IDS_FILE}")
                return _sent_ids_cache
        except Exception as e:
            print(f"[WARN] Gagal baca {SENT_IDS_FILE}: {e}")
            _sent_ids_cache = set()
            return _sent_ids_cache
    return None   # file belum ada

def _persist_sent_id(sms_id: str):
    """Tambahkan ID ke cache + tulis ulang file (immediate flush)."""
    global _sent_ids_cache
    if _sent_ids_cache is None:
        _sent_ids_cache = set()
    _sent_ids_cache.add(sms_id)
    # Batasi agar file tidak membengkak
    if len(_sent_ids_cache) > MAX_SENT_IDS:
        excess = len(_sent_ids_cache) - MAX_SENT_IDS
        to_remove = list(_sent_ids_cache)[:excess]
        _sent_ids_cache -= set(to_remove)
    try:
        with open(SENT_IDS_FILE, "w") as f:
            json.dump(list(_sent_ids_cache), f)
    except Exception as e:
        print(f"[WARN] Gagal simpan {SENT_IDS_FILE}: {e}")

def _init_sent_ids_first_run():
    """Inisialisasi sent_ids.json kosong saat pertama kali jalan (forward semua OTP yang ada)."""
    global _sent_ids_cache
    _sent_ids_cache = set()
    try:
        with open(SENT_IDS_FILE, "w") as f:
            json.dump([], f)
        print(f"[INFO] First-ever run: sent_ids.json dibuat kosong, akan forward semua OTP yang ada.")
    except Exception as e:
        print(f"[WARN] Gagal inisialisasi {SENT_IDS_FILE}: {e}")

def _webhook_signature_matches(
    request: web.Request,
    raw_body: bytes,
    secret: str,
) -> bool:
    """Validate an Augestel HMAC signature against one candidate secret."""
    secret = str(secret or "").strip()
    if not secret:
        return False

    provided = request.headers.get("X-Augestel-Signature", "").strip()
    if not provided.lower().startswith("sha256="):
        return False

    provided_value = provided.split("=", 1)[1].strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", provided_value):
        return False

    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided_value.lower(), expected.lower())


def _webhook_signature_is_valid(request: web.Request, raw_body: bytes) -> bool:
    """Backward-compatible check for the legacy global webhook secret."""
    return _webhook_signature_matches(request, raw_body, WEBHOOK_SECRET)


def _webhook_timestamp_is_valid(value: str) -> bool:
    """Reject missing, timezone-less, stale, or malformed webhook timestamps."""
    timestamp = str(value or "").strip()
    if not timestamp or len(timestamp) > 128:
        return False
    try:
        normalized = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return False
        age = abs(
            (
                datetime.now(timezone.utc)
                - parsed.astimezone(timezone.utc)
            ).total_seconds()
        )
        return age <= WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS
    except (TypeError, ValueError, OverflowError):
        return False


async def _claim_incoming_event(event_keys: set[str]) -> bool:
    """Reserve all known message aliases before acknowledging the webhook."""
    if not event_keys:
        return False
    async with _incoming_sms_lock:
        if _load_sent_ids_once() is None:
            _init_sent_ids_first_run()
        if event_keys.intersection(_sent_ids_cache or set()):
            return False
        if event_keys.intersection(_incoming_sms_inflight):
            return False
        _incoming_sms_inflight.update(event_keys)
        return True


async def _release_incoming_event(event_keys: set[str] | str) -> None:
    if isinstance(event_keys, str):
        event_keys = {event_keys}
    if event_keys:
        async with _incoming_sms_lock:
            _incoming_sms_inflight.difference_update(event_keys)


def _webhook_account_name(request: web.Request, payload: dict) -> str:
    """Resolve a useful internal account label for webhook logs."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    candidates = [
        request.headers.get("X-Augestel-Account", ""),
        request.headers.get("X-Account-Name", ""),
        data.get("account_name", ""),
        data.get("account", ""),
    ]
    if WEBHOOK_ACCOUNT_NAME:
        candidates.insert(0, WEBHOOK_ACCOUNT_NAME)
    accounts = load_accounts()
    known = {str(a.get("name", "")).strip() for a in accounts}
    for candidate in candidates:
        name = str(candidate).strip()
        if name and name in known:
            return name
    if len(accounts) == 1:
        return str(accounts[0].get("name", "Augestel")).strip() or "Augestel"
    return WEBHOOK_ACCOUNT_NAME or "Augestel Webhook"


def _webhook_account_hint(request: web.Request, payload: dict) -> str:
    """Read an optional account label sent by the webhook provider."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    candidates = [
        request.headers.get("X-Augestel-Account", ""),
        request.headers.get("X-Account-Name", ""),
        data.get("account_name", ""),
        data.get("account", ""),
    ]
    known = {
        str(account.get("name", "")).strip()
        for account in load_accounts()
        if str(account.get("name", "")).strip()
    }
    for candidate in candidates:
        name = str(candidate).strip()
        if name and name in known:
            return name
    return ""


def _webhook_signature_account(
    request: web.Request,
    raw_body: bytes,
    payload: dict,
) -> str | None:
    """Verify a webhook and resolve the account that owns its secret."""
    accounts = load_accounts()
    hint = _webhook_account_hint(request, payload)

    if hint:
        account = next(
            (item for item in accounts if str(item.get("name", "")).strip() == hint),
            None,
        )
        if account:
            account_secret = str(account.get("webhook_secret", "")).strip()
            candidates = [account_secret] if account_secret else []
            # Keep the old global secret as a migration fallback for accounts
            # created before per-account secrets existed.
            if WEBHOOK_SECRET and not account_secret:
                candidates.append(WEBHOOK_SECRET)
            if any(
                _webhook_signature_matches(request, raw_body, secret)
                for secret in candidates
            ):
                return hint
            return None

    matches = [
        str(account.get("name", "")).strip()
        for account in accounts
        if str(account.get("name", "")).strip()
        and _webhook_signature_matches(
            request, raw_body, str(account.get("webhook_secret", "")).strip()
        )
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        configured_name = str(WEBHOOK_ACCOUNT_NAME).strip()
        return configured_name if configured_name in matches else None

    if WEBHOOK_SECRET and _webhook_signature_matches(request, raw_body, WEBHOOK_SECRET):
        if len(accounts) == 1:
            return str(accounts[0].get("name", "")).strip() or "Augestel"
        configured_name = str(WEBHOOK_ACCOUNT_NAME).strip()
        if configured_name and any(
            str(account.get("name", "")).strip() == configured_name
            for account in accounts
        ):
            return configured_name
        if not accounts:
            return WEBHOOK_ACCOUNT_NAME or "Augestel Webhook"
    return None


def _stable_sms_id(source: str, number: str, message: str, received_at: str) -> str:
    """Build the same fallback ID for webhook and polling deliveries."""
    stable = {
        "event": "message.received",
        "source": str(source).strip(),
        "number": str(number).strip(),
        "message": str(message).strip(),
        "received_at": str(received_at).strip(),
    }
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _incoming_dedup_keys(row: dict) -> set[str]:
    """Build aliases shared by webhook and polling representations of one SMS."""
    source = " ".join(str(row.get("source", "")).strip().split()).casefold()
    number = re.sub(r"[^0-9]", "", str(
        row.get("number")
        or row.get("phone_number")
        or row.get("recipient")
        or row.get("range_name")
        or ""
    ))
    message = " ".join(str(row.get("message", "")).strip().split()).casefold()
    received_at = str(row.get("received_at", "")).strip()
    event_id = str(row.get("id", "")).strip()
    keys: set[str] = set()
    if event_id:
        # Keep the unprefixed form for compatibility with existing sent_ids.json.
        keys.update({event_id, f"event:{event_id}"})

    if source and number and message:
        if received_at:
            keys.add(
                "fingerprint:" + _stable_sms_id(
                    source, number, message, received_at
                )
            )
        try:
            normalized = received_at[:-1] + "+00:00" if received_at.endswith("Z") else received_at
            timestamp = datetime.fromisoformat(normalized)
            day = timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OverflowError):
            day = now_wib().strftime("%Y-%m-%d")
        content = {
            "source": source,
            "number": number,
            "message": message,
            "day": day,
        }
        keys.add(
            "content-day:" + hashlib.sha256(
                json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
    return keys


def _webhook_event_id(payload: dict, raw_body: bytes) -> str:
    """Use provider IDs when present, otherwise hash stable message fields."""
    for key in ("id", "event_id", "webhook_id", "delivery_id"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    data = payload.get("data")
    if isinstance(data, dict) and data.get("id") is not None:
        if str(data["id"]).strip():
            return str(data["id"]).strip()
    # Providers may retry with equivalent JSON serialized differently. Hash the
    # stable message fields instead of raw bytes so that whitespace/key order
    # changes cannot produce a second Telegram message.
    if isinstance(data, dict):
        return _stable_sms_id(
            str(data.get("source", "")).strip(),
            str(
                data.get("number")
                or data.get("phone_number")
                or data.get("recipient")
                or ""
            ).strip(),
            str(data.get("message", "")).strip(),
            str(
                data.get("received_at") or payload.get("timestamp") or ""
            ).strip(),
        )
    return hashlib.sha256(raw_body).hexdigest()

def _webhook_row(payload: dict) -> tuple[str, dict] | tuple[None, None]:
    """Convert the documented event envelope to the bot's internal SMS row."""
    if str(payload.get("event", "")).strip().lower() != "message.received":
        return None, None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None, None
    source = str(data.get("source", "")).strip()
    message = str(data.get("message", "")).strip()
    number = str(
        data.get("number")
        or data.get("phone_number")
        or data.get("recipient")
        or ""
    ).strip()
    if not source or not message or not number:
        return None, None
    row = {
        "id": data.get("id") or payload.get("id") or "",
        "source": source,
        "number": number,
        "message": message,
        "range_name": str(data.get("range_name") or data.get("range") or "").strip(),
        "received_at": str(
            data.get("received_at") or payload.get("timestamp") or ""
        ).strip(),
        "status": str(data.get("status") or "").strip(),
        "type": str(data.get("type") or "").strip(),
        "rate": data.get("rate"),
    }
    return message, row

async def process_incoming_sms(
    app,
    row: dict,
    account_name: str,
    *,
    event_claimed: bool = False,
) -> tuple[str, str]:
    """Forward one SMS row exactly once.

    Returns `(status, event_id)` where status is `sent`, `duplicate`, `ignored`,
    or `failed`. The lock covers the check/send/mark sequence so two webhook
    retries arriving at the same time cannot both reach Telegram.
    """
    source = str(row.get("source", "")).strip()
    message = str(row.get("message", "")).strip()
    range_template = str(
        row.get("number") or row.get("range_name") or row.get("phone_number") or ""
    ).strip()
    received_at = str(row.get("received_at", "")).strip()
    event_id = str(row.get("id", "")).strip() or _stable_sms_id(
        source, range_template, message, received_at
    )
    if not source or not message or not event_id:
        return "ignored", event_id

    dedup_keys = _incoming_dedup_keys({**row, "id": event_id})
    async with _incoming_sms_lock:
        claimed_here = False
        if _load_sent_ids_once() is None:
            _init_sent_ids_first_run()
        if dedup_keys.intersection(_sent_ids_cache or set()):
            return "duplicate", event_id
        if not event_claimed:
            if dedup_keys.intersection(_incoming_sms_inflight):
                return "duplicate", event_id
            _incoming_sms_inflight.update(dedup_keys)
            claimed_here = True

        try:
            clean_rc = re.sub(r"[^0-9]", "", range_template)
            if len(clean_rc) < 4:
                clean_rc = re.sub(r"[^0-9]", "", source)
            if len(clean_rc) < 4:
                _persist_sent_id(event_id)
                return "ignored", event_id

            try:
                text_msg, kb_override, otp = build_final_otp_message(
                    source, message, str(row.get("range_name", "")).strip(), clean_rc
                )
                delivered = await send_to_telegram(
                    app, text_msg, otp, message, kb_override=kb_override
                )
                if not delivered:
                    raise RuntimeError("Tidak ada tujuan Telegram yang berhasil menerima pesan")
            except Exception as row_err:
                print(
                    f"{C_RED}[ERR] [{account_name}] Gagal proses OTP "
                    f"{event_id}: {row_err}{C_RESET}"
                )
                return "failed", event_id

            # Persist immediately after Telegram succeeds. This makes retries safe
            # even if log/stat writes fail afterward.
            for dedup_key in dedup_keys:
                _persist_sent_id(dedup_key)
            try:
                log_otp(
                    account_name,
                    source,
                    str(row.get("range_name", "")).strip(),
                    range_template,
                    otp,
                    message,
                )
                _, _, c_iso_stat = get_country_info(clean_rc)
                record_stat(source, c_iso_stat)
            except Exception as log_err:
                print(
                    f"{C_YELLOW}[WARN] [{account_name}] Gagal log/stat "
                    f"(OTP tetap terkirim): {log_err}{C_RESET}"
                )
            return "sent", event_id
        finally:
            if claimed_here or event_claimed:
                _incoming_sms_inflight.difference_update(dedup_keys)

# ─── Settings ────────────────────────────────────────────────────────────────────

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                return json.load(f)
        except: pass
    return {
        "groups": {},
        "allocation": {"rangeId": None, "quantity": 10},
        "extra_admins": [],
        "active_account": "",
    }

def save_settings(data):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=4)

def is_admin(user_id: int) -> bool:
    settings = load_settings()
    return user_id == ADMIN_ID or user_id in settings.get("extra_admins", [])

# ─── Augestel API helpers ────────────────────────────────────────────────────────

def _account_api_config(account: dict | None = None) -> tuple[str, str] | None:
    """Return the base URL and key for an account selected in the bot.

    API credentials deliberately live in account.json entries created by the
    admin flow, not in a panel-wide environment variable.  This keeps every
    polling worker isolated to its own account.
    """
    selected = account or get_active_account()
    if not selected:
        return None
    base = str(selected.get("base_url", "")).strip().rstrip("/")
    api_key = str(selected.get("api_key", "")).strip()
    if not base or not api_key:
        return None
    return base, api_key

async def panel_get(
    path: str,
    params: dict | None = None,
    account: dict | None = None,
    wait_for_slot: bool = False,
) -> dict | None:
    selected = account or get_active_account()
    config = _account_api_config(selected)
    if not config:
        return {
            "_error": "configuration",
            "_message": "Belum ada akun aktif dengan API key yang valid.",
        }
    base, api_key = config
    account_name = str(selected.get("name", "")) if selected else ""
    cooldown = _rate_limit_wait(account_name, api_key) if account_name else 0
    # Handler interaktif langsung mengembalikan status cooldown supaya tidak
    # menggantung. Worker download justru harus mengantre sampai slot API
    # tersedia; kalau tidak, polling rutin dapat membuatnya kelaparan terus.
    if cooldown and not wait_for_slot:
        return {
            "_error": 429,
            "_status": 429,
            "_message": f"Rate limit aktif. Coba lagi dalam {cooldown} detik.",
        }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    runtime = _account_status(account_name)[1] if account_name else None
    started_at = time.perf_counter()
    try:
        await _wait_for_api_slot(api_key)
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{base}{path}", params=params, headers=headers)
            if runtime is not None:
                runtime["last_latency_ms"] = round((time.perf_counter() - started_at) * 1000)
                runtime["last_http_status"] = r.status_code
            try:
                payload = r.json()
            except ValueError:
                payload = {"message": r.text.strip() or "Respons bukan JSON"}

            if r.is_success:
                if isinstance(payload, dict):
                    return payload
                return {"data": payload}

            if r.status_code == 429 and account_name:
                retry_after = r.headers.get("Retry-After")
                if not retry_after:
                    match = re.search(r"retry\s+after\s+(\d+)", r.text, re.IGNORECASE)
                    retry_after = match.group(1) if match else None
                cooldown = _mark_rate_limit(account_name, retry_after, api_key)
                result_message = f"Rate limit exceeded. Coba lagi dalam {cooldown} detik."
            else:
                result_message = None

            # Simpan status terstruktur agar tombol dapat menampilkan error
            # yang berguna, bukan dump object Python yang sulit dibaca.
            if isinstance(payload, dict):
                result = dict(payload)
            else:
                result = {"message": str(payload)}
            result["_error"] = r.status_code
            result["_status"] = r.status_code
            result["_text"] = r.text[:500]
            if result_message:
                result["_message"] = result_message
            return result
    except Exception as e:
        return {"_error": "network", "_message": str(e)}

def _api_error_text(data: dict | None) -> str:
    """Format error API singkat dan aman untuk ditampilkan di Telegram."""
    if not data:
        return "Tidak ada respons dari API."
    status = data.get("_status") or data.get("_error")
    message = (
        data.get("message")
        or data.get("_message")
        or data.get("error_description")
        or data.get("error")
        or data.get("_text")
        or str(data)
    )
    if isinstance(message, dict):
        message = message.get("message") or message.get("detail") or str(message)
    prefix = f"HTTP {status}: " if status else ""
    return f"{prefix}{str(message)[:300]}"

def _response_rows(data: dict | None, *keys: str) -> tuple[list, dict]:
    """Ambil rows dari beberapa bentuk respons API yang umum."""
    if not isinstance(data, dict):
        return [], {}
    value = None
    for key in keys:
        if isinstance(data.get(key), (list, dict)):
            value = data[key]
            break
    if value is None:
        value = data.get("data", [])
    if isinstance(value, dict):
        rows = (
            value.get("items")
            or value.get("rows")
            or value.get("numbers")
            or value.get("data")
            or []
        )
        pagination = value.get("pagination") or value.get("meta") or {}
    else:
        rows = value
        pagination = (
            data.get("pagination")
            or data.get("meta")
            or {}
        )
    if not isinstance(pagination, dict):
        pagination = {}
    return (rows if isinstance(rows, list) else []), (
        pagination if isinstance(pagination, dict) else {}
    )

# Kompatibilitas internal untuk callback lama. Endpoint lama tidak ditampilkan
# lagi di dashboard karena Augestel hanya mendokumentasikan numbers/messages/
# statistics.
async def tw_get(path: str, params: dict | None = None) -> dict | None:
    return await panel_get(path.replace("/api/v1", "", 1), params)

async def tw_post(path: str, body: dict) -> dict:
    return {"_error": "Endpoint POST tidak tersedia di API Augestel"}

async def tw_delete(path: str, body: dict | None = None) -> dict:
    return {"_error": "Endpoint DELETE tidak tersedia di API Augestel"}

# ─── OTP helpers ─────────────────────────────────────────────────────────────────

def get_country_info(phone_number):
    clean = re.sub(r'\D', '', str(phone_number))
    # Prefix wilayah bisa lebih panjang dari empat digit (mis. Guernsey
    # 441481, Christmas Island 6189164, atau Mayotte 262269). Selalu cek
    # prefix terpanjang lebih dulu agar tidak tertukar dengan kode induknya.
    for prefix in sorted(COUNTRY_CODES, key=len, reverse=True):
        if clean.startswith(prefix):
            d = COUNTRY_CODES[prefix]
            return d[0], d[1], d[2]
    return 'Unknown', '🌎', 'UN'

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

# ─── Mask helpers ────────────────────────────────────────────────────────────────

DEFAULT_MASK_EMOJI_ID  = "6217304154138742190"
DEFAULT_MASK_EMOJI_FB  = "✨"
DEFAULT_MASK_FRONT     = 4
DEFAULT_MASK_BACK      = 4

def get_mask_settings() -> dict:
    """Ambil konfigurasi mask nomor dari settings."""
    s = load_settings()
    return {
        "front":    max(1, min(int(s.get("mask_front",    DEFAULT_MASK_FRONT)), 8)),
        "back":     max(1, min(int(s.get("mask_back",     DEFAULT_MASK_BACK)),  8)),
        "emoji_id": s.get("mask_emoji_id",  DEFAULT_MASK_EMOJI_ID),
        "emoji_fb": s.get("mask_emoji_fb",  DEFAULT_MASK_EMOJI_FB),
    }

def build_separator(mask: dict) -> str:
    """Bangun HTML separator (premium emoji atau plain emoji)."""
    eid = mask["emoji_id"]
    efb = mask["emoji_fb"]
    if eid:
        return f'<tg-emoji emoji-id="{eid}">{efb}</tg-emoji>'
    return efb   # plain emoji jika tidak ada ID premium

def build_masked_number(clean_rc: str, mask: dict | None = None, bold: bool = True) -> str:
    """
    Buat nomor yang sudah di-mask sesuai setting.
    Contoh:  +92301234  →  +9230✨1234   (front=4, back=4)
    """
    if mask is None:
        mask = get_mask_settings()
    front = mask["front"]
    back  = mask["back"]
    sep   = build_separator(mask)

    front_digits = clean_rc[:front]
    back_digits  = clean_rc[-back:] if back <= len(clean_rc) - front else clean_rc[front:]

    number_str = f"+{front_digits}{sep}{back_digits}"
    return f"<b>{number_str}</b>" if bold else number_str

def format_premium_message(c_iso, c_flag, short_cli, clean_rc):
    asset  = SERVICE_ASSETS.get(short_cli, {"premium_id": None})
    svc    = f'<tg-emoji emoji-id="{asset["premium_id"]}">📱</tg-emoji>' if asset.get("premium_id") else short_cli
    flag   = f'<tg-emoji emoji-id="{FLAG_EMOJI_PREMIUM[c_iso]}">{c_flag}</tg-emoji>' if c_iso in FLAG_EMOJI_PREMIUM else c_flag
    masked = build_masked_number(clean_rc)
    return f"<b>{flag} {c_iso} {svc} {masked} UN</b>"

def build_final_otp_message(source: str, message: str, range_name: str, clean_rc: str):
    """Bangun (text_msg, kb_override, otp) dari data SMS mentah.

    SATU-SATUNYA sumber logic rendering pesan OTP di seluruh bot -- dipakai baik
    oleh worker() saat forward SMS SUNGGUHAN, maupun oleh fitur Preview/Test
    Template di menu admin. Karena keduanya lewat fungsi yang sama persis, hasil
    preview/test dijamin 100% identik dengan pesan OTP asli yang dikirim ke grup
    (format teks, warna tombol, emoji premium -- semuanya sama, bukan simulasi)."""
    short_cli             = detect_service(source, message)
    c_name, c_flag, c_iso = get_country_info(clean_rc)
    otp                   = extract_otp(message)

    json_tpl = get_otp_json_template()
    fmt      = get_custom_format()
    mask     = get_mask_settings()
    masked   = build_masked_number(clean_rc, mask, bold=False)
    if c_iso in FLAG_EMOJI_PREMIUM:
        flag_display = f'<tg-emoji emoji-id="{FLAG_EMOJI_PREMIUM[c_iso]}">{c_flag}</tg-emoji>'
    else:
        flag_display = c_flag
    kb_override = None

    if json_tpl:
        asset = SERVICE_ASSETS.get(short_cli, {"premium_id": None})
        svc_icon_html = (f'<tg-emoji emoji-id="{asset["premium_id"]}">📱</tg-emoji>'
                          if asset.get("premium_id") else html.escape(short_cli))
        text_ph, raw_ph = build_otp_placeholders(
            flag_html=flag_display, flag_raw=c_flag, c_iso=c_iso,
            svc_icon_html=svc_icon_html, short_cli=short_cli, source=source,
            masked_html=masked, number_raw=clean_rc, otp=otp,
            range_name=range_name, message=message,
        )
        text_msg, kb_override = render_otp_json_template(json_tpl, text_ph, raw_ph)
        if not text_msg:
            # Template kosong/rusak → fallback ke premium bawaan agar tidak gagal kirim
            text_msg = format_premium_message(c_iso, c_flag, short_cli, clean_rc)
            kb_override = None
    elif fmt == DEFAULT_FORMAT:
        text_msg = format_premium_message(c_iso, c_flag, short_cli, clean_rc)
    else:
        # Escape semua konten dari user/API agar tidak rusak HTML Telegram
        text_msg = apply_custom_format(
            fmt, flag=flag_display, country=html.escape(c_iso),
            service=html.escape(source), number=masked, otp=html.escape(otp),
            sender=html.escape(source), range_name=html.escape(range_name),
            message=html.escape(message)
        )
    return text_msg, kb_override, otp

# ─── Data dummy untuk fitur Preview & Test Template (admin) ────────────────────
# Daftar layanan realistis dengan format pesan asli tiap layanan, dipakai untuk
# menghasilkan SMS OTP acak yang terlihat 100% seperti SMS sungguhan.
DUMMY_SERVICES = [
    ("WS",  "WhatsApp",  "Your WhatsApp code is {otp}. Don't share this code."),
    ("TG",  "Telegram",  "Telegram code: {otp}. Do not give this code to anyone, even if they say they are from Telegram."),
    ("FB",  "Facebook",  "{otp} is your Facebook confirmation code"),
    ("GO",  "Google",    "G-{otp} is your Google verification code."),
    ("IG",  "Instagram", "{otp} is your Instagram code. Don't share it."),
    ("TT",  "TikTok",    "{otp} is your TikTok verification code"),
    ("TW",  "Twitter",   "Your Twitter confirmation code is {otp}"),
    ("SC",  "Snapchat",  "Your Snapchat login code is {otp}"),
    ("DC",  "Discord",   "Your Discord verification code is {otp}"),
    ("MS",  "Microsoft", "Use {otp} as your Microsoft verification code"),
    ("AP",  "Apple",     "Your Apple ID Code is: {otp}. Do not share it with anyone."),
    ("BG",  "Bitget",    "{otp} is your Bitget verification code. Valid for 10 minutes."),
    ("WC",  "WeChat",    "WeChat verification code {otp}, valid for 5 minutes"),
    ("NF",  "Netflix",   "Your Netflix verification code is {otp}"),
    ("SP",  "Shopee",    "{otp} is your Shopee verification code"),
    ("LA",  "Lazada",    "{otp} is your Lazada verification code"),
    ("TN",  "Tinder",    "Your Tinder code is {otp}"),
]
DUMMY_OPERATORS = [
    "Vodafone", "Jazz", "Zong", "Ufone", "Ooredoo", "Airtel", "MTN", "Orange",
    "T-Mobile", "Verizon", "AT&T", "Etisalat", "du", "Globe", "Smart", "Digi", "Maxis",
]

def generate_dummy_otp() -> dict:
    """Bangun 1 set data SMS OTP ACAK yang realistis: negara acak, operator acak,
    nomor HP acak, layanan acak, dan kode OTP acak -- untuk fitur Preview/Test
    Template. Hasilnya dilewatkan ke build_final_otp_message() yang sama persis
    dipakai untuk SMS sungguhan, jadi tampilannya 100% seperti pesan asli."""
    prefix, (c_name, c_flag, c_iso) = random.choice(list(COUNTRY_CODES.items()))
    short_cli, svc_name, msg_tpl     = random.choice(DUMMY_SERVICES)
    otp      = ''.join(random.choices('0123456789', k=random.choice([4, 5, 6])))
    clean_rc = prefix + ''.join(random.choices('0123456789', k=random.randint(7, 9)))
    return {
        "source":     svc_name,
        "message":    msg_tpl.format(otp=otp),
        "range_name": f"{c_name} - {random.choice(DUMMY_OPERATORS)}",
        "clean_rc":   clean_rc,
    }

# ─── Telegram helpers ─────────────────────────────────────────────────────────────

async def _send_single(app, target_id, text, markup):
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            await asyncio.wait_for(
                app.bot.send_message(
                    chat_id=target_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=markup,
                ),
                timeout=5,
            )
            return True
        except Exception as e:
            msg = str(e)
            retryable = (
                isinstance(e, asyncio.TimeoutError)
                or "Flood control" in msg
                or "Retry in" in msg
                or "imed out" in msg
            )
            if retryable and attempt < max_attempts - 1:
                await asyncio.sleep(min(3 * (attempt + 1), 10))
                continue
            print(f"{C_RED}[ERR] {target_id}: {msg}{C_RESET}")
            return False
    return False

async def send_to_telegram(app, text, otp, full_message, kb_override: InlineKeyboardMarkup | None = None):
    settings = load_settings()
    groups   = settings.get("groups", {})
    tasks    = []

    # Jika template JSON (teks+tombol) aktif, kb_override sudah jadi (dibangun di worker)
    # dan dipakai SAMA untuk semua tujuan (chat utama + semua grup).
    if kb_override is not None:
        tasks.append(_send_single(app, str(CHAT_ID), text, kb_override))
        added = {str(CHAT_ID)}
        for nama, data in groups.items():
            tid = str(data["id"])
            if tid in added: continue
            tasks.append(_send_single(app, tid, text, kb_override))
            added.add(tid)
        results = await asyncio.gather(*tasks)
        return bool(results and results[0])

    kb_main = [
        [_copy_btn(str(otp), str(otp),
                   api_kwargs=_style_kwargs("success", "5465443379917629504"))],
        [_copy_btn(str(full_message), "Message",
                   api_kwargs=_style_kwargs("primary", "5296369303661067030")),
         btn("number", url="https://t.me/numberkingjars", style="primary")],
    ]
    tasks.append(_send_single(app, str(CHAT_ID), text, InlineKeyboardMarkup(kb_main)))
    added = {str(CHAT_ID)}

    for nama, data in groups.items():
        tid = str(data["id"])
        if tid in added: continue
        # Style tombol custom template diambil dari setting per-grup (default "primary")
        tpl_style = data.get("btn_style", "primary")
        kb = [
            [_copy_btn(str(otp), str(otp),
                       api_kwargs=_style_kwargs("success", "5465443379917629504"))],
            [_copy_btn(str(full_message), "Message",
                       api_kwargs=_style_kwargs("primary", "5296369303661067030")),
             btn(data["btn_text"], url=data["btn_url"], style=tpl_style)],
        ]
        tasks.append(_send_single(app, tid, text, InlineKeyboardMarkup(kb)))
        added.add(tid)

    results = await asyncio.gather(*tasks)
    return bool(results and results[0])

# ═══════════════════════════════════════════════════════════════════════════════════
# ADMIN DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════════

def get_poll_interval() -> int:
    """Ambil interval polling (detik) dari settings.

    Nilai interval dibatasi agar selalu berada di antara 5 dan 300 detik.
    Nilai POLL_INTERVAL di .env menjadi default jika belum ada nilai tersimpan
    di settings.
    """
    s = load_settings()
    try:
        val = int(s.get("poll_interval", os.getenv("POLL_INTERVAL", "65")))
        return max(POLL_INTERVAL_MIN, min(val, POLL_INTERVAL_MAX))
    except (ValueError, TypeError):
        return 65

def _account_name_from_callback(data: str) -> str:
    return data.split(":", 1)[1].strip()

def _mask_api_key(key: str) -> str:
    key = str(key or "")
    if not key:
        return "belum diisi"
    if len(key) <= 10:
        return "••••••••"
    return f"{key[:5]}••••{key[-4:]}"

def _normalize_account_base(url: str) -> str:
    """Keep every account on the Augestel IPRN endpoint.

    The URL is intentionally not configurable from Telegram or environment
    variables so adding an account never requires a separate URL input.
    """
    return PANEL_BASE

def _save_accounts(accounts: list[dict]) -> None:
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=4, ensure_ascii=False)


def _mask_webhook_secret(secret: str) -> str:
    secret = str(secret or "")
    if not secret:
        return "opsional, belum diisi"
    if len(secret) <= 8:
        return "••••••••"
    return f"{secret[:3]}••••{secret[-3:]}"

async def stop_account_worker(name: str) -> bool:
    """Stop one worker without stopping the Telegram bot."""
    task = _worker_tasks.get(name)
    if not task:
        _account_status(name)[1]["status"] = "stopped"
        return False
    _account_status(name)[1]["status"] = "stopping"
    event = _worker_wake_events.get(name)
    if event:
        event.set()
    config_event = _worker_config_events.get(name)
    if config_event:
        config_event.set()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        _worker_tasks.pop(name, None)
        _worker_wake_events.pop(name, None)
        _account_status(name)[1]["status"] = "stopped"
    return True

async def start_account_worker(app, account: dict, restart: bool = False) -> str:
    """Start or restart one account worker while Telegram keeps running."""
    name = str(account.get("name", "")).strip()
    if not name or not account.get("api_key"):
        return "missing_key"
    current = _worker_tasks.get(name)
    if current and not current.done():
        if not restart:
            return "already_running"
        await stop_account_worker(name)
    _worker_runtime[name] = {
        "status": "starting",
        "last_error": "",
        "started_at": now_wib().strftime("%Y-%m-%d %H:%M:%S WIB"),
    }
    task = asyncio.create_task(worker(app, account))
    _worker_tasks[name] = task
    return "started"

async def start_all_workers(app, restart: bool = False) -> tuple[int, int]:
    started = 0
    skipped = 0
    for account in load_accounts():
        result = await start_account_worker(app, account, restart=restart)
        if result in {"started", "already_running"}:
            started += 1
        else:
            skipped += 1
    return started, skipped

async def stop_all_workers() -> int:
    names = list(_worker_tasks)
    for name in names:
        await stop_account_worker(name)
    return len(names)

def wake_all_workers() -> None:
    """Apply setting changes without restarting the Telegram application."""
    for event in _worker_config_events.values():
        event.set()

def admin_main_keyboard():
    interval = get_poll_interval()
    mask     = get_mask_settings()
    efb      = mask["emoji_fb"] or "✨"
    return InlineKeyboardMarkup([
        # ── Fitur Augestel yang terdokumentasi ──────────────────────────────
        [btn("📱 My Numbers",         callback_data="adm_numbers",      style="success"),
         btn("📨 Message History",    callback_data="adm_traffic",      style="success")],
        [btn("📈 Earnings & Stats",   callback_data="adm_me",            style="success")],
        [btn("📥 Download Numbers",   callback_data="adm_dl_numbers",   style="primary"),
         btn("📈 Statistik Harian",   callback_data="adm_stats",        style="primary")],
        [btn("📋 Download Log",       callback_data="adm_dl_log",       style="primary"),
         btn("✏️ Format Pesan",       callback_data="adm_format",       style="primary")],
        # ── Merah (danger): Konfigurasi ─────────────────────────────────────
        [btn(f"⏱ Interval Cek: {interval}s", callback_data="adm_poll_interval", style="danger")],
        [btn(
            f"🎭 Mask Nomor: {mask['front']}depan {efb} {mask['back']}belakang",
            callback_data="adm_mask", style="danger"
        )],
        [btn(
            "🌐 Webhook URL / IP",
            callback_data="adm_webhook",
            style="danger",
        )],
        # ── Biru (primary): Manajemen ────────────────────────────────────────
        [btn("👫 Kelola Grup",        callback_data="adm_groups",       style="primary"),
         btn("👑 Tambah Admin",       callback_data="adm_add_admin",    style="primary")],
        [btn("👥 Multi Akun & Worker", callback_data="adm_accounts",      style="primary")],
    ])

async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            f"⛔ Akses ditolak. User ID kamu ({update.effective_user.id}) "
            f"tidak terdaftar sebagai admin."
        )
        return ConversationHandler.END
    await update.message.reply_text(
        "🛠 <b>Admin Dashboard</b>\nPilih menu:", parse_mode="HTML",
        reply_markup=admin_main_keyboard()
    )
    return ADMIN_MAIN

async def adm_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("🛠 <b>Admin Dashboard</b>\nPilih menu:", parse_mode="HTML",
                              reply_markup=admin_main_keyboard())
    return ADMIN_MAIN

# ── Statistik Augestel (/statistics) ───────────────────────────────────────────

async def adm_me(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Memuat...")
    data = await panel_get("/statistics", {"group_by": "month"})
    if not data or "_error" in data:
        await q.edit_message_text(
            f"❌ Gagal mengambil statistik:\n<code>{html.escape(_api_error_text(data))}</code>",
            parse_mode="HTML", reply_markup=_back_kb()
        )
        return ADMIN_MAIN

    summary = data.get("summary", {})
    currency = summary.get("currency", "")
    text = (
        f"📊 <b>Augestel Statistics</b>\n\n"
        f"💬 Total pesan   : <b>{summary.get('total_messages', '—')}</b>\n"
        f"✅ Delivered     : <b>{summary.get('delivered', '—')}</b>\n"
        f"❌ Failed        : <b>{summary.get('failed', '—')}</b>\n"
        f"💵 Total earnings: <b>{summary.get('total_earnings', '—')} {currency}</b>\n\n"
        f"📅 Periode: {data.get('period', {}).get('start_date', '—')} – "
        f"{data.get('period', {}).get('end_date', '—')}"
    )
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=_back_kb())
    return ADMIN_MAIN

# ═══════════════════════════════════════════════════════════════════════════════════
# 🔄 PAGINATION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════════

PAGE_SIZE = 10

def _page_kb(prefix: str, page: int, total: int, extra_rows: list = None) -> InlineKeyboardMarkup:
    """Tombol navigasi halaman universal."""
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    nav = []
    if page > 1:
        nav.append(btn("⬅️ Sebelumnya", callback_data=f"{prefix}_page:{page-1}", style="primary"))
    nav.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(btn("Selanjutnya ➡️", callback_data=f"{prefix}_page:{page+1}", style="primary"))
    rows = [nav] if nav else []
    if extra_rows:
        rows.extend(extra_rows)
    rows.append([btn("🔙 Kembali", callback_data="adm_back", style="secondary")])
    return InlineKeyboardMarkup(rows)

# ── Legacy panel screens (not exposed for Augestel) ────────────────────────────

async def _send_ratecard_page(target, ctx, page: int, keyword: str, edit: bool = False):
    params = {"page": page, "pageSize": PAGE_SIZE}
    if keyword and keyword != "-":
        params["search"] = keyword
    data = await tw_get("/api/v1/ratecard", params)
    if not data or "_error" in data:
        text = f"❌ Gagal: {html.escape(str(data))}"
        if edit: await target.edit_message_text(text, reply_markup=_back_kb())
        else:    await target.reply_text(text, reply_markup=_back_kb())
        return ADMIN_MAIN
    rows  = data.get("rows", [])
    total = data.get("total", 0)
    if not rows:
        text = "💰 <b>Rate Card</b>\n\nTidak ada hasil."
        if edit: await target.edit_message_text(text, parse_mode="HTML", reply_markup=_back_kb())
        else:    await target.reply_text(text, parse_mode="HTML", reply_markup=_back_kb())
        return ADMIN_MAIN
    ctx.user_data["rc_page"]    = page
    ctx.user_data["rc_keyword"] = keyword
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    lines = [f"💰 <b>Rate Card</b> — {total} destinasi  (hal. {page}/{total_pages})\n"]
    for r in rows:
        lines.append(
            f"🆔 <code>{html.escape(str(r.get('id','?')))}</code> | <b>{html.escape(str(r.get('destinationName','?')))}</b>\n"
            f"   📞 {html.escape(str(r.get('template','?')))} | MCC {r.get('mcc','?')}/{r.get('mnc','?')} | 💲{r.get('rate','?')}"
        )
    kb = _page_kb("adm_rc", page, total)
    text = "\n".join(lines)
    if edit: await target.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:    await target.reply_text(text, parse_mode="HTML", reply_markup=kb)
    return ADMIN_MAIN

async def adm_ratecard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text(
        "💰 <b>Rate Card</b>\nKetik kata pencarian atau <code>-</code> untuk semua:",
        parse_mode="HTML", reply_markup=_back_kb()
    )
    return RATECARD_SEARCH

async def adm_ratecard_result(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyword = update.message.text.strip()
    await _send_ratecard_page(update.message, ctx, 1, keyword, edit=False)
    return ADMIN_MAIN

async def adm_ratecard_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    page    = int(q.data.split(":")[1])
    keyword = ctx.user_data.get("rc_keyword", "-")
    await _send_ratecard_page(q, ctx, page, keyword, edit=True)
    return ADMIN_MAIN


async def _send_access_page(target, ctx, page: int, keyword: str, edit: bool = False):
    params = {"page": page, "pageSize": PAGE_SIZE}
    if keyword and keyword != "-":
        params["source"] = keyword
    data = await tw_get("/api/v1/access-list", params)
    if not data or "_error" in data:
        text = f"❌ Gagal: {html.escape(str(data))}"
        if edit: await target.edit_message_text(text, reply_markup=_back_kb())
        else:    await target.reply_text(text, reply_markup=_back_kb())
        return ADMIN_MAIN
    rows  = data.get("rows", [])
    total = data.get("total", 0)
    if not rows:
        text = "🔍 <b>Access List</b>\n\nTidak ada hasil."
        if edit: await target.edit_message_text(text, parse_mode="HTML", reply_markup=_back_kb())
        else:    await target.reply_text(text, parse_mode="HTML", reply_markup=_back_kb())
        return ADMIN_MAIN
    ctx.user_data["ac_page"]    = page
    ctx.user_data["ac_keyword"] = keyword
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    lines = [f"🔍 <b>Access List</b> — {total} baris  (hal. {page}/{total_pages})\n"]
    for r in rows:
        lines.append(
            f"📤 <b>{html.escape(str(r.get('source','?')))}</b> → {html.escape(str(r.get('rangeName','?')))}\n"
            f"   📞 {html.escape(str(r.get('rangeTemplate','?')))} | 🆔 rangeId <code>{html.escape(str(r.get('rangeId','?')))}</code>\n"
            f"   💬 {html.escape(str(r.get('message',''))[:80])}…\n"
            f"   ⏱ {html.escape(str(r.get('messageTime','?'))[:16])}"
        )
    kb = _page_kb("adm_ac", page, total)
    text = "\n".join(lines)
    if edit: await target.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:    await target.reply_text(text, parse_mode="HTML", reply_markup=kb)
    return ADMIN_MAIN

async def adm_access(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text(
        "🔍 <b>Access List</b>\nKetik source prefix atau <code>-</code> untuk semua:",
        parse_mode="HTML", reply_markup=_back_kb()
    )
    return ACCESS_SEARCH

async def adm_access_result(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyword = update.message.text.strip()
    await _send_access_page(update.message, ctx, 1, keyword, edit=False)
    return ADMIN_MAIN

async def adm_access_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    page    = int(q.data.split(":")[1])
    keyword = ctx.user_data.get("ac_keyword", "-")
    await _send_access_page(q, ctx, page, keyword, edit=True)
    return ADMIN_MAIN

# ── My Numbers (/numbers) ──────────────────────────────────────────────────────

async def _send_numbers_page(target, ctx, page: int, keyword: str, edit: bool = False):
    params = {"page": page, "per_page": PAGE_SIZE}
    if keyword and keyword != "-":
        params["range_name"] = keyword
    data = await panel_get("/numbers", params)
    if not data or "_error" in data:
        text = (
            "❌ <b>Gagal mengambil My Numbers</b>\n\n"
            f"<code>{html.escape(_api_error_text(data))}</code>"
        )
        if edit: await target.edit_message_text(text, parse_mode="HTML", reply_markup=_back_kb())
        else:    await target.reply_text(text, parse_mode="HTML", reply_markup=_back_kb())
        return ADMIN_MAIN
    rows, pagination = _response_rows(data, "data", "numbers", "items", "rows")
    total = (
        pagination.get("total")
        or data.get("total")
        or data.get("total_count")
        or len(rows)
    )
    if not rows:
        text = "📱 <b>My Numbers</b>\n\nTidak ada nomor."
        if edit: await target.edit_message_text(text, parse_mode="HTML", reply_markup=_back_kb())
        else:    await target.reply_text(text, parse_mode="HTML", reply_markup=_back_kb())
        return ADMIN_MAIN
    ctx.user_data["nm_page"]    = page
    ctx.user_data["nm_keyword"] = keyword
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    lines = [f"📱 <b>My Numbers</b> — {total} nomor  (hal. {page}/{total_pages})\n"]
    for r in rows:
        last_seen = r.get("last_message_at") or "belum ada pesan"
        lines.append(
            f"🟢 <code>{html.escape(str(r.get('number','?')))}</code> | "
            f"{html.escape(str(r.get('range_name','?')))}\n"
            f"   💰 Rate: {html.escape(str(r.get('a2p_rate','?')))} | "
            f"Pesan terakhir: {html.escape(str(last_seen)[:16])}"
        )
    kb = _page_kb("adm_nm", page, total)
    text = "\n".join(lines)
    if edit: await target.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:    await target.reply_text(text, parse_mode="HTML", reply_markup=kb)
    return ADMIN_MAIN

async def adm_numbers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Memuat nomor...")
    # Tombol langsung memuat semua nomor. Pencarian berdasarkan nama range
    # tetap tersedia lewat input teks jika pengguna membutuhkannya.
    return await _send_numbers_page(q, ctx, 1, "-", edit=True)

async def adm_numbers_result(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyword = update.message.text.strip()
    await _send_numbers_page(update.message, ctx, 1, keyword, edit=False)
    return ADMIN_MAIN

async def adm_numbers_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    page    = int(q.data.split(":")[1])
    keyword = ctx.user_data.get("nm_keyword", "-")
    await _send_numbers_page(q, ctx, page, keyword, edit=True)
    return ADMIN_MAIN

# ── Download Numbers sebagai .txt ───────────────────────────────────────────────

DOWNLOAD_PAGE_SIZE = 100
DOWNLOAD_COUNTRIES_PER_PAGE = 24
DOWNLOAD_CACHE_TTL = 60
DOWNLOAD_MAX_PAGES = 1000
DOWNLOAD_STORAGE_DIR = (
    os.getenv("DOWNLOAD_STORAGE_DIR", "SC").strip() or "SC"
)
DOWNLOAD_STORAGE_FILENAME = (
    os.getenv("DOWNLOAD_STORAGE_FILENAME", "mynumber.txt").strip()
    or "mynumber.txt"
)
_download_tasks: dict[int, asyncio.Task] = {}
_download_job_state: dict[int, dict] = {}


def _as_positive_int(value, default: int = 0) -> int:
    """Konversi nilai pagination API menjadi integer dengan aman."""
    try:
        number = int(value)
        return number if number >= 0 else default
    except (TypeError, ValueError):
        return default


def _number_value(row: dict) -> str:
    """Ambil nomor dari beberapa bentuk row API yang umum."""
    if isinstance(row, str):
        return row.strip()
    if not isinstance(row, dict):
        return ""
    value = (
        row.get("number")
        or row.get("phone_number")
        or row.get("phone")
        or row.get("phoneNumber")
        or row.get("msisdn")
    )
    return str(value).strip() if value is not None else ""


async def _fetch_all_download_numbers(progress_callback=None) -> tuple[list[dict] | None, dict | None]:
    """Ambil semua nomor dari endpoint /numbers, bukan hanya halaman pertama."""
    first = await panel_get(
        "/numbers",
        {"page": 1, "per_page": DOWNLOAD_PAGE_SIZE},
        wait_for_slot=True,
    )
    if not first or "_error" in first:
        return None, first

    rows, pagination = _response_rows(first, "data", "numbers", "items", "rows")
    all_rows = list(rows)
    total = _as_positive_int(
        pagination.get("total")
        or first.get("total")
        or first.get("total_count")
    )
    total_pages = _as_positive_int(
        pagination.get("total_pages")
        or pagination.get("pages")
        or first.get("total_pages")
    )
    # Jangan menghitung total_pages sendiri dari ukuran halaman yang diminta.
    # Beberapa API membatasi ukuran halaman di server (mis. selalu 100), jadi
    # hasil hitungan dari per_page=1000 bisa membuat 5000 nomor terpotong
    # setelah hanya 5 request.
    page = 1
    if progress_callback:
        await progress_callback(page, len(all_rows), total)

    page = 2
    last_page_fingerprint = None
    while page <= DOWNLOAD_MAX_PAGES:
        if total and len(all_rows) >= total:
            break
        if total_pages and page > total_pages and (
            not total or len(all_rows) >= total
        ):
            break

        data = await panel_get(
            "/numbers",
            {"page": page, "per_page": DOWNLOAD_PAGE_SIZE},
            wait_for_slot=True,
        )
        if not data or "_error" in data:
            return None, data
        page_rows, _ = _response_rows(data, "data", "numbers", "items", "rows")
        if not page_rows:
            break

        # Cegah loop tanpa akhir bila server mengabaikan parameter page dan
        # mengembalikan halaman yang sama berulang kali.
        page_fingerprint = tuple(_number_value(row) for row in page_rows)
        if page_fingerprint and page_fingerprint == last_page_fingerprint:
            return None, {
                "_error": "pagination",
                "_message": (
                    "API mengembalikan halaman nomor yang sama berulang kali. "
                    "Data belum dianggap lengkap."
                ),
            }
        last_page_fingerprint = page_fingerprint

        all_rows.extend(page_rows)
        rows = page_rows
        if progress_callback:
            await progress_callback(page, len(all_rows), total)
        page += 1

    # Deduplicate by the displayed number. It prevents duplicate file lines
    # when an API page is repeated during a refresh or pagination race.
    unique_rows = []
    seen_numbers = set()
    for row in all_rows:
        number = _number_value(row)
        if number and number not in seen_numbers:
            seen_numbers.add(number)
            # Normalisasi row string menjadi object agar semua fungsi menu
            # negara dapat memakai bentuk data yang sama.
            unique_rows.append(row if isinstance(row, dict) else {"number": number})
    return unique_rows, None


async def _get_download_numbers(ctx: ContextTypes.DEFAULT_TYPE, force: bool = False):
    """Return a short-lived per-admin snapshot for menu navigation."""
    cached = ctx.user_data.get("download_numbers_cache")
    cached_at = _as_positive_int(ctx.user_data.get("download_numbers_cache_at"))
    if (
        not force
        and isinstance(cached, list)
        and cached
        and cached_at
        and time.time() - cached_at < DOWNLOAD_CACHE_TTL
    ):
        return cached, None

    rows, error = await _fetch_all_download_numbers()
    if error:
        return None, error
    ctx.user_data["download_numbers_cache"] = rows or []
    ctx.user_data["download_numbers_cache_at"] = int(time.time())
    return rows or [], None


def _cached_download_numbers(ctx: ContextTypes.DEFAULT_TYPE) -> tuple[list[dict] | None, int]:
    cached = ctx.user_data.get("download_numbers_cache")
    cached_at = _as_positive_int(ctx.user_data.get("download_numbers_cache_at"))
    if not isinstance(cached, list) or not cached_at:
        return None, 0
    return cached, cached_at


def _download_api_cooldown() -> int:
    account = get_active_account()
    if not account:
        return 0
    return _rate_limit_wait(
        str(account.get("name", "")),
        str(account.get("api_key", "")),
    )


def _format_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600} jam {seconds % 3600 // 60} mnt"
    if seconds >= 60:
        return f"{seconds // 60} mnt {seconds % 60} dtk"
    return f"{seconds} detik"


def _download_job_is_running(chat_id: int) -> bool:
    task = _download_tasks.get(chat_id)
    return bool(task and not task.done())


def _download_status_line(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int = 0) -> str:
    cooldown = _download_api_cooldown()
    if cooldown:
        return (
            f"⏱ Data baru tersedia lagi dalam <b>{_format_seconds(cooldown)}</b> "
            "<i>(cooldown API)</i>"
        )
    if chat_id and _download_job_is_running(chat_id):
        return "🔄 Pembaruan data sedang berjalan di latar belakang."
    return "✅ API siap diperbarui."


def _download_country_counts(rows: list[dict]) -> dict[str, dict]:
    """Kelompokkan nomor berdasarkan negara yang benar-benar muncul."""
    grouped = {}
    seen_numbers = set()
    for row in rows:
        number = _number_value(row)
        if not number or number in seen_numbers:
            continue
        seen_numbers.add(number)
        _, flag, iso = get_country_info(number)
        country = COUNTRY_DIRECTORY.get(iso, {
            "name": "Unknown",
            "flag": flag,
            "iso": iso,
        })
        item = grouped.setdefault(iso, {
            "name": country["name"],
            "flag": country["flag"],
            "iso": iso,
            "count": 0,
        })
        item["count"] += 1
    return grouped


def _download_menu_kb(
    total: int,
    grouped: dict[str, dict] | None = None,
    page: int = 1,
) -> InlineKeyboardMarkup:
    """Show All Negara first, followed immediately by country download buttons."""
    buttons = [[btn(
        f"🌍 All Number / Semua Negara ({total})",
        callback_data="adm_dl_all",
        style="success",
    )]]
    items = sorted(
        (
            item for item in (grouped or {}).values()
            if _as_positive_int(item.get("count")) > 0
        ),
        key=lambda item: (-item["count"], item["name"]),
    )
    total_pages = max(
        1,
        (len(items) + DOWNLOAD_COUNTRIES_PER_PAGE - 1)
        // DOWNLOAD_COUNTRIES_PER_PAGE,
    )
    page = max(1, min(page, total_pages))
    start = (page - 1) * DOWNLOAD_COUNTRIES_PER_PAGE
    page_items = items[start:start + DOWNLOAD_COUNTRIES_PER_PAGE]
    for index in range(0, len(page_items), 2):
        row = []
        for item in page_items[index:index + 2]:
            row.append(btn(
                f"{item['flag']} {item['name']} ({item['count']})",
                callback_data=f"adm_dl_country:{item['iso']}",
                style="primary",
            ))
        buttons.append(row)
    if len(items) > DOWNLOAD_COUNTRIES_PER_PAGE:
        navigation = []
        if page > 1:
            navigation.append(btn(
                "⬅️",
                callback_data=f"adm_dl_cpage:{page - 1}",
                style="secondary",
            ))
        navigation.append(btn(
            f"Hal. {page}/{total_pages}",
            callback_data=f"adm_dl_cpage:{page}",
            style="secondary",
        ))
        if page < total_pages:
            navigation.append(btn(
                "➡️",
                callback_data=f"adm_dl_cpage:{page + 1}",
                style="secondary",
            ))
        buttons.append(navigation)
    buttons.append([
        btn("🔄 Perbarui Data", callback_data="adm_dl_refresh", style="primary"),
        btn("🔙 Kembali", callback_data="adm_back", style="secondary"),
    ])
    return InlineKeyboardMarkup(buttons)


def _download_country_kb(
    grouped: dict[str, dict],
    page: int = 1,
) -> InlineKeyboardMarkup:
    items = sorted(
        (
            item for item in grouped.values()
            if _as_positive_int(item.get("count")) > 0
        ),
        key=lambda item: (-item["count"], item["name"]),
    )
    total_pages = max(
        1,
        (len(items) + DOWNLOAD_COUNTRIES_PER_PAGE - 1)
        // DOWNLOAD_COUNTRIES_PER_PAGE,
    )
    page = max(1, min(page, total_pages))
    start = (page - 1) * DOWNLOAD_COUNTRIES_PER_PAGE
    page_items = items[start:start + DOWNLOAD_COUNTRIES_PER_PAGE]
    buttons = []
    for index in range(0, len(page_items), 2):
        row = []
        for item in page_items[index:index + 2]:
            label = f"{item['flag']} {item['iso']} ({item['count']})"
            row.append(btn(
                label,
                callback_data=f"adm_dl_country:{item['iso']}",
                style="primary",
            ))
        buttons.append(row)
    navigation = []
    if page > 1:
        navigation.append(btn(
            "⬅️",
            callback_data=f"adm_dl_cpage:{page - 1}",
            style="secondary",
        ))
    navigation.append(btn(
        f"{page}/{total_pages}",
        callback_data=f"adm_dl_cpage:{page}",
        style="secondary",
    ))
    if page < total_pages:
        navigation.append(btn(
            "➡️",
            callback_data=f"adm_dl_cpage:{page + 1}",
            style="secondary",
        ))
    if navigation:
        buttons.append(navigation)
    buttons.append([
        btn("⬅️ Menu Download", callback_data="adm_dl_numbers", style="secondary"),
    ])
    return InlineKeyboardMarkup(buttons)


def _download_menu_text(
    rows: list[dict],
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    stale: bool = False,
) -> str:
    age = _as_positive_int(ctx.user_data.get("download_numbers_cache_at"))
    age_text = (
        f"{_format_seconds(int(time.time() - age))} lalu"
        if age
        else "belum tersedia"
    )
    freshness = "⚠️ Menampilkan cache terakhir" if stale else "✅ Data terbaru"
    return (
        "📥 <b>Download Numbers</b>\n\n"
        f"{freshness}\n"
        f"Total nomor tersedia: <b>{len(rows)}</b>\n"
        f"Terakhir diperbarui: <b>{age_text}</b>\n"
        f"{_download_status_line(ctx, chat_id)}\n\n"
        "Pilih <b>Semua Negara</b> atau langsung negara yang diinginkan:"
    )


async def _edit_download_status(
    bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except BadRequest as error:
        # "Message is not modified" is harmless when two UI actions finish
        # nearly together. Other errors are also isolated from the worker so
        # the background refresh never crashes the bot.
        if "not modified" not in str(error).lower():
            print(f"{C_YELLOW}[DOWNLOAD] Gagal memperbarui status: {error}{C_RESET}")
    except Exception as error:
        print(f"{C_YELLOW}[DOWNLOAD] Gagal memperbarui status: {error}{C_RESET}")


def _save_download_numbers_file(rows: list[dict]) -> tuple[str, int]:
    """Persist the complete number snapshot atomically for server-side use."""
    directory = os.path.abspath(DOWNLOAD_STORAGE_DIR)
    filename = os.path.basename(DOWNLOAD_STORAGE_FILENAME) or "mynumber.txt"
    path = os.path.join(directory, filename)
    os.makedirs(directory, exist_ok=True)

    numbers = []
    seen = set()
    for row in rows:
        number = _number_value(row)
        if number and number not in seen:
            seen.add(number)
            numbers.append(number)

    temp_path = f"{path}.tmp.{os.getpid()}"
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as file:
            if numbers:
                file.write("\n".join(numbers))
                file.write("\n")
        os.replace(temp_path, path)
    except Exception:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        raise
    return path, len(numbers)


async def _notify_download_ready(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    path: str,
    count: int,
) -> None:
    """Notify the admin and send the ready file when Telegram accepts it."""
    relative_path = os.path.relpath(path)
    caption = (
        "✅ File number sudah tersedia.\n"
        f"📊 Total: {count} nomor\n"
        f"📁 Lokasi server: {relative_path}"
    )
    try:
        with open(path, "rb") as file:
            await ctx.bot.send_document(
                chat_id=chat_id,
                document=file,
                filename=os.path.basename(path),
                caption=caption,
            )
    except Exception as error:
        print(
            f"{C_YELLOW}[DOWNLOAD] File tersimpan tetapi gagal dikirim "
            f"ke Telegram: {error}{C_RESET}"
        )
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ <b>File number sudah tersedia</b>\n\n"
                f"📊 Total: <b>{count}</b> nomor\n"
                f"📁 Lokasi: <code>{html.escape(relative_path)}</code>\n\n"
                "Pengiriman otomatis ke Telegram gagal. File tetap tersimpan "
                "di server."
            ),
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )


async def _show_download_error(query, ctx: ContextTypes.DEFAULT_TYPE, error) -> None:
    """Never leave a download callback silent when Telegram/API fails."""
    if isinstance(error, dict):
        detail = _api_error_text(error)
    else:
        detail = str(error) or "Kesalahan tidak diketahui."
    text = (
        "❌ <b>Download Numbers gagal</b>\n\n"
        f"<code>{html.escape(detail[:500])}</code>\n\n"
        "Pastikan akun aktif memiliki API key yang valid, lalu coba lagi."
    )
    try:
        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                btn("🔄 Coba Lagi", callback_data="adm_dl_refresh", style="primary"),
                btn("🔙 Kembali", callback_data="adm_back", style="secondary"),
            ]]),
        )
    except Exception:
        try:
            await ctx.bot.send_message(
                chat_id=query.message.chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=_back_kb(),
            )
        except Exception as send_error:
            print(
                f"{C_RED}[DOWNLOAD] Gagal mengirim pesan error: "
                f"{send_error}{C_RESET}"
            )


async def _download_refresh_job(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
) -> None:
    """Refresh numbers without holding up Telegram callback handlers."""
    state = _download_job_state.setdefault(chat_id, {})
    state.update({
        "status": "waiting",
        "started_at": time.time(),
        "message_id": message_id,
    })
    try:
        async def report_progress(page: int, fetched: int, expected: int) -> None:
            expected_text = str(expected) if expected else "?"
            state["page"] = page
            state["fetched"] = fetched
            state["expected"] = expected
            await _edit_download_status(
                ctx.bot,
                chat_id,
                message_id,
                "🔄 <b>Mengambil semua nomor di latar belakang...</b>\n\n"
                f"📄 Halaman API: <b>{page}</b>\n"
                f"📱 Nomor terkumpul: <b>{fetched}</b> / {expected_text}\n"
                "Menu akan muncul otomatis setelah proses selesai.",
                InlineKeyboardMarkup([[
                    btn("🔙 Kembali", callback_data="adm_back", style="secondary"),
                ]]),
            )

        while True:
            cooldown = _download_api_cooldown()
            if cooldown:
                state["status"] = "queued"
                state["retry_at"] = time.time() + cooldown
                await _edit_download_status(
                    ctx.bot,
                    chat_id,
                    message_id,
                    "🔄 <b>Download berjalan di latar belakang</b>\n\n"
                    f"⏱ Menunggu giliran API sekitar <b>{_format_seconds(cooldown)}</b>.\n"
                    "Setelah mendapat slot, semua halaman nomor akan diambil otomatis.",
                    InlineKeyboardMarkup([[
                        btn("🔙 Kembali", callback_data="adm_back", style="secondary"),
                    ]]),
                )

            state["status"] = "fetching"
            rows, error = await _fetch_all_download_numbers(report_progress)
            if error:
                cooldown = _download_api_cooldown()
                if (error.get("_status") if isinstance(error, dict) else None) == 429 or cooldown:
                    continue
                state["status"] = "error"
                await _edit_download_status(
                    ctx.bot,
                    chat_id,
                    message_id,
                    "❌ <b>Pembaruan data gagal</b>\n\n"
                    f"<code>{html.escape(_api_error_text(error))}</code>\n\n"
                    "Data cache sebelumnya tetap aman. Tekan perbarui untuk mencoba lagi.",
                    InlineKeyboardMarkup([[
                        btn("🔄 Coba Lagi", callback_data="adm_dl_refresh", style="primary"),
                        btn("🔙 Kembali", callback_data="adm_back", style="secondary"),
                    ]]),
                )
                return

            fresh_rows = rows or []
            ctx.user_data["download_numbers_cache"] = fresh_rows
            ctx.user_data["download_numbers_cache_at"] = int(time.time())
            grouped = _download_country_counts(fresh_rows)
            ctx.user_data["download_country_counts"] = grouped
            archive_path, archive_count = _save_download_numbers_file(fresh_rows)
            state["archive_path"] = archive_path
            state["archive_count"] = archive_count
            state["status"] = "ready"
            await _edit_download_status(
                ctx.bot,
                chat_id,
                message_id,
                _download_menu_text(fresh_rows, ctx, chat_id),
                _download_menu_kb(len(fresh_rows), grouped),
            )
            await _notify_download_ready(ctx, chat_id, archive_path, archive_count)
            return
    except asyncio.CancelledError:
        raise
    except Exception as error:
        state["status"] = "error"
        print(f"{C_RED}[DOWNLOAD] Background refresh gagal: {error}{C_RESET}")
        await _edit_download_status(
            ctx.bot,
            chat_id,
            message_id,
            "❌ <b>Pembaruan data gagal</b>\n\n"
            f"<code>{html.escape(str(error)[:300])}</code>\n\n"
            "Data belum siap. Tekan coba lagi untuk mengulang proses.",
            InlineKeyboardMarkup([[
                btn("🔄 Coba Lagi", callback_data="adm_dl_refresh", style="primary"),
                btn("🔙 Kembali", callback_data="adm_back", style="secondary"),
            ]]),
        )
    finally:
        current = _download_tasks.get(chat_id)
        if current is asyncio.current_task():
            _download_tasks.pop(chat_id, None)


def _start_download_refresh(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
) -> bool:
    """Start one refresh per chat; repeated clicks reuse the same task."""
    if _download_job_is_running(chat_id):
        return False
    task = asyncio.create_task(_download_refresh_job(ctx, chat_id, message_id))
    _download_tasks[chat_id] = task
    return True


async def stop_download_tasks() -> None:
    """Cancel pending download refreshes cleanly during application shutdown."""
    tasks = list(_download_tasks.values())
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _download_tasks.clear()
    _download_job_state.clear()


async def _send_numbers_file(
    query,
    ctx: ContextTypes.DEFAULT_TYPE,
    rows: list[dict],
    label: str,
    iso: str = "",
):
    """Kirim file teks satu nomor per baris."""
    numbers = [_number_value(row) for row in rows]
    numbers = [number for number in numbers if number]
    if not numbers:
        await query.edit_message_text(
            "ℹ️ Tidak ada nomor untuk didownload.",
            reply_markup=_back_kb(),
        )
        return

    content = "\n".join(numbers) + "\n"
    buf = io.BytesIO(content.encode("utf-8"))
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_") or "all"
    filename = f"numbers_{safe_label}_{now_wib().strftime('%Y%m%d_%H%M%S')}.txt"
    buf.name = filename
    await query.edit_message_text(
        f"📥 Mengirim <b>{len(numbers)}</b> nomor ({html.escape(label)}) sebagai file...",
        parse_mode="HTML",
        reply_markup=_back_kb(),
    )
    await ctx.bot.send_document(
        chat_id=query.message.chat_id,
        document=buf,
        filename=filename,
        caption=(
            f"📱 Negara: {iso or 'Semua negara'}\n"
            f"📊 Total: {len(numbers)} nomor\n"
            f"⏱ {now_wib().strftime('%Y-%m-%d %H:%M:%S WIB')}"
        ),
    )


async def adm_dl_numbers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Tampilkan menu download segera, lalu refresh data di background."""
    q = update.callback_query
    await q.answer("Membuka data nomor...")
    try:
        chat_id = q.message.chat_id
        message_id = q.message.message_id
        rows, cached_at = _cached_download_numbers(ctx)
        if rows is not None:
            grouped = _download_country_counts(rows)
            ctx.user_data["download_country_counts"] = grouped
            stale = not cached_at or time.time() - cached_at >= DOWNLOAD_CACHE_TTL
            await q.edit_message_text(
                _download_menu_text(rows, ctx, chat_id, stale=stale),
                parse_mode="HTML",
                reply_markup=_download_menu_kb(len(rows), grouped),
            )
            if stale:
                _start_download_refresh(ctx, chat_id, message_id)
            return ADMIN_MAIN

        await q.edit_message_text(
            "🔄 <b>Memuat data nomor di latar belakang...</b>\n\n"
            "Menu admin tetap bisa digunakan. Pesan ini akan otomatis berubah "
            "saat data siap.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                btn("🔙 Kembali", callback_data="adm_back", style="secondary"),
            ]]),
        )
        if not _start_download_refresh(ctx, chat_id, message_id):
            await _show_download_error(
                q,
                ctx,
                {"_error": "busy", "_message": "Pembaruan data sudah berjalan."},
            )
    except Exception as error:
        print(f"{C_RED}[DOWNLOAD] Membuka menu gagal: {error}{C_RESET}")
        await _show_download_error(q, ctx, error)
    return ADMIN_MAIN


async def adm_dl_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Force a background refresh without blocking the current dashboard."""
    q = update.callback_query
    chat_id = q.message.chat_id
    message_id = q.message.message_id
    if _download_job_is_running(chat_id):
        await q.answer("Pembaruan sudah berjalan di latar belakang.")
        return ADMIN_MAIN
    await q.answer("Pembaruan dijadwalkan...")
    rows, _ = _cached_download_numbers(ctx)
    if rows is None:
        await q.edit_message_text(
            "🔄 <b>Menyiapkan pembaruan data...</b>\n\n"
            "Dashboard tetap bisa digunakan selama API menunggu cooldown.",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
    else:
        await q.edit_message_text(
            _download_menu_text(rows, ctx, chat_id, stale=True),
            parse_mode="HTML",
            reply_markup=_download_menu_kb(
                len(rows),
                _download_country_counts(rows),
            ),
        )
    _start_download_refresh(ctx, chat_id, message_id)
    return ADMIN_MAIN


async def adm_dl_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        rows, _ = _cached_download_numbers(ctx)
        if rows is None:
            await q.answer(
                "Data masih dimuat di latar belakang. Coba lagi setelah menu berubah.",
                show_alert=True,
            )
            return ADMIN_MAIN
        await q.answer("Menyiapkan semua nomor...")
        await _send_numbers_file(q, ctx, rows, "Semua_Negara")
    except Exception as error:
        print(f"{C_RED}[DOWNLOAD] Kirim semua nomor gagal: {error}{C_RESET}")
        await _show_download_error(q, ctx, error)
    return ADMIN_MAIN


async def adm_dl_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    rows, _ = _cached_download_numbers(ctx)
    if rows is None:
        await q.answer(
            "Data masih diproses di latar belakang.",
            show_alert=True,
        )
        return ADMIN_MAIN
    await q.answer("Memuat daftar negara...")
    grouped = _download_country_counts(rows)
    if not grouped:
        await q.edit_message_text(
            "🔎 <b>Filter Negara</b>\n\nTidak ada nomor yang bisa dikelompokkan.",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        return ADMIN_MAIN
    ctx.user_data["download_country_counts"] = grouped
    await q.edit_message_text(
        _download_menu_text(rows, ctx, q.message.chat_id),
        parse_mode="HTML",
        reply_markup=_download_menu_kb(len(rows), grouped),
    )
    return ADMIN_MAIN


async def adm_dl_country_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    grouped = ctx.user_data.get("download_country_counts") or {}
    if not grouped:
        rows, _ = _cached_download_numbers(ctx)
        if rows is None:
            await q.answer("Data belum siap.", show_alert=True)
            return ADMIN_MAIN
        grouped = _download_country_counts(rows)
        ctx.user_data["download_country_counts"] = grouped
    page = _as_positive_int(q.data.split(":")[1], 1)
    rows, _ = _cached_download_numbers(ctx)
    if rows is None:
        await q.answer("Data belum siap.", show_alert=True)
        return ADMIN_MAIN
    await q.edit_message_text(
        _download_menu_text(rows, ctx, q.message.chat_id),
        parse_mode="HTML",
        reply_markup=_download_menu_kb(len(rows), grouped, page),
    )
    return ADMIN_MAIN


async def adm_dl_country(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        iso = q.data.split(":", 1)[1].upper()
        rows, _ = _cached_download_numbers(ctx)
        if rows is None:
            await q.answer("Data masih dimuat di latar belakang.", show_alert=True)
            return ADMIN_MAIN
        await q.answer("Menyiapkan file negara...")
        selected = [
            row for row in rows
            if get_country_info(_number_value(row))[2] == iso
        ]
        country = COUNTRY_DIRECTORY.get(iso, {
            "name": "Unknown",
            "flag": "🌎",
            "iso": iso,
        })
        await _send_numbers_file(
            q,
            ctx,
            selected,
            f"{country['name']}_{iso}",
            iso=iso,
        )
    except Exception as error:
        print(f"{C_RED}[DOWNLOAD] Kirim nomor negara gagal: {error}{C_RESET}")
        await _show_download_error(q, ctx, error)
    return ADMIN_MAIN

# ── Legacy delete-number screens (not exposed for Augestel) ────────────────────

def _del_nums_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("🗑️ Hapus SEMUA Nomor",     callback_data="adm_del_all",      style="danger")],
        [btn("🎯 Hapus per Range ID",     callback_data="adm_del_by_range", style="warning")],
        [btn("📄 Hapus via File .txt",    callback_data="adm_del_by_file",  style="warning")],
        [btn("🔙 Kembali",                callback_data="adm_back",         style="secondary")],
    ])

async def adm_del_numbers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text(
        "🗑️ <b>Hapus Nomor</b>\n\n"
        "Pilih metode penghapusan:\n\n"
        "• <b>Hapus SEMUA</b> — hapus semua nomor yang terdaftar\n"
        "• <b>Hapus per Range ID</b> — hapus semua nomor dalam 1 range\n"
        "• <b>Hapus via File</b> — upload .txt berisi nomor (1 per baris)\n\n"
        "⚠️ <b>Tindakan ini tidak bisa dibatalkan!</b>",
        parse_mode="HTML", reply_markup=_del_nums_kb()
    )
    return ADMIN_MAIN

async def adm_del_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Konfirmasi hapus semua nomor."""
    q = update.callback_query; await q.answer()
    await q.edit_message_text(
        "⚠️ <b>KONFIRMASI HAPUS SEMUA NOMOR</b>\n\n"
        "Apakah kamu yakin ingin menghapus <b>SEMUA</b> nomor?\n"
        "Tindakan ini <b>tidak bisa dibatalkan</b>!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [btn("✅ Ya, Hapus Semua!", callback_data="adm_del_all_confirm", style="danger")],
            [btn("❌ Batal",            callback_data="adm_del_numbers",     style="secondary")],
        ])
    )
    return ADMIN_MAIN

async def _do_delete_numbers(app_or_ctx, chat_id: int, numbers_to_del: list[str], label: str):
    """Helper: hapus list nomor satu per satu via API, kirim progres & hasil."""
    total     = len(numbers_to_del)
    ok_count  = 0
    fail_count= 0
    errors    = []

    progress_msg = await app_or_ctx.bot.send_message(
        chat_id=chat_id,
        text=f"⏳ Menghapus {total} nomor... (0/{total})"
    )

    for i, number in enumerate(numbers_to_del, 1):
        # Coba DELETE /api/v1/numbers/{number}
        clean = number.strip().lstrip("+")
        result = await tw_delete(f"/api/v1/numbers/{number.strip()}")
        status = result.get("_status", 0) if result else 0
        if status in (200, 204, 201):
            ok_count += 1
        else:
            # Fallback: coba pakai clean number
            result2 = await tw_delete(f"/api/v1/numbers/{clean}")
            s2 = result2.get("_status", 0) if result2 else 0
            if s2 in (200, 204, 201):
                ok_count += 1
            else:
                fail_count += 1
                err = (result2 or result or {}).get("message", "") or (result2 or result or {}).get("_text", "")
                errors.append(f"{number}: {str(err)[:50]}")

        # Update progres setiap 10 nomor
        if i % 10 == 0 or i == total:
            try:
                await app_or_ctx.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_msg.message_id,
                    text=f"⏳ Menghapus {total} nomor... ({i}/{total})\n✅ Berhasil: {ok_count} | ❌ Gagal: {fail_count}"
                )
            except Exception:
                pass
        await asyncio.sleep(0.1)  # rate limit

    err_text = ""
    if errors:
        err_text = "\n\n<b>Error sample:</b>\n" + html.escape("\n".join(errors[:5]))
    await app_or_ctx.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ <b>Selesai hapus {label}</b>\n\n"
            f"✅ Berhasil : {ok_count}\n"
            f"❌ Gagal    : {fail_count}\n"
            f"📊 Total    : {total}{err_text}"
        ),
        parse_mode="HTML"
    )

async def adm_del_all_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Eksekusi hapus semua nomor."""
    q = update.callback_query; await q.answer("⏳ Mengambil daftar nomor...")
    await q.edit_message_text("⏳ Mengambil daftar semua nomor...", parse_mode="HTML")

    data = await tw_get("/api/v1/numbers", {"pageSize": 3000})
    if not data or "_error" in data:
        await ctx.bot.send_message(q.message.chat_id, f"❌ Gagal ambil nomor: {data}")
        return ADMIN_MAIN

    rows = data.get("rows", [])
    if not rows:
        await ctx.bot.send_message(q.message.chat_id, "ℹ️ Tidak ada nomor untuk dihapus.")
        return ADMIN_MAIN

    numbers = [r.get("number", "") for r in rows if r.get("number")]
    await _do_delete_numbers(ctx, q.message.chat_id, numbers, "semua nomor")
    return ADMIN_MAIN

async def adm_del_by_range(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Minta input Range ID."""
    q = update.callback_query; await q.answer()
    await q.edit_message_text(
        "🎯 <b>Hapus per Range ID</b>\n\n"
        "Ketik <b>Range ID</b> (angka) yang nomornya ingin dihapus.\n"
        "💡 Lihat Range ID di menu <b>Rate Card</b> atau <b>Access List</b>.\n\n"
        "Contoh: <code>123</code>",
        parse_mode="HTML", reply_markup=_back_kb()
    )
    ctx.user_data["awaiting"] = "del_range_id"
    return ADMIN_MAIN

async def _adm_del_range_execute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Eksekusi hapus per range ID — dipanggil dari adm_main_text."""
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Range ID harus angka. Coba lagi:", reply_markup=_back_kb())
        ctx.user_data["awaiting"] = "del_range_id"
        return ADMIN_MAIN

    range_id = int(text)
    await update.message.reply_text(f"⏳ Mengambil nomor di range {range_id}...")

    data = await tw_get("/api/v1/numbers", {"pageSize": 3000, "rangeId": range_id})
    if not data or "_error" in data:
        await update.message.reply_text(f"❌ Gagal ambil nomor: {data}")
        return ADMIN_MAIN

    rows = data.get("rows", [])
    if not rows:
        await update.message.reply_text(f"ℹ️ Tidak ada nomor di range {range_id}.")
        return ADMIN_MAIN

    numbers = [r.get("number", "") for r in rows if r.get("number")]
    await _do_delete_numbers(ctx, update.effective_chat.id, numbers, f"range {range_id} ({len(numbers)} nomor)")
    return ADMIN_MAIN

async def adm_del_by_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Minta user upload file .txt."""
    q = update.callback_query; await q.answer()
    await q.edit_message_text(
        "📄 <b>Hapus via File .txt</b>\n\n"
        "Upload file <b>.txt</b> yang berisi nomor telepon,\n"
        "satu nomor per baris.\n\n"
        "Contoh isi file:\n"
        "<code>+6281234567890\n"
        "+6289876543210\n"
        "628111222333</code>\n\n"
        "Format nomor: boleh pakai + atau tidak.",
        parse_mode="HTML", reply_markup=_back_kb()
    )
    ctx.user_data["awaiting"] = "del_file"
    return ADMIN_MAIN

async def adm_del_file_receive(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Terima file .txt dan proses hapus nomor di dalamnya."""
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Harus upload file .txt. Coba lagi:", reply_markup=_back_kb())
        return ADMIN_MAIN

    ctx.user_data.pop("awaiting", None)
    await update.message.reply_text("⏳ Membaca file...")

    try:
        file = await ctx.bot.get_file(doc.file_id)
        raw  = await file.download_as_bytearray()
        content = raw.decode("utf-8", errors="ignore")
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal baca file: {e}")
        return ADMIN_MAIN

    numbers = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if not numbers:
        await update.message.reply_text("❌ File kosong atau tidak ada nomor yang valid.")
        return ADMIN_MAIN

    await update.message.reply_text(f"📄 Ditemukan {len(numbers)} nomor. Memulai penghapusan...")
    await _do_delete_numbers(ctx, update.effective_chat.id, numbers, f"dari file ({len(numbers)} nomor)")
    return ADMIN_MAIN

# ── Message History (/messages) ────────────────────────────────────────────────

async def _send_traffic_page(target, ctx, page: int, keyword: str, edit: bool = False):
    params = {"page": page, "per_page": PAGE_SIZE, "type": "all"}
    if keyword and keyword != "-":
        params["number"] = keyword
    data = await panel_get("/messages", params)
    if not data or "_error" in data:
        text = (
            "❌ <b>Gagal mengambil Message History</b>\n\n"
            f"<code>{html.escape(_api_error_text(data))}</code>"
        )
        if edit: await target.edit_message_text(text, parse_mode="HTML", reply_markup=_back_kb())
        else:    await target.reply_text(text, parse_mode="HTML", reply_markup=_back_kb())
        return ADMIN_MAIN
    rows, pagination = _response_rows(data, "data", "messages", "items", "rows")
    total = (
        pagination.get("total")
        or data.get("total")
        or data.get("total_count")
        or len(rows)
    )
    if not rows:
        text = "📨 <b>Traffic</b>\n\nTidak ada traffic."
        if edit: await target.edit_message_text(text, parse_mode="HTML", reply_markup=_back_kb())
        else:    await target.reply_text(text, parse_mode="HTML", reply_markup=_back_kb())
        return ADMIN_MAIN
    ctx.user_data["tf_page"]    = page
    ctx.user_data["tf_keyword"] = keyword
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    lines = [f"📨 <b>Traffic</b> — {total} pesan  (hal. {page}/{total_pages})\n"]
    for r in rows:
        status = str(r.get("status", "?")).lower()
        icon   = "✅" if status == "delivered" else "⏳"
        lines.append(
            f"{icon} <b>{html.escape(str(r.get('source','?')))}</b> → "
            f"{html.escape(str(r.get('number','?')))}\n"
            f"   💬 {html.escape(str(r.get('message',''))[:80])}\n"
            f"   ⏱ {html.escape(str(r.get('received_at','?'))[:16])} | "
            f"{html.escape(status)} | {html.escape(str(r.get('type','?')))}"
        )
    kb = _page_kb("adm_tf", page, total)
    text = "\n".join(lines)
    if edit: await target.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:    await target.reply_text(text, parse_mode="HTML", reply_markup=kb)
    return ADMIN_MAIN

async def adm_traffic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text(
        "📨 <b>Message History</b>\nKetik nomor tujuan atau <code>-</code> untuk semua:",
        parse_mode="HTML", reply_markup=_back_kb()
    )
    return TRAFFIC_SEARCH

async def adm_traffic_result(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyword = update.message.text.strip()
    await _send_traffic_page(update.message, ctx, 1, keyword, edit=False)
    return ADMIN_MAIN

async def adm_traffic_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    page    = int(q.data.split(":")[1])
    keyword = ctx.user_data.get("tf_keyword", "-")
    await _send_traffic_page(q, ctx, page, keyword, edit=True)
    return ADMIN_MAIN

# ── Legacy allocation screens (not exposed for Augestel) ────────────────────────

def _alloc_panel_text(cfg_range, cfg_qty) -> str:
    status = "✅ Siap dialokasikan" if cfg_range else "⚠️ Range ID belum diset!"
    return (
        f"⚡ <b>Allocate Number</b>\n\n"
        f"  🆔 Range ID  : <code>{cfg_range or 'belum diset'}</code>\n"
        f"  📦 Jumlah    : <code>{cfg_qty}</code> nomor\n"
        f"  📊 Status    : {status}\n\n"
        f"{'Klik <b>🚀 Alokasi Sekarang</b> untuk langsung eksekusi.' if cfg_range else 'Atur Range ID dulu melalui <b>⚙️ Setting Alokasi</b>.'}"
    )

def _alloc_panel_kb(cfg_range, cfg_qty) -> InlineKeyboardMarkup:
    rows = []
    if cfg_range:
        rows.append([btn("🚀 Alokasi Sekarang", callback_data="adm_alloc_confirm", style="success")])
    qty_row = [
        btn(f"{'✅' if cfg_qty==n else ''}{n}", callback_data=f"adm_alloc_qty_set:{n}",
            style="success" if cfg_qty == n else "secondary")
        for n in [5, 10, 20, 50, 100]
    ]
    rows.append(qty_row)
    rows.append([
        btn("✏️ Custom Jumlah", callback_data="adm_alloc_custom_qty",  style="primary"),
        btn("✏️ Set Range ID",  callback_data="adm_alloc_range_input", style="primary"),
    ])
    rows.append([btn("🔙 Kembali", callback_data="adm_back", style="secondary")])
    return InlineKeyboardMarkup(rows)

async def adm_alloc_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    settings  = load_settings()
    alloc_cfg = settings.get("allocation", {})
    cfg_range = alloc_cfg.get("rangeId")
    cfg_qty   = alloc_cfg.get("quantity", 10)
    await q.edit_message_text(
        _alloc_panel_text(cfg_range, cfg_qty),
        parse_mode="HTML",
        reply_markup=_alloc_panel_kb(cfg_range, cfg_qty)
    )
    return ADMIN_MAIN

async def adm_alloc_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Eksekusi alokasi langsung pakai setting yang tersimpan."""
    q = update.callback_query; await q.answer("⏳ Mengalokasikan nomor...")
    settings  = load_settings()
    alloc_cfg = settings.get("allocation", {})
    range_id  = alloc_cfg.get("rangeId")
    quantity  = alloc_cfg.get("quantity", 10)

    if not range_id:
        await q.answer("❌ Range ID belum diset! Atur dulu di Setting Alokasi.", show_alert=True)
        return ADMIN_MAIN

    await q.edit_message_text(
        f"⏳ <b>Mengalokasikan {quantity} nomor dari range {range_id}...</b>\nMohon tunggu.",
        parse_mode="HTML"
    )

    result = await tw_post("/api/v1/numbers/allocate", {"rangeId": int(range_id), "quantity": int(quantity)})

    if not result:
        await ctx.bot.send_message(
            chat_id=q.message.chat_id,
            text="❌ Gagal: tidak ada response dari API.",
            reply_markup=admin_main_keyboard()
        )
        return ADMIN_MAIN

    status = result.get("_status", 0)
    if status not in (200, 201):
        err = result.get("message") or result.get("error") or result.get("code") or str(result)
        await ctx.bot.send_message(
            chat_id=q.message.chat_id,
            text=(
                f"❌ <b>Gagal alokasi (HTTP {status})</b>\n\n"
                f"Error: <code>{html.escape(str(err)[:300])}</code>\n\n"
                f"💡 Cek Range ID di Rate Card / Access List."
            ),
            parse_mode="HTML",
            reply_markup=admin_main_keyboard()
        )
        return ADMIN_MAIN

    allocated = result.get("allocated", result.get("count", 0))
    remaining = result.get("remainingToday", result.get("remaining", "?"))
    numbers   = result.get("numbers", result.get("data", []))

    if numbers:
        num_lines = "\n".join(f"  • <code>{n['number'] if isinstance(n, dict) else n}</code>" for n in numbers[:25])
        more = f"\n  … dan {len(numbers)-25} lainnya" if len(numbers) > 25 else ""
    else:
        num_lines = "<i>(daftar nomor tidak tersedia di response)</i>"
        more = ""

    await ctx.bot.send_message(
        chat_id=q.message.chat_id,
        text=(
            f"✅ <b>Berhasil alokasi {allocated} nomor!</b>\n"
            f"🆔 Range ID   : <code>{range_id}</code>\n"
            f"🟢 Sisa hari ini: {remaining}\n\n"
            f"{num_lines}{more}"
        ),
        parse_mode="HTML",
        reply_markup=admin_main_keyboard()
    )
    return ADMIN_MAIN

async def adm_alloc_qty_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Set qty langsung via tombol inline tanpa ketik."""
    q = update.callback_query; await q.answer()
    qty = int(q.data.split(":")[1])
    settings = load_settings()
    if "allocation" not in settings:
        settings["allocation"] = {}
    settings["allocation"]["quantity"] = qty
    save_settings(settings)
    alloc_cfg = settings["allocation"]
    cfg_range = alloc_cfg.get("rangeId")
    await q.edit_message_text(
        _alloc_panel_text(cfg_range, qty),
        parse_mode="HTML",
        reply_markup=_alloc_panel_kb(cfg_range, qty)
    )
    return ADMIN_MAIN

async def adm_alloc_range_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Minta input Range ID via teks."""
    q = update.callback_query; await q.answer()
    await q.edit_message_text(
        "✏️ <b>Set Range ID</b>\n\n"
        "Ketik <b>Range ID</b> (angka) yang ingin dialokasikan.\n"
        "💡 Lihat ID di menu <b>Rate Card</b> atau <b>Access List</b>.\n\n"
        "Contoh: <code>123</code>",
        parse_mode="HTML",
        reply_markup=_back_kb()
    )
    ctx.user_data["awaiting"] = "alloc_range_id"
    return ADMIN_MAIN

async def adm_alloc_custom_qty(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Minta input custom quantity via teks."""
    q = update.callback_query; await q.answer()
    await q.edit_message_text(
        "✏️ <b>Custom Jumlah Nomor</b>\n\n"
        "Ketik jumlah nomor yang ingin dialokasikan (1 – 1000):\n\n"
        "Contoh: <code>250</code>",
        parse_mode="HTML", reply_markup=_back_kb()
    )
    ctx.user_data["awaiting"] = "alloc_custom_qty"
    return ADMIN_MAIN

async def adm_alloc_range(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Terima input Range ID — dipanggil dari adm_main_text saat awaiting=alloc_range_id."""
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text(
            "❌ Range ID harus berupa angka. Coba lagi:",
            reply_markup=_back_kb()
        )
        return ADMIN_MAIN
    settings = load_settings()
    if "allocation" not in settings:
        settings["allocation"] = {}
    settings["allocation"]["rangeId"] = int(text)
    save_settings(settings)
    cfg_qty = settings["allocation"].get("quantity", 10)
    await update.message.reply_text(
        _alloc_panel_text(int(text), cfg_qty),
        parse_mode="HTML",
        reply_markup=_alloc_panel_kb(int(text), cfg_qty)
    )
    return ADMIN_MAIN

# ── Setting Alokasi ─────────────────────────────────────────────────────────────

async def adm_alloc_cfg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    settings  = load_settings()
    alloc_cfg = settings.get("allocation", {"rangeId": None, "quantity": 10})
    await q.edit_message_text(
        f"⚙️ <b>Setting Alokasi Default</b>\n\n"
        f"Range ID saat ini : <code>{alloc_cfg.get('rangeId','belum diset')}</code>\n"
        f"Quantity saat ini : <code>{alloc_cfg.get('quantity',10)}</code>\n\n"
        f"Format: <code>rangeId quantity</code>\nContoh: <code>123 50</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[btn("🔙 Kembali", callback_data="adm_back", style="secondary")]])
    )
    ctx.user_data["awaiting"] = "alloc_cfg"
    return ADMIN_MAIN

# ── Kelola Grup ─────────────────────────────────────────────────────────────────

async def adm_groups(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    settings = load_settings()
    groups   = settings.get("groups", {})
    lines    = ["👥 <b>Daftar Grup</b>\n"]
    for nama, data in groups.items():
        lines.append(f"• <b>{html.escape(nama)}</b> — ID: <code>{html.escape(str(data['id']))}</code>")
    if not groups:
        lines.append("<i>Belum ada grup yang didaftarkan.</i>")
    lines.append("\n<i>Gunakan perintah teks:</i>\n"
                 "/addid nama id_grup\n"
                 "/delid nama_grup")
    await q.edit_message_text("\n".join(lines), parse_mode="HTML",
                              disable_web_page_preview=True, reply_markup=_back_kb())
    return ADMIN_MAIN

# ── Tambah Admin ────────────────────────────────────────────────────────────────

async def adm_add_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    settings    = load_settings()
    extra_admins = settings.get("extra_admins", [])
    lines = [f"👑 <b>Daftar Admin</b>\n",
             f"• Owner: <code>{ADMIN_ID}</code>"]
    for aid in extra_admins:
        lines.append(f"• <code>{aid}</code>")
    lines.append("\nGunakan:\n/addadmin &lt;user_id&gt;\n/deladmin &lt;user_id&gt;")
    await q.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=_back_kb())
    return ADMIN_MAIN

def _back_kb():
    return InlineKeyboardMarkup([[btn("🔙 Kembali", callback_data="adm_back", style="secondary")]])

# ── 📈 Statistik Harian ─────────────────────────────────────────────────────────

async def adm_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Memuat statistik...")
    kb = InlineKeyboardMarkup([
        [btn("📊 Kirim ke Grup Utama", callback_data="adm_stats_send", style="primary")],
        [btn("🔙 Kembali",             callback_data="adm_back",      style="secondary")],
    ])
    await q.edit_message_text(build_stats_text(), parse_mode="HTML", reply_markup=kb)
    return ADMIN_MAIN

async def adm_stats_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await send_daily_stats(ctx.application)
    await q.edit_message_text("✅ Statistik berhasil dikirim ke grup utama.", reply_markup=_back_kb())
    return ADMIN_MAIN

# ── 📋 Download Log ─────────────────────────────────────────────────────────────

async def adm_dl_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Mempersiapkan log...")
    if not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0:
        await q.edit_message_text("📋 Log masih kosong, belum ada OTP yang dicatat.", reply_markup=_back_kb())
        return ADMIN_MAIN
    try:
        size_kb = os.path.getsize(LOG_FILE) / 1024
        with open(LOG_FILE, "rb") as f:
            buf = io.BytesIO(f.read())
        fname = f"otp_log_{now_wib().strftime('%Y%m%d_%H%M%S')}.txt"
        buf.name = fname
        await q.edit_message_text(f"📋 Mengirim log ({size_kb:.1f} KB)...", reply_markup=_back_kb())
        await ctx.bot.send_document(
            chat_id=q.message.chat_id, document=buf, filename=fname,
            caption=f"📋 OTP Log\n⏱ {now_wib().strftime('%Y-%m-%d %H:%M:%S WIB')}"
        )
    except Exception as e:
        await ctx.bot.send_message(chat_id=q.message.chat_id, text=f"❌ Gagal: {e}")
    return ADMIN_MAIN

# ── ✏️ Format Pesan Custom ──────────────────────────────────────────────────────

async def adm_format(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    json_tpl = get_otp_json_template()
    if json_tpl:
        current_disp = "<code>JSON custom (teks + tombol)</code> — kirim template baru atau <code>reset</code>"
    else:
        current = get_custom_format()
        current_disp = ("<code>premium (bawaan)</code>" if current == DEFAULT_FORMAT
                         else f"<code>{html.escape(current)}</code>")
    kb = InlineKeyboardMarkup([
        [btn("👁 Preview Template Saat Ini", callback_data="adm_format_preview", style="primary")],
        [btn("🧪 Test Kirim OTP Acak ke Grup", callback_data="adm_format_test",   style="success")],
        [btn("🔙 Kembali",                    callback_data="adm_back",          style="secondary")],
    ])
    await q.edit_message_text(
        f"{FORMAT_HELP}\n\n<b>Format saat ini:</b>\n{current_disp}",
        parse_mode="HTML", reply_markup=kb
    )
    return FORMAT_INPUT

async def adm_format_preview(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Tampilkan preview LANGSUNG dari template yang sedang aktif, memakai data
    SMS acak (negara/layanan/nomor/OTP acak) yang dirender lewat pipeline yang
    SAMA PERSIS dengan pesan OTP asli. Hanya tampil di chat admin, TIDAK dikirim
    ke grup manapun."""
    q = update.callback_query; await q.answer("Membuat preview...")
    dummy = generate_dummy_otp()
    try:
        text_msg, kb_override, otp = build_final_otp_message(**dummy)
        await q.message.reply_text(text_msg, parse_mode="HTML", reply_markup=kb_override)
        note = (
            "⬆️ Itu <b>preview</b> template kamu saat ini (data acak, hanya tampil "
            "di sini, <u>tidak</u> dikirim ke grup)."
        )
        warn = _nested_emoji_in_code_warning(text_msg)
        if warn:
            note += f"\n\n{warn}"
        await q.message.reply_text(note, parse_mode="HTML", reply_markup=admin_main_keyboard())
    except BadRequest as e:
        await q.message.reply_text(
            f"⚠️ Template tidak bisa dirender: <code>{html.escape(str(e))}</code>\n\n"
            "Kemungkinan <code>style</code> atau <code>icon</code> di JSON tidak valid.",
            parse_mode="HTML", reply_markup=admin_main_keyboard()
        )
    return ADMIN_MAIN

async def adm_format_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Kirim 1 OTP ACAK (negara/layanan/nomor/OTP acak, layanan asli seperti
    WhatsApp/Telegram/Apple/dll) ke grup utama + semua grup terdaftar, memakai
    template & rendering yang SAMA PERSIS dengan OTP asli -- untuk menguji
    tampilan template secara nyata di grup, 100% seperti pesan sungguhan."""
    q = update.callback_query; await q.answer("Mengirim OTP test ke grup...")
    dummy = generate_dummy_otp()
    try:
        text_msg, kb_override, otp = build_final_otp_message(**dummy)
    except BadRequest as e:
        await q.message.reply_text(
            f"⚠️ Template tidak bisa dirender: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML", reply_markup=admin_main_keyboard()
        )
        return ADMIN_MAIN

    ok = await send_to_telegram(ctx.application, text_msg, otp, dummy["message"], kb_override=kb_override)
    if ok:
        msg = (
            f"✅ <b>OTP test terkirim ke grup!</b>\n\n"
            f"Layanan : <code>{html.escape(dummy['source'])}</code>\n"
            f"Nomor   : <code>{dummy['clean_rc']}</code>\n"
            f"OTP     : <code>{otp}</code>"
        )
        warn = _nested_emoji_in_code_warning(text_msg)
        if warn:
            msg += f"\n\n{warn}"
        await q.message.reply_text(msg, parse_mode="HTML", reply_markup=admin_main_keyboard())
    else:
        await q.message.reply_text(
            "❌ Gagal mengirim OTP test (cek koneksi/permission bot ke grup).",
            reply_markup=admin_main_keyboard()
        )
    return ADMIN_MAIN

async def adm_format_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    # Emoji premium yang ditempel LANGSUNG di pesan ini (kalau ada) → {char: id}
    emoji_map = _custom_emoji_map(update.message)
    settings = load_settings()

    if text.lower() == "reset":
        settings["otp_format"] = DEFAULT_FORMAT
        settings["otp_json_template"] = None
        save_settings(settings)
        await update.message.reply_text(
            "✅ Format direset ke <b>premium bawaan</b>.", parse_mode="HTML",
            reply_markup=admin_main_keyboard()
        )
        return ADMIN_MAIN

    # ── Deteksi input JSON (teks + tombol) ──
    if text.startswith("{"):
        try:
            tpl = json.loads(text)
        except json.JSONDecodeError as e:
            await update.message.reply_text(
                f"❌ JSON tidak valid: <code>{html.escape(str(e))}</code>\n\n"
                "Perbaiki lalu kirim ulang, atau ketik <code>reset</code>.",
                parse_mode="HTML", reply_markup=_back_kb()
            )
            return FORMAT_INPUT
        if not isinstance(tpl, dict) or "text" not in tpl:
            await update.message.reply_text(
                "❌ JSON harus berupa object dan minimal punya field <code>\"text\"</code>.\n\n"
                "Ketik <code>/formatjson</code> untuk contoh lengkap.",
                parse_mode="HTML", reply_markup=_back_kb()
            )
            return FORMAT_INPUT

        # Suntik emoji premium yang ditempel langsung: di field "text" jadi tag
        # <tg-emoji> (biar tampil sebagai emoji premium beneran di pesan HTML),
        # dan di field "icon" tombol jadi ID mentahnya (sesuai format Bot API).
        if emoji_map:
            if isinstance(tpl.get("text"), str):
                tpl["text"] = _wrap_custom_emoji_html(tpl["text"], emoji_map)
            for b in tpl.get("buttons", []) or []:
                if isinstance(b, dict):
                    icon_val = b.get("icon")
                    if isinstance(icon_val, str) and icon_val in emoji_map:
                        b["icon"] = emoji_map[icon_val]

        settings["otp_json_template"] = tpl
        save_settings(settings)

        dummy = generate_dummy_otp()
        try:
            preview_text, preview_kb, _otp = build_final_otp_message(**dummy)
            n_btn = sum(len(row) for row in (preview_kb.inline_keyboard if preview_kb else []))
            warn = _nested_emoji_in_code_warning(preview_text)
            msg = f"✅ Template JSON disimpan! ({n_btn} tombol)\n\n<b>Preview teks:</b>\n{preview_text}"
            if warn:
                msg += f"\n\n{warn}"
            await update.message.reply_text(
                msg, parse_mode="HTML", reply_markup=preview_kb or admin_main_keyboard()
            )
            if preview_kb:
                await update.message.reply_text(
                    "⬆️ Preview langsung (data acak) — dirender dengan pipeline yang "
                    "sama persis dengan OTP asli. Pakai <b>🧪 Test Kirim OTP Acak</b> "
                    "di menu Format Pesan untuk mengetesnya di grup sungguhan.",
                    reply_markup=admin_main_keyboard()
                )
        except BadRequest as e:
            # Biasanya karena "icon" (icon_custom_emoji_id) bukan ID emoji premium
            # yang valid. Template tetap tersimpan; admin tinggal kirim ulang JSON
            # dengan ID emoji yang benar atau tanpa field "icon".
            await update.message.reply_text(
                f"⚠️ Template tersimpan, tapi Telegram menolak tombolnya: "
                f"<code>{html.escape(str(e))}</code>\n\n"
                "Kemungkinan penyebab: nilai <code>icon</code> (ID emoji premium) "
                "tidak valid, atau <code>style</code> tidak dikenal. Kirim ulang JSON "
                "dengan ID emoji yang benar, atau hapus field <code>icon</code>.",
                parse_mode="HTML", reply_markup=admin_main_keyboard()
            )
        return ADMIN_MAIN

    # ── Format teks biasa (mode lama) ──
    # Kalau ada emoji premium yang ditempel langsung, bungkus jadi tag <tg-emoji>
    # supaya tetap tampil sebagai emoji premium saat template dipakai nanti.
    text = _wrap_custom_emoji_html(text, emoji_map)
    settings["otp_format"] = text
    settings["otp_json_template"] = None
    save_settings(settings)
    preview = apply_custom_format(
        text, flag="🇵🇰", country="PK", service="Apple",
        number="+9230✨1234", otp="123456",
        sender="Apple", range_name="Pakistan - Jazz",
        message="Your code is 123456"
    )
    warn = _nested_emoji_in_code_warning(preview)
    msg = f"✅ Format disimpan!\n\n<b>Preview:</b>\n{preview}"
    if warn:
        msg += f"\n\n{warn}"
    await _safe_reply_html(
        update.message,
        msg,
        reply_markup=admin_main_keyboard()
    )
    return ADMIN_MAIN

async def formatjson_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Perintah teks /formatjson — tampilkan contoh template JSON teks+tombol."""
    if not is_admin(update.effective_user.id): return
    example = {
        "text": "{flag} <b>{iso}</b> | {svc_icon} | <b>{number_masked}</b>",
        "buttons": [
            {"type": "otp", "label": "🔑 {otp}", "value": "{otp}", "style": "success",
             "icon": "5465443379917629504"},
            {"type": "sep"},
            {"type": "link", "label": "☎️ Number", "value": "https://t.me/infosemuahanz",
             "style": "primary", "icon": "5296369303661067030"},
            {"type": "link", "label": "🔔 Chanel", "value": "https://t.me/serbagunasekali",
             "style": "primary"},
        ],
    }
    pretty = html.escape(json.dumps(example, indent=2, ensure_ascii=False))
    await update.message.reply_text(
        "🧩 <b>Contoh Template JSON</b>\n\n"
        f"<pre>{pretty}</pre>\n\n"
        "Tempel JSON seperti ini lewat menu <b>✏️ Format Pesan</b> untuk mengubah "
        "teks + tombol pesan OTP yang dikirim ke grup sekaligus.\n\n"
        "Tipe tombol: <code>otp</code>, <code>copy</code>, <code>link</code>, <code>sep</code> (baris baru).\n"
        "<b>style</b> — warna tombol asli Telegram (bukan emoji). Hanya 3 nilai resmi: "
        "<code>primary</code> (biru), <code>success</code> (hijau), <code>danger</code> (merah). "
        "Nilai lain otomatis dipetakan ke warna terdekat.\n"
        "<b>icon</b> — ID emoji premium/custom emoji Telegram (opsional) yang tampil "
        "di depan teks tombol.",
        parse_mode="HTML"
    )



# ── 👥 Multi Account Manager ────────────────────────────────────────────────────

def _accounts_keyboard():
    accounts = load_accounts()
    rows = []
    for acc in accounts:
        name = acc.get("name", "?")
        status, runtime = _account_status(name)
        status_text = _status_label(name, runtime, str(acc.get("api_key", "")))
        icon = status_text.split(" ", 1)[0]
        rows.append([
            btn(f"{icon} {name}", callback_data=f"adm_acc_select:{name}", style="success" if status == "running" else "primary"),
            btn("▶️", callback_data=f"adm_acc_start:{name}", style="success"),
            btn("⏹", callback_data=f"adm_acc_stop:{name}", style="danger"),
        ])
        rows.append([
            btn("🔌 Test", callback_data=f"adm_acc_test:{name}", style="primary"),
            btn("🔄 Restart", callback_data=f"adm_acc_restart:{name}", style="primary"),
            btn("🔑 Ganti Key", callback_data=f"adm_acc_key:{name}", style="primary"),
            btn("🗑 Hapus", callback_data=f"adm_acc_del:{name}", style="danger"),
        ])
        rows.append([
            btn("🔐 Webhook Secret", callback_data=f"adm_acc_webhook:{name}", style="primary"),
        ])
    rows.append([
        btn("▶️ Start All", callback_data="adm_acc_start_all", style="success"),
        btn("🔄 Restart All", callback_data="adm_acc_restart_all", style="primary"),
        btn("⏹ Stop All", callback_data="adm_acc_stop_all", style="danger"),
    ])
    rows.append([btn("➕ Tambah Akun Baru", callback_data="adm_acc_add", style="success")])
    rows.append([btn("🔃 Refresh Dashboard", callback_data="adm_acc_refresh", style="primary")])
    rows.append([btn("🔙 Kembali",          callback_data="adm_back",   style="secondary")])
    return InlineKeyboardMarkup(rows)

async def adm_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    accounts = load_accounts()
    active = get_active_account()
    active_name = active.get("name") if active else ""
    lines = ["👥 <b>Multi Akun Augestel</b>\n"]
    for i, acc in enumerate(accounts, 1):
        name = acc.get("name", "?")
        base = acc.get("base_url", "?")
        status, runtime = _account_status(name)
        selected = " ✅ aktif untuk panel" if name == active_name else ""
        status_label = _status_label(name, runtime, str(acc.get("api_key", "")))
        error = f"\n   ⚠️ Error: <code>{html.escape(runtime.get('last_error', ''))}</code>" if runtime.get("last_error") else ""
        cooldown = _rate_limit_wait(name, str(acc.get("api_key", "")))
        cooldown_line = f"\n   ⏳ Cooldown: <b>{cooldown} detik</b>" if cooldown else ""
        lines.append(
            f"{i}. <b>{html.escape(name)}</b>{selected}\n"
            f"   Status: <b>{status_label}</b>\n"
            f"   🌐 {html.escape(base)}\n"
            f"   🔑 <code>{_mask_api_key(acc.get('api_key', ''))}</code>\n"
            f"   🔐 Webhook: <code>{_mask_webhook_secret(acc.get('webhook_secret', ''))}</code>\n"
            f"   Last polling: <b>{_format_when(runtime.get('last_poll_at', 0))}</b>\n"
            f"   Next polling: <b>{_format_countdown(runtime.get('next_poll_at', 0))}</b>\n"
            f"   API latency: <b>{runtime.get('last_latency_ms', 0) or '—'} ms</b> | "
            f"HTTP: <b>{runtime.get('last_http_status', 0) or '—'}</b>\n"
            f"   OTP hari ini: <b>{_account_today_otp_count(name)}</b> | "
            f"Berhasil: <b>{runtime.get('success_count', 0)}</b> | "
            f"Error: <b>{runtime.get('error_count', 0)}</b>"
            f"{cooldown_line}{error}"
        )
    if not accounts:
        lines.append("Belum ada akun. Tambahkan akun pertama untuk mulai polling.")
    lines.append("\nPilih nama akun untuk menjadikannya akun aktif pada menu panel.")
    await q.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=_accounts_keyboard())
    return ADMIN_MAIN

async def adm_acc_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Dashboard diperbarui.")
    return await adm_accounts(update, ctx)

async def adm_acc_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    name = _account_name_from_callback(q.data)
    account = next((a for a in load_accounts() if a.get("name") == name), None)
    if not account:
        await q.answer("Akun tidak ditemukan.", show_alert=True)
        return ADMIN_MAIN
    if not account.get("api_key"):
        await q.answer("API key belum diisi.", show_alert=True)
        return ADMIN_MAIN

    await q.answer("Menguji koneksi...")
    started_at = time.perf_counter()
    result = await panel_get("/messages", {"page": 1, "per_page": 1, "type": "all"}, account)
    elapsed_ms = round((time.perf_counter() - started_at) * 1000)
    if result and "_error" not in result:
        text = (
            f"✅ <b>Koneksi berhasil</b>\n\n"
            f"Akun: <b>{html.escape(name)}</b>\n"
            f"Latency: <b>{elapsed_ms} ms</b>\n"
            f"Endpoint: <code>/messages</code>\n"
            f"Status: <b>Healthy</b>"
        )
    else:
        text = (
            f"❌ <b>Koneksi gagal</b>\n\n"
            f"Akun: <b>{html.escape(name)}</b>\n"
            f"Latency: <b>{elapsed_ms} ms</b>\n"
            f"Detail: <code>{html.escape(_api_error_text(result))}</code>"
        )
    await q.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [btn("🔃 Kembali ke Dashboard", callback_data="adm_acc_refresh", style="primary")]
        ])
    )
    return ADMIN_MAIN

async def adm_acc_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    name = _account_name_from_callback(q.data)
    accounts = load_accounts()
    if not any(a.get("name") == name for a in accounts):
        await q.answer("Akun tidak ditemukan.", show_alert=True)
        return ADMIN_MAIN
    settings = load_settings()
    settings["active_account"] = name
    save_settings(settings)
    await q.answer(f"{name} dipilih sebagai akun aktif untuk panel.")
    return await adm_accounts(update, ctx)

async def adm_acc_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    name = _account_name_from_callback(q.data)
    account = next((a for a in load_accounts() if a.get("name") == name), None)
    if not account:
        await q.answer("Akun tidak ditemukan.", show_alert=True)
        return ADMIN_MAIN
    result = await start_account_worker(ctx.application, account)
    await q.answer({
        "started": "Polling akun dimulai.",
        "already_running": "Akun sudah berjalan.",
        "missing_key": "API key belum diisi.",
    }.get(result, result), show_alert=result == "missing_key")
    return await adm_accounts(update, ctx)

async def adm_acc_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Restarting...")
    name = _account_name_from_callback(q.data)
    account = next((a for a in load_accounts() if a.get("name") == name), None)
    if not account:
        await q.answer("Akun tidak ditemukan.", show_alert=True)
        return ADMIN_MAIN
    result = await start_account_worker(ctx.application, account, restart=True)
    await q.answer("Polling akun di-restart." if result == "started" else "API key belum diisi.", show_alert=result != "started")
    return await adm_accounts(update, ctx)

async def adm_acc_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    name = _account_name_from_callback(q.data)
    await stop_account_worker(name)
    return await adm_accounts(update, ctx)

async def adm_acc_start_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Starting all...")
    started, skipped = await start_all_workers(ctx.application)
    await q.answer(f"{started} akun aktif" + (f", {skipped} dilewati" if skipped else ""))
    return await adm_accounts(update, ctx)

async def adm_acc_restart_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Restarting all...")
    started, skipped = await start_all_workers(ctx.application, restart=True)
    await q.answer(f"{started} akun di-restart" + (f", {skipped} dilewati" if skipped else ""))
    return await adm_accounts(update, ctx)

async def adm_acc_stop_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Stopping all...")
    stopped = await stop_all_workers()
    await q.answer(f"{stopped} worker dihentikan.")
    return await adm_accounts(update, ctx)

async def adm_acc_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    name = q.data.split(":", 1)[1]
    accounts = load_accounts()
    new_list = [a for a in accounts if a.get("name") != name]
    if len(new_list) == len(accounts):
        await q.answer(f"Akun '{name}' tidak ditemukan.", show_alert=True)
        return ADMIN_MAIN
    await stop_account_worker(name)
    _save_accounts(new_list)
    settings = load_settings()
    if settings.get("active_account") == name:
        settings["active_account"] = new_list[0].get("name", "") if new_list else ""
        save_settings(settings)
    await q.edit_message_text(
        f"🗑️ Akun <b>{name}</b> berhasil dihapus dan worker-nya dihentikan.",
        parse_mode="HTML", reply_markup=_accounts_keyboard()
    )
    return ADMIN_MAIN

async def adm_acc_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text(
        "➕ <b>Tambah Akun Baru</b>\n\nKetik <b>nama akun</b> (contoh: <code>akun2</code>):",
        parse_mode="HTML", reply_markup=_back_kb()
    )
    return ACC_ADD_NAME

async def adm_acc_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name or len(name) > 40 or ":" in name or "\n" in name:
        await update.message.reply_text(
            "❌ Nama akun tidak valid. Gunakan 1–40 karakter tanpa tanda titik dua.",
            reply_markup=_back_kb()
        )
        return ACC_ADD_NAME
    ctx.user_data["new_acc_name"] = name
    await update.message.reply_text(
        f"✅ Nama: <code>{ctx.user_data['new_acc_name']}</code>\n\n"
        f"Endpoint otomatis: <code>{PANEL_BASE}</code>\n\n"
        "Ketik <b>API Key</b>:",
        parse_mode="HTML", reply_markup=_back_kb()
    )
    return ACC_ADD_KEY

async def adm_acc_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key  = update.message.text.strip()
    name = ctx.user_data.get("new_acc_name", "")
    if not name:
        await update.message.reply_text("❌ Data tidak lengkap. Mulai ulang dari /admin.")
        return ADMIN_MAIN
    if not key:
        await update.message.reply_text(
            "❌ API key tidak boleh kosong. Kirim API key yang valid:",
            reply_markup=_back_kb(),
        )
        return ACC_ADD_KEY
    accounts = load_accounts()
    if any(a.get("name") == name for a in accounts):
        await update.message.reply_text(
            f"❌ Nama akun <code>{name}</code> sudah ada. Pilih nama lain.",
            parse_mode="HTML", reply_markup=admin_main_keyboard()
        )
        return ADMIN_MAIN
    ctx.user_data["new_acc_key"] = key
    await update.message.reply_text(
        "🔐 <b>Webhook secret akun (opsional)</b>\n\n"
        "Kirim secret webhook untuk akun ini, atau ketik <code>skip</code> "
        "jika akun hanya memakai polling API key.",
        parse_mode="HTML", reply_markup=_back_kb()
    )
    return ACC_ADD_WEBHOOK_SECRET


async def adm_acc_webhook_secret_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    secret = update.message.text.strip()
    if secret.lower() in {"skip", "-", "kosong", "none", "reset"}:
        secret = ""
    name = str(ctx.user_data.get("new_acc_name", "")).strip()
    key = str(ctx.user_data.pop("new_acc_key", "")).strip()
    if not name or not key:
        ctx.user_data.pop("new_acc_name", None)
        await update.message.reply_text("❌ Data tidak lengkap. Mulai ulang dari /admin.")
        return ADMIN_MAIN
    accounts = load_accounts()
    if any(a.get("name") == name for a in accounts):
        ctx.user_data.pop("new_acc_name", None)
        await update.message.reply_text(
            f"❌ Nama akun <code>{html.escape(name)}</code> sudah ada. Pilih nama lain.",
            parse_mode="HTML", reply_markup=admin_main_keyboard()
        )
        return ADMIN_MAIN
    account = {
        "name": name,
        "base_url": PANEL_BASE,
        "api_key": key,
        "webhook_secret": secret,
    }
    accounts.append(account)
    _save_accounts(accounts)
    settings = load_settings()
    if not settings.get("active_account"):
        settings["active_account"] = name
        save_settings(settings)
    await start_account_worker(ctx.application, account)
    ctx.user_data.pop("new_acc_name", None)
    await update.message.reply_text(
        f"✅ Akun <b>{html.escape(name)}</b> berhasil ditambahkan dan polling otomatis dimulai.\n"
        f"Webhook: <b>{'aktif untuk akun ini' if secret else 'opsional / tidak dikonfigurasi'}</b>",
        parse_mode="HTML", reply_markup=admin_main_keyboard()
    )
    return ADMIN_MAIN

async def adm_acc_edit_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    name = _account_name_from_callback(q.data)
    if not any(a.get("name") == name for a in load_accounts()):
        await q.answer("Akun tidak ditemukan.", show_alert=True)
        return ADMIN_MAIN
    ctx.user_data["edit_acc_name"] = name
    await q.edit_message_text(
        f"🔑 <b>Ganti API key</b> untuk akun <code>{html.escape(name)}</code>\n\n"
        "Kirim API key baru. Worker akan di-restart otomatis setelah key disimpan.",
        parse_mode="HTML", reply_markup=_back_kb()
    )
    return ACC_EDIT_KEY

async def adm_acc_edit_key_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = ctx.user_data.pop("edit_acc_name", "")
    key = update.message.text.strip()
    accounts = load_accounts()
    account = next((a for a in accounts if a.get("name") == name), None)
    if not account or not key:
        await update.message.reply_text("❌ Akun atau API key tidak valid.", reply_markup=admin_main_keyboard())
        return ADMIN_MAIN
    account["api_key"] = key
    _save_accounts(accounts)
    await start_account_worker(ctx.application, account, restart=True)
    await update.message.reply_text(
        f"✅ API key akun <b>{html.escape(name)}</b> diperbarui. Polling sudah di-restart.",
        parse_mode="HTML", reply_markup=admin_main_keyboard()
    )
    return ADMIN_MAIN


async def adm_acc_webhook(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    name = _account_name_from_callback(q.data)
    account = next((a for a in load_accounts() if a.get("name") == name), None)
    if not account:
        await q.answer("Akun tidak ditemukan.", show_alert=True)
        return ADMIN_MAIN
    ctx.user_data["edit_acc_webhook_name"] = name
    await q.edit_message_text(
        f"🔐 <b>Webhook secret</b> untuk akun <code>{html.escape(name)}</code>\n\n"
        f"Secret saat ini: <code>{_mask_webhook_secret(account.get('webhook_secret', ''))}</code>\n\n"
        "Kirim secret baru. Ketik <code>reset</code> untuk menonaktifkan "
        "webhook akun ini. Perubahan berlaku setelah bot direstart.",
        parse_mode="HTML", reply_markup=_back_kb()
    )
    return ACC_EDIT_WEBHOOK_SECRET


async def adm_acc_webhook_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = str(ctx.user_data.pop("edit_acc_webhook_name", "")).strip()
    secret = update.message.text.strip()
    if secret.lower() in {"reset", "skip", "-", "kosong", "none"}:
        secret = ""
    accounts = load_accounts()
    account = next((a for a in accounts if a.get("name") == name), None)
    if not account:
        await update.message.reply_text(
            "❌ Akun tidak ditemukan.", reply_markup=admin_main_keyboard()
        )
        return ADMIN_MAIN
    account["webhook_secret"] = secret
    _save_accounts(accounts)
    await update.message.reply_text(
        f"✅ Webhook secret akun <b>{html.escape(name)}</b> diperbarui.\n"
        f"Status: <b>{'aktif' if secret else 'nonaktif'}</b>\n\n"
        "Restart bot agar konfigurasi listener webhook dimuat.",
        parse_mode="HTML", reply_markup=admin_main_keyboard()
    )
    return ADMIN_MAIN

# ── ⏱ Interval Polling ──────────────────────────────────────────────────────────

async def adm_poll_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    current = get_poll_interval()
    await q.edit_message_text(
        f"⏱ <b>Setting Interval Cek OTP</b>\n\n"
        f"Interval sekarang : <b>{current} detik</b>\n\n"
        f"Ketik angka detik baru (minimal <b>{POLL_INTERVAL_MIN}</b>, maksimal <b>{POLL_INTERVAL_MAX}</b>).\n\n"
        f"💡 <i>Contoh:</i>\n"
        f"  • <code>5</code>   → cek setiap 5 detik\n"
        f"  • <code>120</code> → cek setiap 2 menit\n\n"
        f"⚠️ Interval kecil dapat menyebabkan HTTP 429 dari API. Perubahan "
        f"langsung berlaku tanpa restart bot.",
        parse_mode="HTML", reply_markup=_back_kb()
    )
    return POLL_INTERVAL_INPUT

async def adm_poll_interval_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text(
            "❌ Masukkan angka saja. Contoh: <code>65</code>",
            parse_mode="HTML"
        )
        return POLL_INTERVAL_INPUT

    val = int(text)
    if val < POLL_INTERVAL_MIN or val > POLL_INTERVAL_MAX:
        await update.message.reply_text(
            f"❌ Interval harus antara <b>{POLL_INTERVAL_MIN}</b> – <b>{POLL_INTERVAL_MAX}</b> detik.",
            parse_mode="HTML"
        )
        return POLL_INTERVAL_INPUT

    settings = load_settings()
    old_val  = get_poll_interval()
    settings["poll_interval"] = val
    save_settings(settings)
    wake_all_workers()

    await update.message.reply_text(
        f"✅ <b>Interval polling diperbarui!</b>\n\n"
        f"  Lama   : <code>{old_val} detik</code>\n"
        f"  Baru   : <code>{val} detik</code>\n\n"
        f"Bot akan cek OTP setiap <b>{val} detik</b> mulai polling berikutnya.",
        parse_mode="HTML", reply_markup=admin_main_keyboard()
    )
    return ADMIN_MAIN

# ── 🌐 Webhook URL / IP di .env ──────────────────────────────────────────────────

def _webhook_panel_text() -> str:
    bind_host = WEBHOOK_HOST
    if ":" in bind_host and not bind_host.startswith("["):
        bind_host = f"[{bind_host}]"
    bind_endpoint = f"http://{bind_host}:{WEBHOOK_PORT}{WEBHOOK_PATH}"
    public_url = WEBHOOK_PUBLIC_URL or "belum diset"
    env_path = os.path.abspath(DOTENV_FILE)
    return (
        "🌐 <b>Setting Webhook</b>\n\n"
        f"Status        : <b>{'aktif' if WEBHOOK_ENABLED else 'nonaktif'}</b>\n"
        f"Bind IP/Host  : <code>{html.escape(WEBHOOK_HOST)}</code>\n"
        f"Port          : <code>{WEBHOOK_PORT}</code>\n"
        f"Path          : <code>{html.escape(WEBHOOK_PATH)}</code>\n"
        f"Public URL    : <code>{html.escape(public_url)}</code>\n\n"
        f"Endpoint lokal: <code>{html.escape(bind_endpoint)}</code>\n"
        f"File konfigurasi: <code>{html.escape(env_path)}</code>\n\n"
        "Perubahan disimpan ke <code>.env</code> dan berlaku setelah bot "
        "direstart."
    )


def _webhook_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            btn("✏️ Edit IP/Host", callback_data="adm_webhook_host", style="primary"),
            btn("✏️ Edit Port", callback_data="adm_webhook_port", style="primary"),
        ],
        [
            btn("✏️ Edit Path URL", callback_data="adm_webhook_path", style="primary"),
            btn("✏️ Edit Public URL", callback_data="adm_webhook_public", style="primary"),
        ],
        [btn("🔙 Kembali", callback_data="adm_back", style="secondary")],
    ])


async def adm_webhook(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        _webhook_panel_text(),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=_webhook_panel_kb(),
    )
    return ADMIN_MAIN


async def adm_webhook_host(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "✏️ <b>Edit IP / Host Webhook</b>\n\n"
        f"Nilai sekarang: <code>{html.escape(WEBHOOK_HOST)}</code>\n\n"
        "Ketik IP atau hostname untuk bind server.\n"
        "Contoh: <code>0.0.0.0</code>, <code>127.0.0.1</code>, "
        "atau <code>localhost</code>.",
        parse_mode="HTML",
        reply_markup=_back_kb(),
    )
    return WEBHOOK_HOST_INPUT


async def adm_webhook_port(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "✏️ <b>Edit Port Webhook</b>\n\n"
        f"Nilai sekarang: <code>{WEBHOOK_PORT}</code>\n\n"
        "Ketik port antara <b>1–65535</b>.\n"
        "Contoh: <code>8080</code>.",
        parse_mode="HTML",
        reply_markup=_back_kb(),
    )
    return WEBHOOK_PORT_INPUT


async def adm_webhook_path(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "✏️ <b>Edit Path URL Webhook</b>\n\n"
        f"Nilai sekarang: <code>{html.escape(WEBHOOK_PATH)}</code>\n\n"
        "Ketik path yang diawali <code>/</code>.\n"
        "Contoh: <code>/webhooks/augestel</code>.",
        parse_mode="HTML",
        reply_markup=_back_kb(),
    )
    return WEBHOOK_PATH_INPUT


async def adm_webhook_public(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    current = WEBHOOK_PUBLIC_URL or "belum diset"
    await q.edit_message_text(
        "✏️ <b>Edit Public URL Webhook</b>\n\n"
        f"Nilai sekarang: <code>{html.escape(current)}</code>\n\n"
        "Ketik URL publik lengkap dengan <code>https://</code> atau "
        "<code>http://IP:PORT</code>.\n"
        "Ketik <code>reset</code> untuk mengosongkan nilai.\n"
        "Contoh domain: <code>https://domain.com/webhooks/augestel</code>\n"
        "Contoh Pterodactyl: <code>http://145.239.65.118:20260/webhooks/augestel</code>.",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=_back_kb(),
    )
    return WEBHOOK_PUBLIC_URL_INPUT


async def _save_webhook_env_input(
    update: Update,
    key: str,
    value: str,
    label: str,
) -> int:
    try:
        _write_env_values({key: value})
    except (OSError, ValueError) as error:
        await update.message.reply_text(
            f"❌ Gagal menyimpan {label} ke .env:\n"
            f"<code>{html.escape(str(error))}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        return ADMIN_MAIN
    await update.message.reply_text(
        f"✅ <b>{label} berhasil disimpan ke .env</b>\n\n"
        f"Nilai baru: <code>{html.escape(value) or '(kosong)'}</code>\n\n"
        "Restart bot agar listener webhook memakai konfigurasi baru.",
        parse_mode="HTML",
        reply_markup=_webhook_panel_kb(),
    )
    return ADMIN_MAIN


async def adm_webhook_host_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    valid, result = _validate_webhook_host(value)
    if not valid:
        await update.message.reply_text(
            f"❌ {html.escape(result)} Coba lagi:",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        return WEBHOOK_HOST_INPUT
    return await _save_webhook_env_input(
        update, "WEBHOOK_HOST", result, "IP/Host webhook"
    )


async def adm_webhook_port_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    valid, result = _validate_webhook_port(update.message.text)
    if not valid:
        await update.message.reply_text(
            "❌ Port harus berupa angka antara 1–65535. Coba lagi:",
            reply_markup=_back_kb(),
        )
        return WEBHOOK_PORT_INPUT
    return await _save_webhook_env_input(
        update, "WEBHOOK_PORT", str(result), "Port webhook"
    )


async def adm_webhook_path_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    valid, result = _validate_webhook_path(update.message.text)
    if not valid:
        await update.message.reply_text(
            f"❌ {html.escape(result)} Coba lagi:",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        return WEBHOOK_PATH_INPUT
    return await _save_webhook_env_input(
        update, "WEBHOOK_PATH", result, "Path URL webhook"
    )


async def adm_webhook_public_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw_value = update.message.text.strip()
    value = "" if raw_value.lower() == "reset" else raw_value
    valid, result = _validate_webhook_public_url(value)
    if not valid:
        await update.message.reply_text(
            f"❌ {html.escape(result)} Coba lagi:",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        return WEBHOOK_PUBLIC_URL_INPUT
    return await _save_webhook_env_input(
        update, "WEBHOOK_PUBLIC_URL", result, "Public URL webhook"
    )

# ── 🎭 Mask Nomor ───────────────────────────────────────────────────────────────

def _mask_panel_text(mask: dict) -> str:
    eid = mask["emoji_id"]
    efb = mask["emoji_fb"] or "✨"
    sep_preview = f'<tg-emoji emoji-id="{eid}">{efb}</tg-emoji>' if eid else efb
    example_num = "628123456789"
    preview = build_masked_number(example_num, mask, bold=True)
    return (
        f"🎭 <b>Setting Mask Nomor</b>\n\n"
        f"Digit depan  : <b>{mask['front']}</b>\n"
        f"Digit belakang: <b>{mask['back']}</b>\n"
        f"Emoji separator: {sep_preview} "
        f"(<code>{eid or '—'}</code>)\n\n"
        f"📱 Preview: {preview}\n\n"
        f"Pilih tombol di bawah untuk mengubah:"
    )

def _mask_keyboard(mask: dict) -> InlineKeyboardMarkup:
    """Keyboard inline dengan pilihan cepat digit depan/belakang dan ganti emoji."""
    def front_row():
        return [
            btn(
                f"{'✅' if mask['front'] == n else ''}{n}depan",
                callback_data=f"adm_mask_front:{n}",
                style="success" if mask['front'] == n else "secondary"
            ) for n in [2, 3, 4, 5, 6]
        ]
    def back_row():
        return [
            btn(
                f"{'✅' if mask['back'] == n else ''}{n}belakang",
                callback_data=f"adm_mask_back:{n}",
                style="success" if mask['back'] == n else "secondary"
            ) for n in [2, 3, 4, 5, 6]
        ]
    return InlineKeyboardMarkup([
        front_row(),
        back_row(),
        [btn("✏️ Ganti Emoji Separator", callback_data="adm_mask_emoji", style="primary")],
        [btn("🔙 Kembali", callback_data="adm_back", style="secondary")],
    ])

async def adm_mask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    mask = get_mask_settings()
    await q.edit_message_text(
        _mask_panel_text(mask), parse_mode="HTML",
        reply_markup=_mask_keyboard(mask)
    )
    return ADMIN_MAIN

async def adm_mask_front(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    n = int(q.data.split(":")[1])
    settings = load_settings()
    settings["mask_front"] = n
    save_settings(settings)
    mask = get_mask_settings()
    await q.edit_message_text(
        _mask_panel_text(mask), parse_mode="HTML",
        reply_markup=_mask_keyboard(mask)
    )
    return ADMIN_MAIN

async def adm_mask_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    n = int(q.data.split(":")[1])
    settings = load_settings()
    settings["mask_back"] = n
    save_settings(settings)
    mask = get_mask_settings()
    await q.edit_message_text(
        _mask_panel_text(mask), parse_mode="HTML",
        reply_markup=_mask_keyboard(mask)
    )
    return ADMIN_MAIN

async def adm_mask_emoji(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    mask = get_mask_settings()
    eid  = mask["emoji_id"] or "—"
    efb  = mask["emoji_fb"] or "✨"
    await q.edit_message_text(
        f"✏️ <b>Ganti Emoji Separator Mask</b>\n\n"
        f"Sekarang: <code>{eid}</code> ({efb})\n\n"
        f"Ketik salah satu:\n"
        f"  • <b>Tempel emoji premium langsung</b> dari keyboard emoji Telegram\n"
        f"    → ID-nya otomatis terdeteksi, tidak perlu cari manual\n"
        f"  • <b>ID premium emoji</b> (angka panjang) kalau sudah punya ID-nya\n"
        f"    Contoh: <code>6217304154138742190</code>\n"
        f"  • <b>Tempel emoji biasa</b> (bukan premium)\n"
        f"    Contoh: <code>⭐</code> atau <code>🔥</code>\n"
        f"  • Ketik <code>reset</code> → kembali ke ✨ default\n\n"
        f"💡 Catatan: emoji premium HANYA terdeteksi kalau kamu tempel langsung "
        f"dari keyboard emoji (bukan hasil copy-paste teks biasa).",
        parse_mode="HTML", reply_markup=_back_kb()
    )
    return MASK_EMOJI_INPUT

def _strip_html_tags(text: str) -> str:
    """Fallback kasar: buang semua tag HTML dari teks, dipakai saat parse_mode
    HTML gagal (mis. tag <tg-emoji> dengan ID tidak valid) supaya pesan tetap
    bisa dikirim sebagai plain text alih-alih gagal total / tidak terkirim."""
    return re.sub(r"<[^>]+>", "", text)

async def _safe_reply_html(message, text: str, reply_markup=None):
    """Kirim balasan dengan parse_mode HTML; kalau Telegram MENOLAK (mis. ID
    emoji premium tidak valid/tidak cocok -> BadRequest), otomatis fallback
    kirim versi plain text + catatan error, supaya:
      1) User TETAP dapat balasan (tidak pernah 'diam'/hang),
      2) reply_markup (tombol) tetap terpasang & bisa dipencet."""
    try:
        return await message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
    except BadRequest as e:
        plain = _strip_html_tags(text)
        return await message.reply_text(
            f"⚠️ Sebagian tidak bisa dirender (kemungkinan ID emoji premium "
            f"tidak valid/tidak cocok): <code>{html.escape(str(e))}</code>\n\n"
            f"Versi teks biasa:\n{plain}",
            reply_markup=reply_markup
        )

async def adm_mask_emoji_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    settings = load_settings()

    try:
        if text.lower() == "reset":
            settings["mask_emoji_id"] = DEFAULT_MASK_EMOJI_ID
            settings["mask_emoji_fb"] = DEFAULT_MASK_EMOJI_FB
            save_settings(settings)
            mask = get_mask_settings()
            await _safe_reply_html(
                update.message,
                f"✅ Emoji direset ke default ✨\n\n{_mask_panel_text(mask)}",
                reply_markup=_mask_keyboard(mask)
            )
            return ADMIN_MAIN

        # ── Emoji premium ditempel LANGSUNG (bukan ketik ID) — deteksi via entities ──
        emoji_map = _custom_emoji_map(update.message)
        if emoji_map:
            fallback, eid = next(iter(emoji_map.items()))
            # Verifikasi ulang ke Telegram supaya karakter fallback dijamin cocok
            # dengan ID (kalau gagal cek, tetap pakai karakter dari paste sebagai
            # cadangan -- itu seharusnya sudah benar karena datang langsung dari
            # Telegram saat emoji ditempel).
            try:
                stickers = await ctx.bot.get_custom_emoji_stickers(custom_emoji_ids=[eid])
                if stickers and stickers[0].emoji:
                    fallback = stickers[0].emoji
            except AttributeError:
                pass  # library lama -- tetap lanjut pakai karakter dari paste (sudah cukup akurat)
            except Exception:
                pass
            settings["mask_emoji_id"] = eid
            settings["mask_emoji_fb"] = fallback
            save_settings(settings)
            mask = get_mask_settings()
            await _safe_reply_html(
                update.message,
                f"✅ Emoji premium disimpan (terdeteksi otomatis dari paste)!\n"
                f"ID    : <code>{eid}</code>\n"
                f"Tampil: {build_separator(mask)}\n\n"
                f"{_mask_panel_text(mask)}",
                reply_markup=_mask_keyboard(mask)
            )
            return ADMIN_MAIN

        # Cek apakah input adalah ID emoji premium (hanya angka, panjang > 10)
        if text.isdigit() and len(text) > 10:
            # PENTING: tag <tg-emoji> HARUS membungkus karakter emoji dasar yang
            # persis sama dengan yang terasosiasi ke ID ini di server Telegram --
            # kalau tidak cocok, Telegram DIAM-DIAM menolak render-nya dan cuma
            # nampilin karakter fallback biasa. Jadi ambil karakter yang benar
            # langsung dari Telegram lewat getCustomEmojiStickers.
            try:
                stickers = await ctx.bot.get_custom_emoji_stickers(custom_emoji_ids=[text])
            except AttributeError:
                await update.message.reply_text(
                    "❌ Library <code>python-telegram-bot</code> di server kamu terlalu lama "
                    "(belum punya fitur cek emoji premium).\n\n"
                    "Update dengan: <code>pip install -U python-telegram-bot</code> lalu "
                    "restart bot, atau tempel emoji-nya langsung saja (tidak butuh fitur ini).",
                    parse_mode="HTML", reply_markup=_back_kb()
                )
                return MASK_EMOJI_INPUT
            except Exception as e:
                await update.message.reply_text(
                    f"❌ ID <code>{text}</code> gagal dicek ke Telegram: "
                    f"<code>{html.escape(str(e))}</code>\n\n"
                    "Coba lagi, atau tempel emoji-nya langsung saja (lebih akurat).",
                    parse_mode="HTML", reply_markup=_back_kb()
                )
                return MASK_EMOJI_INPUT
            if not stickers:
                await update.message.reply_text(
                    f"❌ ID emoji <code>{text}</code> tidak ditemukan di Telegram "
                    "(bukan ID emoji premium yang valid).\n\n"
                    "Cek lagi ID-nya, atau tempel emoji-nya langsung saja "
                    "(lebih akurat & tidak perlu cari ID manual).",
                    parse_mode="HTML", reply_markup=_back_kb()
                )
                return MASK_EMOJI_INPUT
            settings["mask_emoji_id"] = text
            settings["mask_emoji_fb"] = stickers[0].emoji or DEFAULT_MASK_EMOJI_FB
            save_settings(settings)
            mask = get_mask_settings()
            await _safe_reply_html(
                update.message,
                f"✅ Emoji premium disimpan!\n"
                f"ID    : <code>{text}</code>\n"
                f"Tampil: {build_separator(mask)}\n\n"
                f"{_mask_panel_text(mask)}",
                reply_markup=_mask_keyboard(mask)
            )
        else:
            # Perlakukan sebagai plain emoji / teks — hapus ID premium
            settings["mask_emoji_id"] = ""
            settings["mask_emoji_fb"] = text
            save_settings(settings)
            mask = get_mask_settings()
            await _safe_reply_html(
                update.message,
                f"✅ Emoji disimpan: {html.escape(text)}\n\n{_mask_panel_text(mask)}",
                reply_markup=_mask_keyboard(mask)
            )
        return ADMIN_MAIN
    except Exception as e:
        # Jaring pengaman terakhir -- APA PUN yang gagal di atas, user tetap
        # HARUS dapat balasan dan tombol (khususnya "Kembali") tetap hidup.
        # Setting TIDAK disimpan kalau sampai jalur ini kepakai.
        print(f"{C_YELLOW}[MASK_EMOJI] Error tak terduga: {e}{C_RESET}")
        await update.message.reply_text(
            f"❌ Terjadi error tak terduga: <code>{html.escape(str(e))}</code>\n\n"
            "Setting belum berubah. Coba lagi atau tekan Kembali.",
            parse_mode="HTML", reply_markup=_back_kb()
        )
        return MASK_EMOJI_INPUT

# ─── Text handler saat menunggu input di ADMIN_MAIN ─────────────────────────────

async def adm_main_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle teks masuk saat state ADMIN_MAIN."""
    awaiting = ctx.user_data.get("awaiting")

    # ── Input Range ID untuk alokasi ──────────────────────────────────────────
    if awaiting == "alloc_range_id":
        ctx.user_data.pop("awaiting", None)
        return await adm_alloc_range(update, ctx)

    # ── Custom quantity untuk alokasi ─────────────────────────────────────────
    if awaiting == "alloc_custom_qty":
        ctx.user_data.pop("awaiting", None)
        text = update.message.text.strip()
        if not text.isdigit() or not (1 <= int(text) <= 1000):
            await update.message.reply_text(
                "❌ Jumlah harus angka antara 1–1000. Coba lagi:",
                reply_markup=_back_kb()
            )
            ctx.user_data["awaiting"] = "alloc_custom_qty"
            return ADMIN_MAIN
        qty = int(text)
        settings = load_settings()
        if "allocation" not in settings:
            settings["allocation"] = {}
        settings["allocation"]["quantity"] = qty
        save_settings(settings)
        cfg_range = settings["allocation"].get("rangeId")
        await update.message.reply_text(
            _alloc_panel_text(cfg_range, qty),
            parse_mode="HTML",
            reply_markup=_alloc_panel_kb(cfg_range, qty)
        )
        return ADMIN_MAIN

    # ── Hapus nomor per range ID ──────────────────────────────────────────────
    if awaiting == "del_range_id":
        ctx.user_data.pop("awaiting", None)
        return await _adm_del_range_execute(update, ctx)

    # ── Setting alokasi lama (format: rangeId qty) ────────────────────────────
    if awaiting == "alloc_cfg":
        ctx.user_data.pop("awaiting", None)
        parts = update.message.text.strip().split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            await update.message.reply_text(
                "❌ Format salah. Contoh: <code>123 50</code>",
                parse_mode="HTML", reply_markup=admin_main_keyboard()
            )
            return ADMIN_MAIN
        settings = load_settings()
        settings["allocation"] = {"rangeId": int(parts[0]), "quantity": int(parts[1])}
        save_settings(settings)
        await update.message.reply_text(
            f"✅ Setting disimpan!\nRange ID: <code>{parts[0]}</code> | Quantity: <code>{parts[1]}</code>",
            parse_mode="HTML", reply_markup=admin_main_keyboard()
        )
    return ADMIN_MAIN

# ─── Group management commands ───────────────────────────────────────────────────

async def addid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(ctx.args) < 2:
        await update.message.reply_text("Format: /addid <nama_grup> <id_grup>"); return
    nama, id_grup = ctx.args[0], ctx.args[1]
    settings = load_settings()
    if "groups" not in settings: settings["groups"] = {}
    if nama in settings["groups"]:
        await update.message.reply_text(f"Grup '{nama}' sudah ada."); return
    settings["groups"][nama] = {
        "id": id_grup, "btn_text": "number", "btn_url": "https://t.me/numberkingjars",
        "btn_style": "primary",
    }
    save_settings(settings)
    await update.message.reply_text(f"✅ Grup '{nama}' (ID: {id_grup}) ditambahkan!")

async def settombol2(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(ctx.args) < 3:
        await update.message.reply_text(
            "Format: /settombol2 <nama_grup> <judul> <link> [style]\n"
            "Style: primary, secondary, success, danger, warning (default: primary)"
        ); return
    nama = ctx.args[0]; title = ctx.args[1]
    # Style opsional adalah token terakhir jika cocok dengan salah satu warna yang didukung
    rest = ctx.args[2:]
    style = "primary"
    if len(rest) > 1 and rest[-1].lower() in BUTTON_STYLES:
        style = rest[-1].lower()
        rest = rest[:-1]
    link = " ".join(rest)
    if not link.startswith("http"): link = "https://" + link
    settings = load_settings()
    if nama not in settings.get("groups", {}):
        await update.message.reply_text(f"Grup '{nama}' tidak ditemukan!"); return
    settings["groups"][nama].update({"btn_text": title, "btn_url": link, "btn_style": style})
    save_settings(settings)
    await update.message.reply_text(f"✅ Tombol grup '{nama}' diupdate!\n{title} → {link}\nStyle: {style}")

async def delid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not ctx.args:
        await update.message.reply_text("Format: /delid <nama_grup>"); return
    nama = ctx.args[0]; settings = load_settings()
    if nama in settings.get("groups", {}):
        del settings["groups"][nama]; save_settings(settings)
        await update.message.reply_text(f"🗑️ Grup '{nama}' dihapus!")
    else:
        await update.message.reply_text(f"Grup '{nama}' tidak ditemukan.")

async def listid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    settings = load_settings(); groups = settings.get("groups", {})
    if not groups: await update.message.reply_text("Database grup kosong."); return
    msg = f"Total: {len(groups)} grup\n\n"
    for nama, data in groups.items():
        msg += (f"• <b>{nama}</b> | <code>{data['id']}</code>\n"
                f"  [{data['btn_text']}]({data['btn_url']}) — style: {data.get('btn_style', 'primary')}\n")
    await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)

# ─── Admin management commands ───────────────────────────────────────────────────

async def addadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not ctx.args or not ctx.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Format: /addadmin <user_id>"); return
    uid = int(ctx.args[0]); settings = load_settings()
    if "extra_admins" not in settings: settings["extra_admins"] = []
    if uid in settings["extra_admins"]:
        await update.message.reply_text("User sudah jadi admin."); return
    settings["extra_admins"].append(uid); save_settings(settings)
    await update.message.reply_text(f"✅ <code>{uid}</code> ditambahkan sebagai admin!", parse_mode="HTML")

async def deladmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not ctx.args or not ctx.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Format: /deladmin <user_id>"); return
    uid = int(ctx.args[0]); settings = load_settings()
    if uid in settings.get("extra_admins", []):
        settings["extra_admins"].remove(uid); save_settings(settings)
        await update.message.reply_text(f"🗑️ <code>{uid}</code> dihapus dari admin.", parse_mode="HTML")
    else:
        await update.message.reply_text("User tidak ada di daftar admin.")

# ═══════════════════════════════════════════════════════════════════════════════════
# POLLING WORKER
# ═══════════════════════════════════════════════════════════════════════════════════

async def fetch_and_process_sms(app, session, account):
    """
    Polling SMS dan teruskan OTP yang belum pernah dikirim ke Telegram.

    Logika restart-safe:
    - sent_ids.json ADA  → sudah pernah jalan, teruskan apapun yang belum ada di file
    - sent_ids.json TIDAK ADA → pertama kali jalan, tandai semua sebagai 'sudah lihat'
      tanpa forward (jadi hanya pesan BARU setelah startup yang akan diforward)
    """
    acc_name = account.get("name", "Augestel")
    base     = str(account.get("base_url", PANEL_BASE)).rstrip("/")
    api_key  = str(account.get("api_key", "")).strip()
    if not api_key:
        return False, "API key belum diisi"

    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    runtime = _account_status(acc_name)[1]
    started_at = time.perf_counter()
    runtime["last_poll_at"] = time.time()
    try:
        r = await session.get(
            f"{base}/messages",
            params={"page": 1, "per_page": 50, "type": "all"},
            headers=headers, timeout=15
        )
        runtime["last_latency_ms"] = round((time.perf_counter() - started_at) * 1000)
        runtime["last_http_status"] = r.status_code
        if r.status_code == 401:
            print(f"{C_RED}[AUTH] [{acc_name}] API Key tidak valid{C_RESET}")
            return False, "API Key tidak valid (401)"
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            if not retry_after:
                match = re.search(r"retry\s+after\s+(\d+)", r.text, re.IGNORECASE)
                retry_after = match.group(1) if match else None
            cooldown = _mark_rate_limit(acc_name, retry_after, api_key)
            print(f"{C_YELLOW}[RATE] [{acc_name}] Cooldown API {cooldown} detik{C_RESET}")
            return False, f"HTTP 429: rate limit, retry dalam {cooldown} detik"
        if r.status_code != 200:
            print(f"{C_RED}[ERR] [{acc_name}] Status {r.status_code}{C_RESET}")
            retry_after = r.headers.get("Retry-After")
            suffix = f", coba lagi dalam {retry_after} detik" if retry_after else ""
            return False, f"HTTP {r.status_code}{suffix}"

        rows = r.json().get("data", [])
        if not rows:
            return True, None

        forwarded = 0
        for row in rows:
            row.setdefault("id", "")
            status, event_id = await process_incoming_sms(app, row, acc_name)
            if status == "sent":
                forwarded += 1
                print(
                    f"{C_CYAN}📨 [{acc_name}] event {event_id} diteruskan realtime"
                    f"{C_RESET}"
                )
            await asyncio.sleep(0.5)

        if forwarded:
            print(f"{C_GREEN}[DONE] [{acc_name}] {forwarded} pesan diforward{C_RESET}")

        return True, None

    except Exception as e:
        runtime["last_latency_ms"] = round((time.perf_counter() - started_at) * 1000)
        print(f"{C_RED}[ERR] [{acc_name}] {e}{C_RESET}")
        return False, str(e)

def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE) as f:
                accounts = json.load(f)
            if isinstance(accounts, list) and accounts:
                normalized = [
                    {
                        "name": str(acc.get("name", "augestel")).strip() or "augestel",
                        "base_url": _normalize_account_base(acc.get("base_url", PANEL_BASE)),
                        "api_key": str(acc.get("api_key", "")).strip(),
                        "webhook_secret": str(acc.get("webhook_secret", "")).strip(),
                    }
                    for acc in accounts if isinstance(acc, dict)
                ]
                if normalized:
                    if normalized != accounts:
                        _save_accounts(normalized)
                    # Single-account convenience: use the environment Secret
                    # without persisting it to account.json. Explicit keys in
                    # account.json always remain the source of truth.
                    if len(normalized) == 1 and not normalized[0]["api_key"] and AUGESTEL_API_KEY:
                        normalized[0]["api_key"] = AUGESTEL_API_KEY
                    return normalized
        except Exception:
            pass
    # Legacy migrations never copy a global key into an account. The admin must
    # enter each account key explicitly, so accounts cannot accidentally share
    # credentials.
    if AUGESTEL_API_KEY:
        return [{
            "name": WEBHOOK_ACCOUNT_NAME or "augestel",
            "base_url": PANEL_BASE,
            "api_key": AUGESTEL_API_KEY,
        }]
    default = []
    _save_accounts(default)
    return default

async def worker(app, account):
    acc_name = account.get("name", "Account")
    hdrs = {'User-Agent': 'Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36'}
    stop_event = asyncio.Event()
    config_event = asyncio.Event()
    _worker_wake_events[acc_name] = stop_event
    _worker_config_events[acc_name] = config_event
    state = _worker_runtime.setdefault(acc_name, {})
    state["status"] = "running"
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, verify=False, headers=hdrs) as session:
            interval = get_poll_interval()
            print(f"{C_GREEN}[START] [{acc_name}] Polling setiap {interval} detik...{C_RESET}")
            while not stop_event.is_set():
                cooldown = _rate_limit_wait(acc_name, str(account.get("api_key", "")))
                if cooldown:
                    state["status"] = "rate_limited"
                    state["next_poll_at"] = time.time() + cooldown
                    state["last_error"] = f"Rate limit cooldown {cooldown} detik"
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=cooldown)
                    except asyncio.TimeoutError:
                        pass
                    if not stop_event.is_set():
                        state["status"] = "running"
                    continue

                await _wait_for_api_slot(str(account.get("api_key", "")))
                state["status"] = "running"
                state["next_poll_at"] = time.time()
                result = await fetch_and_process_sms(app, session, account)
                if isinstance(result, tuple):
                    ok, reason = result
                else:
                    ok, reason = result, None

                if ok:
                    await on_poll_success(app, acc_name)
                    state["last_error"] = ""
                    state["success_count"] = int(state.get("success_count", 0)) + 1
                else:
                    state["last_error"] = reason or "Unknown error"
                    state["error_count"] = int(state.get("error_count", 0)) + 1
                    await on_poll_failure(app, acc_name, state["last_error"])

                interval = get_poll_interval()
                sleep_time = interval if ok else min(max(interval * 2, 3), 300)
                state["next_poll_at"] = time.time() + sleep_time
                stop_wait = asyncio.create_task(stop_event.wait())
                config_wait = asyncio.create_task(config_event.wait())
                try:
                    done, pending = await asyncio.wait(
                        {stop_wait, config_wait},
                        timeout=sleep_time,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if config_wait in done and not stop_event.is_set():
                        config_event.clear()
                finally:
                    for pending_task in (stop_wait, config_wait):
                        if not pending_task.done():
                            pending_task.cancel()
                    await asyncio.gather(stop_wait, config_wait, return_exceptions=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        state["last_error"] = str(e)
        print(f"{C_RED}[ERR] [{acc_name}] worker berhenti: {e}{C_RESET}")
    finally:
        state["status"] = "stopped"
        _worker_wake_events.pop(acc_name, None)
        _worker_config_events.pop(acc_name, None)

# ═══════════════════════════════════════════════════════════════════════════════════
# REALTIME WEBHOOK SERVER
# ═══════════════════════════════════════════════════════════════════════════════════

async def _read_webhook_body(request: web.Request) -> bytes:
    """Read a bounded request body so a public endpoint cannot consume memory."""
    if request.content_length and request.content_length > WEBHOOK_MAX_BODY_BYTES:
        raise web.HTTPRequestEntityTooLarge(
            max_size=WEBHOOK_MAX_BODY_BYTES, actual_size=request.content_length
        )
    body = await request.read()
    if len(body) > WEBHOOK_MAX_BODY_BYTES:
        raise web.HTTPRequestEntityTooLarge(
            max_size=WEBHOOK_MAX_BODY_BYTES, actual_size=len(body)
        )
    return body


async def _webhook_worker(app) -> None:
    """Process acknowledged webhook events with bounded internal retries."""
    while True:
        row, account_name = await _webhook_queue.get()
        event_id = str(row.get("id", "")).strip()
        try:
            status, processed_id = await process_incoming_sms(
                app, row, account_name, event_claimed=True
            )
            for attempt in range(1, WEBHOOK_WORKER_RETRIES):
                if status != "failed":
                    break
                await asyncio.sleep(
                    WEBHOOK_WORKER_BACKOFF_SECONDS * (2 ** (attempt - 1))
                )
                status, processed_id = await process_incoming_sms(
                    app, row, account_name
                )
            if status == "sent":
                print(
                    f"{C_CYAN}📨 [{account_name}] webhook event "
                    f"{processed_id} diteruskan realtime{C_RESET}"
                )
            elif status == "failed":
                print(
                    f"{C_RED}[ERR] [{account_name}] webhook event "
                    f"{processed_id or event_id} gagal setelah "
                    f"{WEBHOOK_WORKER_RETRIES} percobaan{C_RESET}"
                )
        except asyncio.CancelledError:
            raise
        except Exception as worker_err:
            print(
                f"{C_RED}[ERR] Webhook worker gagal memproses "
                f"{event_id}: {worker_err}{C_RESET}"
            )
            await _release_incoming_event(_incoming_dedup_keys(row))
        finally:
            _webhook_queue.task_done()


async def stop_webhook_worker() -> None:
    global _webhook_worker_task
    if _webhook_worker_task is None:
        return
    _webhook_worker_task.cancel()
    await asyncio.gather(_webhook_worker_task, return_exceptions=True)
    _webhook_worker_task = None


async def webhook_health(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "ok": True,
            "service": "augestel-otp-telegram-bot",
            "webhook": WEBHOOK_ENABLED,
            "host": WEBHOOK_HOST,
            "port": WEBHOOK_PORT,
            "path": WEBHOOK_PATH,
            "public_url": WEBHOOK_PUBLIC_URL or None,
            "polling_fallback": POLLING_ENABLED,
            "timestamp": now_wib().isoformat(),
        }
    )

async def augestel_webhook(request: web.Request) -> web.Response:
    try:
        raw_body = await _read_webhook_body(request)
    except web.HTTPException:
        raise
    except Exception:
        return web.json_response({"ok": False, "error": "request_body_unreadable"}, status=400)

    header_event = request.headers.get("X-Augestel-Event", "").strip().lower()
    header_timestamp = request.headers.get("X-Augestel-Timestamp", "").strip()
    if not header_event or not header_timestamp:
        return web.json_response(
            {"ok": False, "error": "missing_webhook_headers"}, status=400
        )
    if not _webhook_timestamp_is_valid(header_timestamp):
        return web.json_response(
            {"ok": False, "error": "invalid_or_expired_webhook_timestamp"},
            status=400,
        )

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "json_object_required"}, status=400)

    matched_account = _webhook_signature_account(request, raw_body, payload)
    if not matched_account:
        return web.json_response(
            {"ok": False, "error": "invalid_webhook_signature_or_account"}, status=401
        )

    event_name = str(payload.get("event", "")).strip().lower()
    if not event_name or event_name != header_event:
        return web.json_response(
            {"ok": False, "error": "event_header_mismatch"}, status=400
        )
    if event_name != "message.received":
        # Returning 2xx prevents Augestel retrying events this bot intentionally
        # does not consume (number/earnings lifecycle events).
        return web.json_response(
            {"ok": True, "accepted": False, "event": event_name or None}, status=200
        )

    _, row = _webhook_row(payload)
    if row is None:
        # The signature is valid, but this payload is permanently invalid for
        # the OTP relay. A 2xx response prevents retries that cannot succeed.
        return web.json_response(
            {
                "ok": True,
                "accepted": False,
                "event": event_name,
                "error": "message.received_requires_source_number_message",
            },
            status=200,
        )

    event_id = _webhook_event_id(payload, raw_body)
    row["id"] = event_id
    account_name = matched_account
    dedup_keys = _incoming_dedup_keys(row)
    if not await _claim_incoming_event(dedup_keys):
        return web.json_response(
            {
                "ok": True,
                "accepted": True,
                "event": event_name,
                "event_id": event_id,
                "status": "duplicate",
            },
            status=200,
        )

    try:
        await asyncio.wait_for(
            _webhook_queue.put((row, account_name)),
            timeout=1,
        )
    except asyncio.TimeoutError:
        await _release_incoming_event(dedup_keys)
        return web.json_response(
            {
                "ok": False,
                "accepted": False,
                "event_id": event_id,
                "error": "webhook_queue_full",
            },
            status=503,
        )

    state = _account_status(account_name)[1]
    state["last_webhook_at"] = time.time()
    state["webhook_count"] = int(state.get("webhook_count", 0)) + 1

    return web.json_response(
        {
            "ok": True,
            "accepted": True,
            "event": event_name,
            "event_id": event_id,
            "status": "queued",
        },
        status=200,
    )

async def start_webhook_server(app) -> tuple[web.AppRunner, web.TCPSite] | None:
    if not WEBHOOK_ENABLED:
        print(f"{C_YELLOW}[WEBHOOK] Dinonaktifkan lewat WEBHOOK_ENABLED=false{C_RESET}")
        return None
    accounts_have_secret = any(
        str(account.get("webhook_secret", "")).strip()
        for account in load_accounts()
    )
    if not WEBHOOK_SECRET and not accounts_have_secret:
        print(
            f"{C_RED}[WEBHOOK] Dinonaktifkan: "
            f"isi webhook secret global atau secret pada minimal satu akun{C_RESET}"
        )
        return None
    if not WEBHOOK_PUBLIC_URL or not WEBHOOK_PUBLIC_URL.lower().startswith(("http://", "https://")):
        print(
            f"{C_RED}[WEBHOOK] Dinonaktifkan: "
            f"WEBHOOK_PUBLIC_URL harus berupa URL http:// atau https:// publik{C_RESET}"
        )
        return None
    server = web.Application(client_max_size=WEBHOOK_MAX_BODY_BYTES)
    server["telegram_app"] = app
    server.add_routes(
        [
            web.get(WEBHOOK_PATH, webhook_health),
            web.post(WEBHOOK_PATH, augestel_webhook),
            web.get(f"{WEBHOOK_PATH}/health", webhook_health),
        ]
    )
    runner = web.AppRunner(server, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT)
    await site.start()
    global _webhook_worker_task
    _webhook_worker_task = asyncio.create_task(_webhook_worker(app))
    print(
        f"{C_GREEN}[WEBHOOK] Aktif di {WEBHOOK_PUBLIC_URL}"
        f"{WEBHOOK_PATH}{C_RESET}"
    )
    return runner, site

# ═══════════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════════

async def main():
    print("🚀 Bot SMS OTP + Admin Dashboard berjalan...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    await app.initialize()

    # ── Admin dashboard conversation ──
    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin_cmd)],
        # Jangan menahan antrean update Telegram saat menu admin sedang
        # menunggu API Augestel, download file, atau stop/restart worker.
        # Handler tetap mengembalikan state conversation setelah selesai,
        # sementara klik berikutnya dapat diproses oleh application task lain.
        block=False,
        states={
            ADMIN_MAIN: [
                # ── menu utama ───────────────────────────────────────────────
                CallbackQueryHandler(adm_numbers,          pattern="^adm_numbers$"),
                CallbackQueryHandler(adm_dl_numbers,       pattern="^adm_dl_numbers$"),
                CallbackQueryHandler(adm_dl_refresh,        pattern="^adm_dl_refresh$"),
                CallbackQueryHandler(adm_dl_all,            pattern="^adm_dl_all$"),
                CallbackQueryHandler(adm_dl_filter,         pattern="^adm_dl_filter$"),
                CallbackQueryHandler(adm_dl_country_page,   pattern="^adm_dl_cpage:\\d+$"),
                CallbackQueryHandler(adm_dl_country,         pattern="^adm_dl_country:[A-Z]{2}$"),
                CallbackQueryHandler(adm_traffic,          pattern="^adm_traffic$"),
                CallbackQueryHandler(adm_me,                pattern="^adm_me$"),
                CallbackQueryHandler(adm_webhook,            pattern="^adm_webhook$"),
                CallbackQueryHandler(adm_webhook_host,       pattern="^adm_webhook_host$"),
                CallbackQueryHandler(adm_webhook_port,       pattern="^adm_webhook_port$"),
                CallbackQueryHandler(adm_webhook_path,       pattern="^adm_webhook_path$"),
                CallbackQueryHandler(adm_webhook_public,     pattern="^adm_webhook_public$"),
                # ── pagination ───────────────────────────────────────────────
                CallbackQueryHandler(adm_ratecard_page,    pattern="^adm_rc_page:\\d+$"),
                CallbackQueryHandler(adm_access_page,      pattern="^adm_ac_page:\\d+$"),
                CallbackQueryHandler(adm_numbers_page,     pattern="^adm_nm_page:\\d+$"),
                CallbackQueryHandler(adm_traffic_page,     pattern="^adm_tf_page:\\d+$"),
                # ── alokasi ──────────────────────────────────────────────────
                CallbackQueryHandler(adm_alloc_start,      pattern="^adm_alloc_start$"),
                CallbackQueryHandler(adm_alloc_confirm,    pattern="^adm_alloc_confirm$"),
                CallbackQueryHandler(adm_alloc_qty_set,    pattern="^adm_alloc_qty_set:\\d+$"),
                CallbackQueryHandler(adm_alloc_custom_qty, pattern="^adm_alloc_custom_qty$"),
                CallbackQueryHandler(adm_alloc_range_input,pattern="^adm_alloc_range_input$"),
                CallbackQueryHandler(adm_alloc_cfg,        pattern="^adm_alloc_cfg$"),
                # ── hapus nomor ───────────────────────────────────────────────
                CallbackQueryHandler(adm_del_numbers,      pattern="^adm_del_numbers$"),
                CallbackQueryHandler(adm_del_all,          pattern="^adm_del_all$"),
                CallbackQueryHandler(adm_del_all_confirm,  pattern="^adm_del_all_confirm$"),
                CallbackQueryHandler(adm_del_by_range,     pattern="^adm_del_by_range$"),
                CallbackQueryHandler(adm_del_by_file,      pattern="^adm_del_by_file$"),
                # ── menu lain ─────────────────────────────────────────────────
                CallbackQueryHandler(adm_groups,           pattern="^adm_groups$"),
                CallbackQueryHandler(adm_add_admin,        pattern="^adm_add_admin$"),
                CallbackQueryHandler(adm_stats,            pattern="^adm_stats$"),
                CallbackQueryHandler(adm_stats_send,       pattern="^adm_stats_send$"),
                CallbackQueryHandler(adm_dl_log,           pattern="^adm_dl_log$"),
                CallbackQueryHandler(adm_format,           pattern="^adm_format$"),
                CallbackQueryHandler(adm_accounts,         pattern="^adm_accounts$"),
                CallbackQueryHandler(adm_acc_add,          pattern="^adm_acc_add$"),
                CallbackQueryHandler(adm_acc_del,          pattern="^adm_acc_del:.*$"),
                CallbackQueryHandler(adm_acc_select,        pattern="^adm_acc_select:.*$"),
                CallbackQueryHandler(adm_acc_start,         pattern="^adm_acc_start:.*$"),
                CallbackQueryHandler(adm_acc_restart,       pattern="^adm_acc_restart:.*$"),
                CallbackQueryHandler(adm_acc_stop,          pattern="^adm_acc_stop:.*$"),
                CallbackQueryHandler(adm_acc_start_all,     pattern="^adm_acc_start_all$"),
                CallbackQueryHandler(adm_acc_restart_all,   pattern="^adm_acc_restart_all$"),
                CallbackQueryHandler(adm_acc_stop_all,      pattern="^adm_acc_stop_all$"),
                CallbackQueryHandler(adm_acc_edit_key,       pattern="^adm_acc_key:.*$"),
                CallbackQueryHandler(adm_acc_webhook,         pattern="^adm_acc_webhook:.*$"),
                CallbackQueryHandler(adm_acc_test,           pattern="^adm_acc_test:.*$"),
                CallbackQueryHandler(adm_acc_refresh,        pattern="^adm_acc_refresh$"),
                CallbackQueryHandler(adm_poll_interval,    pattern="^adm_poll_interval$"),
                CallbackQueryHandler(adm_mask,             pattern="^adm_mask$"),
                CallbackQueryHandler(adm_mask_front,       pattern="^adm_mask_front:\\d+$"),
                CallbackQueryHandler(adm_mask_back,        pattern="^adm_mask_back:\\d+$"),
                CallbackQueryHandler(adm_mask_emoji,       pattern="^adm_mask_emoji$"),
                CallbackQueryHandler(adm_back,             pattern="^adm_back$"),
                # ── teks & file ───────────────────────────────────────────────
                MessageHandler(filters.Document.MimeType("text/plain"), adm_del_file_receive),
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_main_text),
            ],
            RATECARD_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_ratecard_result),
                CallbackQueryHandler(adm_back, pattern="^adm_back$"),
            ],
            ACCESS_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_access_result),
                CallbackQueryHandler(adm_back, pattern="^adm_back$"),
            ],
            NUM_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_numbers_result),
                CallbackQueryHandler(adm_back, pattern="^adm_back$"),
            ],
            TRAFFIC_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_traffic_result),
                CallbackQueryHandler(adm_back, pattern="^adm_back$"),
            ],
            FORMAT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_format_input),
                CallbackQueryHandler(adm_format_preview, pattern="^adm_format_preview$"),
                CallbackQueryHandler(adm_format_test,    pattern="^adm_format_test$"),
                CallbackQueryHandler(adm_back, pattern="^adm_back$"),
            ],
            ACC_ADD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_acc_name),
                CallbackQueryHandler(adm_back, pattern="^adm_back$"),
            ],
            ACC_ADD_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_acc_key),
                CallbackQueryHandler(adm_back, pattern="^adm_back$"),
            ],
            ACC_ADD_WEBHOOK_SECRET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_acc_webhook_secret_input),
                CallbackQueryHandler(adm_back, pattern="^adm_back$"),
            ],
            ACC_EDIT_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_acc_edit_key_input),
                CallbackQueryHandler(adm_back, pattern="^adm_back$"),
            ],
            ACC_EDIT_WEBHOOK_SECRET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_acc_webhook_input),
                CallbackQueryHandler(adm_back, pattern="^adm_back$"),
            ],
            POLL_INTERVAL_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_poll_interval_input),
                CallbackQueryHandler(adm_back, pattern="^adm_back$"),
            ],
            MASK_EMOJI_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_mask_emoji_input),
                CallbackQueryHandler(adm_back, pattern="^adm_back$"),
            ],
            WEBHOOK_HOST_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_webhook_host_input),
                CallbackQueryHandler(adm_back, pattern="^adm_back$"),
            ],
            WEBHOOK_PORT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_webhook_port_input),
                CallbackQueryHandler(adm_back, pattern="^adm_back$"),
            ],
            WEBHOOK_PATH_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_webhook_path_input),
                CallbackQueryHandler(adm_back, pattern="^adm_back$"),
            ],
            WEBHOOK_PUBLIC_URL_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_webhook_public_input),
                CallbackQueryHandler(adm_back, pattern="^adm_back$"),
            ],
        },
        fallbacks=[CommandHandler("admin", admin_cmd)],
        per_message=False,
    )

    app.add_handler(admin_conv)
    app.add_handler(CommandHandler("addid",      addid))
    app.add_handler(CommandHandler("delid",      delid))
    app.add_handler(CommandHandler("listid",     listid))
    app.add_handler(CommandHandler("settombol2", settombol2))
    app.add_handler(CommandHandler("formatjson", formatjson_cmd))
    app.add_handler(CommandHandler("addadmin",   addadmin))
    app.add_handler(CommandHandler("deladmin",   deladmin))

    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    webhook_server = await start_webhook_server(app)
    try:
        if POLLING_ENABLED:
            # Fallback tetap tersedia saat webhook belum dikonfigurasi atau
            # sedang mengalami gangguan. Deduplication mencegah double-send.
            await start_all_workers(app)
            print(f"{C_GREEN}[MODE] Webhook realtime + polling fallback aktif{C_RESET}")
        else:
            print(f"{C_GREEN}[MODE] Webhook realtime aktif; polling fallback nonaktif{C_RESET}")
        await daily_stats_loop(app)
    finally:
        await stop_download_tasks()
        await stop_all_workers()
        if webhook_server:
            await stop_webhook_worker()
            runner, _ = webhook_server
            await runner.cleanup()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
