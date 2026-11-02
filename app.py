import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from urllib.parse import quote, unquote, urlparse

import requests
import telebot
from dotenv import load_dotenv
from telebot import types
from identity_db import IdentityStore, MAX_USER_ID
from hybrid_identity import observe_message, shared_user, message_time
from identity_reliability import ReliableIdentityStore
from identity_diagnostics import audit_database, CATEGORIES
import secrets
import hashlib
from telegram_identity import TelegramResolver, IdentityRateLimited
from community_store import CommunityStore
from community import CommunityFeatures
from ui import UI, UIBot
from message_content import CONTENT_TYPES
from runtime_lock import InstanceLock
from identity_maintenance import IdentityMaintenance
from report_view import complaint_card
import texts

load_dotenv()
logger = logging.getLogger("lamascambase")
BUILD_VERSION = "2026.11.01-r3.4-transactional-id"


def _get_env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and (value is None or str(value).strip() == ""):
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return value


API_TOKEN = _get_env("API_TOKEN", required=True)
ADMIN_ID = int(_get_env("ADMIN_ID", required=True))
if ADMIN_ID <= 0 or API_TOKEN == "CHANGE_ME":
    raise RuntimeError("Заполни API_TOKEN и ADMIN_ID в личном .env файле")
CHANNEL_USERNAMES = [
    item.strip() for item in _get_env("CHANNEL_USERNAMES", "").split(",") if item.strip()
]
DB_PATH = _get_env("DB_PATH", "data/reports.sqlite3")
PAGE_SIZE = max(1, min(20, int(_get_env("PAGE_SIZE", "5"))))
MANUAL_ADD_STATUS = _get_env("MANUAL_ADD_STATUS", "pending").strip().lower() or "pending"
VISIBLE_STATUSES = tuple(
    item.strip().lower()
    for item in _get_env("VISIBLE_STATUSES", "accepted").split(",")
    if item.strip()
)
REVIEW_STATUS = _get_env("REVIEW_STATUS", "review").strip().lower() or "review"
if REVIEW_STATUS != "review":
    raise ValueError("REVIEW_STATUS должен быть review — новые жалобы не должны публиковаться автоматически")
USERNAME_LOOKUP_ENABLED = _get_env("USERNAME_LOOKUP_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
USERNAME_LOOKUP_PLATFORMS = tuple(
    item.strip().lower()
    for item in _get_env(
        "USERNAME_LOOKUP_PLATFORMS",
        "github,gitlab,reddit,roblox",
    ).split(",")
    if item.strip()
)
USERNAME_LOOKUP_MAX_VARIANTS = max(1, min(6, int(_get_env("USERNAME_LOOKUP_MAX_VARIANTS", "4"))))
USERNAME_LOOKUP_MAX_MATCHES = max(1, min(12, int(_get_env("USERNAME_LOOKUP_MAX_MATCHES", "6"))))
USERNAME_LOOKUP_TIMEOUT = max(0.5, min(8.0, float(_get_env("USERNAME_LOOKUP_TIMEOUT", "2.5"))))
USERNAME_LOOKUP_CACHE_SECONDS = max(60, int(_get_env("USERNAME_LOOKUP_CACHE_SECONDS", "900")))
USERNAME_LOOKUP_SEARCH_FALLBACK = _get_env("USERNAME_LOOKUP_SEARCH_FALLBACK", "false").strip().lower() in {"1", "true", "yes", "on"}
TELEGRAM_API_ID = _get_env('TELEGRAM_API_ID', '')
TELEGRAM_API_HASH = _get_env('TELEGRAM_API_HASH', '')
TELEGRAM_SESSION_PATH = _get_env('TELEGRAM_SESSION_PATH', 'data/telegram_identity')
IDENTITY_BATCH_SIZE = max(1, min(10, int(_get_env('IDENTITY_BATCH_SIZE', '4'))))
IDENTITY_MAINTENANCE_SECONDS = max(300, int(_get_env('IDENTITY_MAINTENANCE_SECONDS', '1800')))
IDENTITY_BACKLOG_PAUSE_SECONDS = max(5, min(300, int(_get_env('IDENTITY_BACKLOG_PAUSE_SECONDS', '10'))))
PUBLIC_PAGE_SIZE = min(PAGE_SIZE, 5)
USERNAME_LOOKUP_USER_AGENT = _get_env(
    "USERNAME_LOOKUP_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
)

if MANUAL_ADD_STATUS != "pending":
    logger.warning("MANUAL_ADD_STATUS=%s больше не допускается: ручные записи требуют проверки", MANUAL_ADD_STATUS)
    MANUAL_ADD_STATUS = "pending"

if VISIBLE_STATUSES != ("accepted",):
    logger.warning("Отклонён устаревший VISIBLE_STATUSES=%s: наружу публикуются только принятые модератором записи", VISIBLE_STATUSES)
    VISIBLE_STATUSES = ("accepted",)

bot = telebot.TeleBot(API_TOKEN)

DB_LOCK = threading.RLock()
if DB_PATH != ":memory:":
    Path(DB_PATH).expanduser().parent.mkdir(parents=True, exist_ok=True)
conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA busy_timeout = 30000")
conn.execute("PRAGMA foreign_keys = ON")
if DB_PATH != ":memory:":
    conn.execute("PRAGMA journal_mode = WAL")

user_states: dict[int, dict] = {}
admin_chat_sessions: dict[int, int] = {}
username_lookup_cache: dict[str, dict] = {}
USER_STATE_TTL_SECONDS = 60 * 60
MAX_EVIDENCE_ITEMS = 10
MAX_NOTE_LENGTH = 1600
MAX_EVIDENCE_TEXT_LENGTH = 1200
MAX_MANUAL_TARGETS = 25
MAX_CACHE_ITEMS = 256
STATE_LOCK = threading.RLock()
SESSION_LOCK = threading.RLock()
LOOKUP_CACHE_LOCK = threading.RLock()
CHECK_COOLDOWN_SECONDS = 5
LAST_CHECKS: dict[int, float] = {}

STATE_WAITING_FOR_CHECK_TARGET = "waiting_for_check_target"
STATE_WAITING_FOR_REPORT_TARGET = "waiting_for_report_target"
STATE_WAITING_FOR_REPORT_NOTE = "waiting_for_report_note"
STATE_WAITING_FOR_REPORT_EVIDENCE = "waiting_for_report_evidence"
STATE_WAITING_FOR_ADMIN_ADD_TARGETS = "waiting_for_admin_add_targets"
STATE_WAITING_FOR_ADMIN_ADD_NOTE = "waiting_for_admin_add_note"
STATE_WAITING_FOR_ADMIN_FIND = "waiting_for_admin_find"
STATE_WAITING_FOR_APPEAL = "waiting_for_appeal"
STATE_IDENTITY_EVIDENCE = 'identity_review_evidence'

VISIBLE_STATUS_LABELS = {
    "accepted": "принята модерацией",
    "pending": "ожидает проверки",
    REVIEW_STATUS: "на проверке",
    "declined": "отклонена",
    "removed": "снята с публикации",
}


def db_execute(query: str, params=(), *, fetchone: bool = False, fetchall: bool = False, commit: bool = False):
    with DB_LOCK:
        cursor = conn.cursor()
        cursor.execute(query, params)
        result = None
        if fetchone:
            result = cursor.fetchone()
        elif fetchall:
            result = cursor.fetchall()
        if commit:
            conn.commit()
        lastrowid = cursor.lastrowid
        cursor.close()
        return result, lastrowid



def init_db() -> None:
    with DB_LOCK:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target TEXT NOT NULL,
                from_user TEXT,
                from_id INTEGER,
                note TEXT NOT NULL,
                evidence TEXT,
                timestamp TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'review'
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users_seen (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                last_chat_id INTEGER
            )
            """
        )
        cursor.execute("PRAGMA table_info(complaints)")
        cols = [c["name"] for c in cursor.fetchall()]
        if "status" not in cols:
            cursor.execute(f"ALTER TABLE complaints ADD COLUMN status TEXT DEFAULT '{REVIEW_STATUS}'")

        cursor.execute(
            """
            UPDATE complaints
            SET status = CASE
                WHEN from_id = ? THEN 'pending'
                ELSE ?
            END
            WHERE status IS NULL OR TRIM(status) = ''
            """,
            (ADMIN_ID, REVIEW_STATUS),
        )
        cursor.execute("UPDATE complaints SET status = ? WHERE LOWER(status) = 'new'", (REVIEW_STATUS,))
        conn.commit()
        cursor.close()


init_db()
identity_store = ReliableIdentityStore(conn, DB_LOCK, admin_id=ADMIN_ID)
identity_store.initialize()
identity_resolver = TelegramResolver(TELEGRAM_API_ID, TELEGRAM_API_HASH, API_TOKEN,
                                    session_path=TELEGRAM_SESSION_PATH)
IDENTITY_JOB_LOCK = threading.Lock()
IDENTITY_SCAN_WAKE = threading.Event()
IDENTITY_STOP = threading.Event()

community_store = CommunityStore(conn, DB_LOCK, ADMIN_ID)
community_store.initialize()
ui = UI(types, community_store, enabled=_get_env("CUSTOM_EMOJI_ENABLED", "true").lower() in {"1", "true", "yes", "on"})
bot = UIBot(bot, ui)


RISKY_KEYWORDS = [
    "admin", "support", "official", "verify", "guarantee", "garant", "moder", "staff",
    "crypto", "usdt", "btc", "eth", "casino", "bet", "x100", "drop", "market",
    "shop", "sale", "store", "sell", "buy", "card", "boost", "cheat", "hack"
]

ADMIN_LIKE_KEYWORDS = [
    "admin", "support", "help", "service", "official", "verify",
    "moder", "mod", "team", "staff", "guarantee", "garant"
]

GAMBLING_KEYWORDS = ["bet", "casino", "x2", "x10", "x100", "slots", "spin", "win"]
CRYPTO_KEYWORDS = ["btc", "eth", "usdt", "tron", "sol", "bnb", "crypto", "token", "nft"]
MARKET_KEYWORDS = ["shop", "sell", "buy", "market", "store", "sale"]


def _looks_like_year_suffix(core_lower: str) -> bool:
    match = re.search(r'(19[7-9]\d|20[0-3]\d)$', core_lower)
    return match is not None



def _has_long_repetition(core: str, threshold: int = 3) -> bool:
    current = 1
    for i in range(1, len(core)):
        if core[i] == core[i - 1]:
            current += 1
            if current >= threshold:
                return True
        else:
            current = 1
    return False



def _has_mixed_scripts(core: str) -> bool:
    has_cyr = any('а' <= ch <= 'я' or 'А' <= ch <= 'Я' for ch in core)
    has_lat = any('a' <= ch.lower() <= 'z' for ch in core)
    return has_cyr and has_lat



def _risk_level(score: int) -> str:
    if score < 25:
        return "низкий"
    if score < 50:
        return "умеренный"
    if score < 75:
        return "повышенный"
    return "высокий"



def build_darknamescan_profile(raw_name: str, complaints_count: int) -> str:
    name = raw_name.strip()
    core = name.lstrip("@")
    core_lower = core.lower()

    length = len(core)
    digits = sum(ch.isdigit() for ch in core)
    specials = sum(not ch.isalnum() for ch in core)

    hints = []
    dirt_score = 0

    if length <= 2:
        dirt_score += 8
        hints.append("ник очень короткий")
    elif length >= 18:
        dirt_score += 10
        hints.append("ник очень длинный")

    if 3 <= digits <= 5:
        dirt_score += 10
        hints.append("в нике много цифр")
    elif digits > 5:
        dirt_score += 18
        hints.append("в нике слишком много цифр")

    if specials >= 2:
        dirt_score += 10 + specials * 2
        hints.append("в нике много необычных символов")
    elif specials == 1 and core.count("_") + core.count(".") + core.count("-") == 0:
        dirt_score += 6
        hints.append("в нике есть редкий символ")

    if _has_long_repetition(core):
        dirt_score += 12
        hints.append("есть длинные повторения символов")

    if _looks_like_year_suffix(core_lower):
        dirt_score += 5
        hints.append("в конце похоже указан год")

    if _has_mixed_scripts(core):
        dirt_score += 25
        hints.append("смешаны русские и английские буквы")

    risky_hits = [kw for kw in RISKY_KEYWORDS if kw in core_lower]
    admin_hits = [kw for kw in ADMIN_LIKE_KEYWORDS if kw in core_lower]
    gambling_hits = [kw for kw in GAMBLING_KEYWORDS if kw in core_lower]
    crypto_hits = [kw for kw in CRYPTO_KEYWORDS if kw in core_lower]
    market_hits = [kw for kw in MARKET_KEYWORDS if kw in core_lower]

    if admin_hits:
        dirt_score += 28
        hints.append(f"ник похож на официальный или служебный аккаунт: {', '.join(admin_hits[:3])}")
    elif risky_hits:
        dirt_score += 18 + 4 * len(risky_hits)
        hints.append(
            f"в нике есть слова, которые часто используют в торговых или сомнительных аккаунтах: {', '.join(risky_hits[:3])}"
        )

    if gambling_hits:
        dirt_score += 18
        hints.append("есть слова про ставки или быстрый выигрыш")
    if crypto_hits:
        dirt_score += 12
        hints.append("есть упоминание крипты")
    if market_hits:
        dirt_score += 8
        hints.append("есть слова про продажу или магазин")

    dirt_score = max(0, min(100, dirt_score))
    risk_level = _risk_level(dirt_score)

    if dirt_score <= 20:
        verdict = "По оформлению ник выглядит обычным."
    elif dirt_score <= 45:
        verdict = "В оформлении ника есть несколько деталей, которые стоит перепроверить."
    else:
        verdict = "В оформлении ника есть заметные странности. Перед сделкой лучше всё перепроверить."

    lines = [
        "[[icon:info]] <b>Условные сигналы оформления ника</b>",
        f"• Условный уровень сигналов: <b>{risk_level}</b>",
        f"• Длина: <b>{length}</b>",
        f"• Цифры: <b>{digits}</b>",
        f"• Другие символы: <b>{specials}</b>",
        f"\n{verdict}",
        "Эти признаки не показывают надёжность человека и не являются доказательством мошенничества.",
    ]

    lines.append("\n<b>Что бот заметил:</b>")
    if hints:
        for hint in hints[:5]:
            lines.append(f"• {escape_html(hint)}")
    else:
        lines.append("• Ник выглядит без явных странных признаков")

    return "\n".join(lines)


USERNAME_PLATFORM_DEFS = {
    "telegram": {
        "label": "Telegram",
        "profile_url": "https://t.me/{username}",
        "domains": ("t.me",),
        "allow_similar": True,
        "resolver": "direct_html",
        "source_rank": 2,
        "strict_supported": False,
    },
    "github": {
        "label": "GitHub",
        "profile_url": "https://github.com/{username}",
        "domains": ("github.com",),
        "allow_similar": True,
        "resolver": "github_api",
        "source_rank": 3,
        "strict_supported": True,
    },
    "gitlab": {
        "label": "GitLab",
        "profile_url": "https://gitlab.com/{username}",
        "domains": ("gitlab.com",),
        "allow_similar": True,
        "resolver": "gitlab_api",
        "source_rank": 3,
        "strict_supported": True,
    },
    "reddit": {
        "label": "Reddit",
        "profile_url": "https://www.reddit.com/user/{username}/",
        "domains": ("reddit.com", "www.reddit.com"),
        "allow_similar": True,
        "resolver": "reddit_api",
        "source_rank": 3,
        "strict_supported": True,
    },
    "roblox": {
        "label": "Roblox",
        "profile_url": "https://www.roblox.com/users/{user_id}/profile",
        "domains": ("roblox.com", "www.roblox.com"),
        "allow_similar": True,
        "resolver": "roblox_api",
        "source_rank": 3,
        "strict_supported": True,
    },
    "pinterest": {
        "label": "Pinterest",
        "profile_url": "https://www.pinterest.com/{username}/",
        "domains": ("pinterest.com", "www.pinterest.com"),
        "allow_similar": True,
        "resolver": "direct_html",
        "source_rank": 2,
        "strict_supported": False,
    },
    "steam": {
        "label": "Steam",
        "profile_url": "https://steamcommunity.com/id/{username}",
        "domains": ("steamcommunity.com",),
        "allow_similar": False,
        "resolver": "direct_html",
        "source_rank": 2,
        "strict_supported": False,
    },
    "twitch": {
        "label": "Twitch",
        "profile_url": "https://www.twitch.tv/{username}",
        "domains": ("twitch.tv", "www.twitch.tv"),
        "allow_similar": True,
        "resolver": "direct_html",
        "source_rank": 2,
        "strict_supported": False,
    },
    "youtube": {
        "label": "YouTube",
        "profile_url": "https://www.youtube.com/@{username}",
        "domains": ("youtube.com", "www.youtube.com"),
        "allow_similar": True,
        "resolver": "youtube_oembed",
        "source_rank": 3,
        "strict_supported": False,
    },
    "instagram": {
        "label": "Instagram",
        "profile_url": "https://www.instagram.com/{username}/",
        "domains": ("instagram.com", "www.instagram.com"),
        "allow_similar": True,
        "resolver": "direct_html",
        "source_rank": 1,
        "strict_supported": False,
    },
    "tiktok": {
        "label": "TikTok",
        "profile_url": "https://www.tiktok.com/@{username}",
        "domains": ("tiktok.com", "www.tiktok.com"),
        "allow_similar": True,
        "resolver": "direct_html",
        "source_rank": 1,
        "strict_supported": False,
    },
    "kick": {
        "label": "Kick",
        "profile_url": "https://kick.com/{username}",
        "domains": ("kick.com",),
        "allow_similar": True,
        "resolver": "direct_html",
        "source_rank": 1,
        "strict_supported": False,
    },
    "vk": {
        "label": "VK",
        "profile_url": "https://vk.com/{username}",
        "domains": ("vk.com",),
        "allow_similar": True,
        "resolver": "direct_html",
        "source_rank": 1,
        "strict_supported": False,
    },
    "x": {
        "label": "X",
        "profile_url": "https://x.com/{username}",
        "domains": ("x.com", "twitter.com", "www.x.com", "www.twitter.com"),
        "allow_similar": True,
        "resolver": "direct_html",
        "source_rank": 1,
        "strict_supported": False,
    },
}

STRICT_PUBLIC_PLATFORM_KEYS = tuple(
    key for key, meta in USERNAME_PLATFORM_DEFS.items() if meta.get("strict_supported")
)


EXACT_SOCIAL_HEADERS = {
    "User-Agent": USERNAME_LOOKUP_USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def escape_html_attr(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )



def _slugify_username(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]", "", value.lower())



def build_username_variants(raw_target: str) -> list[str]:
    base = _slugify_username(raw_target.lstrip("@"))
    if not base:
        return []

    variants: list[str] = []

    def add(candidate: str):
        candidate = _slugify_username(candidate)
        if not candidate or candidate in variants:
            return
        variants.append(candidate)

    add(base)
    add(re.sub(r"[._-]+", "", base))
    add(base.replace("_", "."))
    add(base.replace(".", "_"))
    add(base.replace("-", "_"))
    add(base.replace("_", "-"))
    add(re.sub(r"(.)\1{1,}", r"\1", base))

    swaps = {
        "0": ["o"],
        "o": ["0"],
        "1": ["l", "i"],
        "l": ["1", "i"],
        "i": ["1", "l"],
        "3": ["e"],
        "e": ["3"],
        "5": ["s"],
        "s": ["5"],
        "4": ["a"],
        "a": ["4"],
    }
    for idx, ch in enumerate(base):
        for replacement in swaps.get(ch, []):
            add(base[:idx] + replacement + base[idx + 1 :])
            if len(variants) >= USERNAME_LOOKUP_MAX_VARIANTS:
                return variants[:USERNAME_LOOKUP_MAX_VARIANTS]

    return variants[:USERNAME_LOOKUP_MAX_VARIANTS]



def _username_request(url: str, *, headers: dict | None = None, method: str = "GET", json_body=None):
    request_headers = dict(EXACT_SOCIAL_HEADERS)
    if headers:
        request_headers.update(headers)
    if method == "POST":
        return requests.post(url, headers=request_headers, json=json_body, timeout=USERNAME_LOOKUP_TIMEOUT, allow_redirects=False)
    return requests.get(url, headers=request_headers, timeout=USERNAME_LOOKUP_TIMEOUT, allow_redirects=False)



def _build_platform_profile_url(platform_key: str, username: str, *, user_id: int | None = None) -> str:
    platform = USERNAME_PLATFORM_DEFS[platform_key]
    if platform_key == "roblox":
        return platform["profile_url"].format(user_id=user_id or 0)
    encoded = quote(username, safe="._-@")
    return platform["profile_url"].format(username=encoded)



def _platform_match_from_response(platform_key: str, username: str, response) -> bool:
    final_url = str(response.url or "").lower()
    text = (response.text or "")[:50000].lower()
    username = username.lower()
    status = int(getattr(response, "status_code", 0) or 0)

    if status >= 400:
        return False

    def path_starts_with(fragment: str) -> bool:
        parsed = urlparse(final_url)
        path = parsed.path.lower().rstrip("/")
        fragment = fragment.lower().rstrip("/")
        return path == fragment or path.startswith(fragment + "/")

    if platform_key == "telegram":
        return (
            status == 200
            and path_starts_with(f"/{username}")
            and "if you have telegram" in text
            and "preview channel" not in text
            and "post not found" not in text
        )

    if platform_key == "github":
        return status == 200 and path_starts_with(f"/{username}") and "not found" not in text

    if platform_key == "gitlab":
        return status == 200 and path_starts_with(f"/{username}") and "page not found" not in text

    if platform_key == "reddit":
        return status == 200 and path_starts_with(f"/user/{username}") and "nobody on reddit goes by that name" not in text

    if platform_key == "twitch":
        return (
            status == 200
            and path_starts_with(f"/{username}")
            and "unless you've got a time machine" not in text
            and "this channel is currently unavailable" not in text
        )

    if platform_key == "steam":
        return status == 200 and path_starts_with(f"/id/{username}") and "the specified profile could not be found" not in text

    if platform_key == "instagram":
        return (
            status == 200
            and path_starts_with(f"/{username}")
            and "page isn't available" not in text
            and "sorry, this page isn't available" not in text
            and (f'"username":"{username}"' in text or f'"alternate_name":"@{username}"' in text or path_starts_with(f"/{username}"))
        )

    if platform_key == "tiktok":
        return (
            status == 200
            and path_starts_with(f"/@{username}")
            and "couldn't find this account" not in text
            and "user doesn't exist" not in text
            and "page not available" not in text
        )

    if platform_key == "roblox":
        return status == 200 and (f'"name":"{username}"' in text or f'"displayname":"{username}"' in text or username in final_url)

    if platform_key == "kick":
        return status == 200 and path_starts_with(f"/{username}") and "channel could not be found" not in text

    if platform_key == "vk":
        return status == 200 and path_starts_with(f"/{username}") and "profile deleted" not in text and "page not found" not in text

    if platform_key == "pinterest":
        return status == 200 and path_starts_with(f"/{username}") and "sorry, we couldn't find that page" not in text

    if platform_key == "youtube":
        return status == 200 and path_starts_with(f"/@{username}") and "this channel does not exist" not in text

    if platform_key == "x":
        return (
            status == 200
            and path_starts_with(f"/{username}")
            and "this account doesn't exist" not in text
            and "this account doesn’t exist" not in text
            and "account suspended" not in text
        )

    return False



def _clean_found_url(url: str) -> str:
    if not url:
        return url
    parsed = urlparse(url)
    return parsed._replace(fragment="").geturl()



def _source_score(source: str) -> int:
    return {
        "api": 3,
        "oembed": 3,
        "direct": 2,
        "fallback": 1,
    }.get((source or "").lower(), 0)



def _build_match(platform_key: str, username: str, url: str, *, source: str = "direct") -> dict:
    parsed = urlparse(url)
    approved_domains = USERNAME_PLATFORM_DEFS[platform_key]["domains"]
    if parsed.scheme != 'https' or (parsed.hostname or '').lower() not in approved_domains:
        raise ValueError("Untrusted profile URL")
    return {
        "platform": platform_key,
        "label": USERNAME_PLATFORM_DEFS[platform_key]["label"],
        "username": _slugify_username(username),
        "url": _clean_found_url(url),
        "source": source,
        "source_score": _source_score(source),
    }



def _check_direct_html_profile(platform_key: str, username: str) -> dict | None:
    url = _build_platform_profile_url(platform_key, username)
    try:
        response = _username_request(url)
    except requests.RequestException:
        return None
    except Exception:
        return None

    if not _platform_match_from_response(platform_key, username, response):
        return None

    final_url = _clean_found_url(str(response.url or url))
    found_username = _extract_username_from_url(platform_key, final_url) or username
    return _build_match(platform_key, found_username, final_url, source="direct")



def _check_github_profile(username: str) -> dict | None:
    api_url = f"https://api.github.com/users/{quote(username, safe='._-')}"
    try:
        response = _username_request(api_url, headers={"Accept": "application/vnd.github+json"})
    except requests.RequestException:
        return None
    except Exception:
        return None

    if response.status_code != 200:
        return None

    try:
        data = response.json()
    except Exception:
        data = {}

    login = _slugify_username(str(data.get("login") or ""))
    html_url = str(data.get("html_url") or "").strip()
    if login == username and html_url:
        return _build_match("github", login, html_url, source="api")

    return None



def _check_gitlab_profile(username: str) -> dict | None:
    api_url = f"https://gitlab.com/api/v4/users?username={quote(username, safe='._-')}"
    try:
        response = _username_request(api_url, headers={"Accept": "application/json"})
    except requests.RequestException:
        return None
    except Exception:
        return None

    if response.status_code != 200:
        return None

    try:
        items = response.json() or []
    except Exception:
        items = []

    for item in items:
        handle = _slugify_username(str(item.get("username") or ""))
        web_url = str(item.get("web_url") or "").strip()
        if handle == username and web_url:
            return _build_match("gitlab", handle, web_url, source="api")

    return None



def _check_reddit_profile(username: str) -> dict | None:
    api_url = f"https://www.reddit.com/user/{quote(username, safe='._-')}/about.json"
    try:
        response = _username_request(api_url, headers={"Accept": "application/json"})
    except requests.RequestException:
        return None
    except Exception:
        return None

    if response.status_code != 200:
        return None

    try:
        payload = response.json() or {}
        data = payload.get("data") or {}
    except Exception:
        data = {}

    handle = _slugify_username(str(data.get("name") or ""))
    if handle == username:
        return _build_match("reddit", handle, _build_platform_profile_url("reddit", handle), source="api")

    return None



def _check_roblox_profile(username: str) -> dict | None:
    api_url = "https://users.roblox.com/v1/usernames/users"
    payload = {"usernames": [username], "excludeBannedUsers": False}
    try:
        response = _username_request(
            api_url,
            method="POST",
            json_body=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
    except requests.RequestException:
        return None
    except Exception:
        return None

    if response.status_code != 200:
        return None

    try:
        data = response.json() or {}
        users = data.get("data") or []
    except Exception:
        users = []

    for item in users:
        requested = _slugify_username(str(item.get("requestedUsername") or item.get("name") or ""))
        found_name = _slugify_username(str(item.get("name") or ""))
        user_id = item.get("id")
        if requested != username and found_name != username:
            continue
        if not user_id:
            continue
        profile_url = _build_platform_profile_url("roblox", found_name or username, user_id=int(user_id))
        return _build_match("roblox", found_name or username, profile_url, source="api")

    return None



def _check_youtube_profile(username: str) -> dict | None:
    profile_url = _build_platform_profile_url("youtube", username)
    oembed_url = (
        "https://www.youtube.com/oembed?format=json&url="
        + quote(profile_url, safe="")
    )
    try:
        response = _username_request(oembed_url, headers={"Accept": "application/json"})
    except requests.RequestException:
        return None
    except Exception:
        return None

    if response.status_code != 200:
        return None

    try:
        data = response.json() or {}
    except Exception:
        data = {}

    author_url = str(data.get("author_url") or "").strip()
    title = str(data.get("title") or "").strip()
    if author_url:
        found_username = _extract_username_from_url("youtube", author_url) or username
        if found_username == username and title:
            return _build_match("youtube", found_username, author_url, source="oembed")

    return None



def _extract_username_from_url(platform_key: str, url: str) -> str | None:
    path = unquote(urlparse(url).path or "").strip("/")
    if not path:
        return None

    if platform_key == "telegram":
        return _slugify_username(path.split("/", 1)[0].lstrip("@")) or None
    if platform_key in {"github", "gitlab", "twitch", "kick", "vk", "x", "instagram", "pinterest"}:
        return _slugify_username(path.split("/", 1)[0].lstrip("@")) or None
    if platform_key == "reddit":
        parts = path.split("/")
        if len(parts) >= 2 and parts[0].lower() == "user":
            return _slugify_username(parts[1]) or None
    if platform_key == "tiktok":
        return _slugify_username(path.split("/", 1)[0].lstrip("@")) or None
    if platform_key == "steam":
        parts = path.split("/")
        if len(parts) >= 2 and parts[0].lower() == "id":
            return _slugify_username(parts[1]) or None
    if platform_key == "youtube":
        first = path.split("/", 1)[0]
        return _slugify_username(first.lstrip("@")) or None

    return None



def search_public_username(platform_key: str, username: str):
    return None



def _best_match_score(match: dict, original_username: str) -> tuple:
    found_username = _slugify_username(str(match.get("username") or ""))
    return (
        1 if found_username == original_username else 0,
        int(match.get("source_score") or 0),
        1 if found_username == _slugify_username(str(match.get("candidate") or "")) else 0,
        -int(match.get("candidate_index") or 0),
        -len(found_username or "~"),
    )



def _pick_best_matches(results: list[dict], original_username: str) -> list[dict]:
    per_platform: dict[str, dict] = {}
    for item in results:
        platform_key = item.get("platform")
        if not platform_key:
            continue
        current = per_platform.get(platform_key)
        if current is None or _best_match_score(item, original_username) > _best_match_score(current, original_username):
            per_platform[platform_key] = item

    chosen = list(per_platform.values())
    for item in chosen:
        item["exact"] = _slugify_username(str(item.get("username") or "")) == original_username

    chosen.sort(key=lambda item: (0 if item.get("exact") else 1, item.get("label", "").lower(), item.get("username", "")))
    return chosen


RESOLVER_MAP = {
    "direct_html": _check_direct_html_profile,
    "github_api": lambda platform_key, username: _check_github_profile(username),
    "gitlab_api": lambda platform_key, username: _check_gitlab_profile(username),
    "reddit_api": lambda platform_key, username: _check_reddit_profile(username),
    "roblox_api": lambda platform_key, username: _check_roblox_profile(username),
    "youtube_oembed": lambda platform_key, username: _check_youtube_profile(username),
}



def check_public_username(platform_key: str, username: str):
    platform = USERNAME_PLATFORM_DEFS.get(platform_key)
    if not platform:
        return None

    resolver_name = platform.get("resolver", "direct_html")
    resolver = RESOLVER_MAP.get(resolver_name)
    if resolver is None:
        return None

    result = resolver(platform_key, username)
    if result:
        return result

    return search_public_username(platform_key, username)


def _cache_lookup_result(username: str, now: float, data: dict) -> None:
    with LOOKUP_CACHE_LOCK:
        username_lookup_cache[username] = {"ts": now, "data": data}
        while len(username_lookup_cache) > MAX_CACHE_ITEMS:
            username_lookup_cache.pop(next(iter(username_lookup_cache)))


def scan_public_profiles(raw_target: str) -> dict:
    normalized_target = normalize_target(raw_target)
    username = _slugify_username(normalized_target.lstrip("@"))
    if not USERNAME_LOOKUP_ENABLED or not username:
        return {"matches": [], "checked": False, "checked_labels": [], "unsupported_labels": []}

    now = time.time()
    with LOOKUP_CACHE_LOCK:
        cached = username_lookup_cache.get(username)
        if cached and now - cached.get("ts", 0) < USERNAME_LOOKUP_CACHE_SECONDS:
            return cached["data"]

    variants = build_username_variants(normalized_target)
    seen_pairs = set()
    collected = []

    requested_platforms = [p for p in USERNAME_LOOKUP_PLATFORMS if p in USERNAME_PLATFORM_DEFS]
    platforms = [p for p in requested_platforms if USERNAME_PLATFORM_DEFS[p].get("strict_supported")]
    unsupported = [USERNAME_PLATFORM_DEFS[p]["label"] for p in requested_platforms if not USERNAME_PLATFORM_DEFS[p].get("strict_supported")]
    if not platforms:
        data = {"matches": [], "checked": False, "checked_labels": [], "unsupported_labels": unsupported}
        _cache_lookup_result(username, now, data)
        return data

    future_to_meta = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(platforms) * 2))) as executor:
        for platform_key in platforms:
            platform = USERNAME_PLATFORM_DEFS[platform_key]
            for idx, candidate in enumerate(variants):
                if idx > 0 and not platform.get("allow_similar", False):
                    continue
                pair = (platform_key, candidate)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                future = executor.submit(check_public_username, platform_key, candidate)
                future_to_meta[future] = {
                    "platform": platform_key,
                    "candidate": candidate,
                    "candidate_index": idx,
                }

        for future in as_completed(future_to_meta):
            meta = future_to_meta[future]
            try:
                result = future.result()
            except Exception:
                continue
            if not result:
                continue
            result["candidate"] = meta["candidate"]
            result["candidate_index"] = meta["candidate_index"]
            collected.append(result)

    matches = _pick_best_matches(collected, username)[:USERNAME_LOOKUP_MAX_MATCHES]
    data = {
        "matches": matches,
        "checked": True,
        "checked_labels": [USERNAME_PLATFORM_DEFS[p]["label"] for p in platforms if p in USERNAME_PLATFORM_DEFS],
        "unsupported_labels": unsupported,
    }
    _cache_lookup_result(username, now, data)
    return data


def format_public_profiles_text(raw_target: str) -> str:
    result = scan_public_profiles(raw_target)
    if not result.get("checked") and not result.get("unsupported_labels"):
        return ""

    lines = ["[[icon:globe]] <b>Похожие аккаунты на других площадках</b>"]
    checked_labels = result.get("checked_labels", [])
    unsupported_labels = result.get("unsupported_labels", [])
    if checked_labels:
        lines.append("Я проверил только те площадки, где можно честно подтвердить аккаунт: " + ", ".join(escape_html(label) for label in checked_labels))
    if unsupported_labels:
        lines.append("Площадки без надёжной открытой проверки я сейчас не показываю, чтобы не выдавать мусорные ссылки: " + ", ".join(escape_html(label) for label in unsupported_labels))

    matches = result.get("matches", [])
    if matches:
        lines.append("Ниже только те страницы, которые удалось подтвердить самой площадкой.")
        for match in matches:
            label = "Точно такой же ник" if match.get("exact") else "Похожий ник"
            url = str(match.get("url") or "").strip()
            username = str(match.get("username") or "").strip()
            lines.append(f"• <b>{escape_html(match['label'])}</b> · {label}")
            if username:
                lines.append(f"  Ник на сайте: <code>{escape_html(username)}</code>")
            if url:
                safe_url = escape_html(url)
                safe_attr = escape_html_attr(url)
                lines.append(f'  Ссылка: <a href="{safe_attr}">{safe_url}</a>')
        lines.append("Совпадение ника НЕ доказывает, что это тот же человек. Ссылки не являются обвинением.")
    else:
        lines.append("Сейчас я не нашёл ни одной подтверждённой страницы на тех площадках, которые удалось проверить честно.")
        lines.append("Я специально не показываю сомнительные совпадения, чтобы не путать тебя лишними ссылками.")
        lines.append("Если человека нет в этом блоке, это ещё не означает, что его больше нигде нет.")

    return "\n".join(lines)


def escape_html(value: str) -> str:
    return (
        str(value).replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )



TARGET_RE = re.compile(r"^@[A-Za-z0-9_]{3,32}$")


def normalize_target(raw_target: str) -> str:
    target = str(raw_target or "").strip()
    if not target.startswith("@"):
        target = f"@{target}"
    return target.lower()


def valid_target(raw_target: str) -> bool:
    return bool(TARGET_RE.fullmatch(normalize_target(raw_target)))


def private_chat(chat) -> bool:
    return getattr(chat, "type", "private") == "private"


def require_private_message(message) -> bool:
    if private_chat(message.chat):
        return True
    bot.send_message(message.chat.id, "[[icon:lock]] Жалобы и проверка доступны только в личном чате с ботом.")
    return False



def status_label(status: str) -> str:
    return VISIBLE_STATUS_LABELS.get((status or "").lower(), (status or "unknown").lower())



def format_timestamp_for_display(raw_timestamp: str | None) -> str:
    if not raw_timestamp:
        return "-"
    value = str(raw_timestamp).strip()
    if not value:
        return "-"

    candidates = (
        value,
        value.replace("Z", "+00:00"),
    )
    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    return value




def parse_evidence_blob(blob: str | None) -> list[dict]:
    if not blob:
        return []
    try:
        data = json.loads(blob)
        return data if isinstance(data, list) else []
    except Exception:
        return []



def get_complaint_by_id(complaint_id: int):
    row, _ = db_execute("SELECT * FROM complaints WHERE id = ?", (complaint_id,), fetchone=True)
    if not row:
        return None
    complaint = dict(row)
    complaint["evidence"] = parse_evidence_blob(complaint.get("evidence"))
    return complaint



def remember_user_seen(user, chat_id: int | None = None, **context) -> None:
    try:
        _remember_user_seen(user, chat_id, **context)
    except sqlite3.Error as exc:
        log_identity_observation_error(exc)


def _remember_user_seen(user, chat_id: int | None = None, *, observed_at=None, event_key=None, source='bot_message') -> None:
    user_id = getattr(user, "id", None)
    if user_id is None:
        return
    raw_username = getattr(user, "username", None)
    username = f"@{raw_username}" if raw_username else None
    previous, _ = db_execute('SELECT observed_at FROM identity_observations WHERE event_key=?',(event_key,),fetchone=True)
    now = previous['observed_at'] if previous else (observed_at or datetime.now(timezone.utc).isoformat())
    db_execute(
        """INSERT INTO users_seen (user_id, username, first_seen, last_seen, last_chat_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = CASE WHEN excluded.last_seen>=users_seen.last_seen THEN excluded.username ELSE users_seen.username END,
            first_seen = MIN(users_seen.first_seen,excluded.first_seen),
            last_chat_id = CASE WHEN excluded.last_seen>=users_seen.last_seen THEN excluded.last_chat_id ELSE users_seen.last_chat_id END,
            last_seen = MAX(users_seen.last_seen,excluded.last_seen)""",
        (user_id, username, now, now, chat_id),
        commit=True,
    )
    if chat_id == user_id:
        community_store.touch_user(int(user_id))
    try:
        identity_store.observe(int(user_id), username, source=source, checked_at=now, actor_id=int(user_id),
                               event_key=event_key or f'seen:{user_id}:{username}:{now[:16]}')
    except ValueError:
        logger.warning('Ignoring invalid Telegram sender ID')



def get_total_seen_users() -> int:
    row, _ = db_execute(
        """
        SELECT COUNT(*) AS total
        FROM (
            SELECT user_id FROM users_seen
            UNION
            SELECT from_id AS user_id FROM complaints WHERE from_id IS NOT NULL AND from_id > 0
        ) AS all_users
        """,
        fetchone=True,
    )
    return int(row["total"] or 0) if row else 0



def get_reporters_count() -> int:
    row, _ = db_execute(
        "SELECT COUNT(DISTINCT from_id) AS total FROM complaints WHERE from_id IS NOT NULL AND from_id > 0",
        fetchone=True,
    )
    return int(row["total"] or 0) if row else 0



def get_visible_counts(target_db_key: str) -> dict:
    placeholders = ",".join("?" for _ in VISIBLE_STATUSES)
    rows, _ = db_execute(
        f"""
        SELECT status, COUNT(*) AS total
        FROM complaints
        WHERE target = ? AND status IN ({placeholders})
        GROUP BY status
        """,
        (target_db_key, *VISIBLE_STATUSES),
        fetchall=True,
    )
    counts = {status: 0 for status in VISIBLE_STATUSES}
    for row in rows:
        counts[row["status"]] = row["total"]
    return counts


def get_recent_visible_complaints(target_db_key: str, limit: int = 3):
    placeholders = ",".join("?" for _ in VISIBLE_STATUSES)
    rows, _ = db_execute(
        f"""
        SELECT id, note, timestamp, status
        FROM complaints
        WHERE target = ? AND status IN ({placeholders})
        ORDER BY id DESC
        LIMIT ?
        """,
        (target_db_key, *VISIBLE_STATUSES, limit),
        fetchall=True,
    )
    return rows or []



def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID



def check_subscription(user_id: int) -> bool:
    if is_admin(user_id) or not CHANNEL_USERNAMES:
        return True
    try:
        for channel in CHANNEL_USERNAMES:
            member = bot.get_chat_member(channel, user_id)
            if member.status not in ["member", "administrator", "creator"]:
                return False
        return True
    except Exception:
        return False



def subscription_keyboard() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=1)
    for channel in CHANNEL_USERNAMES:
        clean = channel.lstrip("@")
        markup.add(ui.button(f"[[icon:broadcast]] {channel}", url=f"https://t.me/{clean}"))
    markup.add(ui.button("[[icon:ok]] Проверить подписку", callback_data="subscription:recheck"))
    return markup



def require_subscription_for_message(message) -> bool:
    if not require_private_message(message):
        return False
    remember_user_seen(message.from_user, getattr(message.chat, "id", None), observed_at=message_time(message), event_key=f'message:{message.chat.id}:{message.message_id}')
    if check_subscription(message.from_user.id):
        return True
    channels_text = "\n".join(CHANNEL_USERNAMES) if CHANNEL_USERNAMES else "каналы не настроены"
    bot.send_message(
        message.chat.id,
        "[[icon:lock]] Чтобы пользоваться ботом, сначала подпишись на эти каналы:\n\n"
        f"{channels_text}\n\n"
        "Потом нажми кнопку проверки.",
        reply_markup=subscription_keyboard(),
    )
    return False



def ensure_subscription_for_callback(call) -> bool:
    if not private_chat(call.message.chat):
        bot.answer_callback_query(call.id, "Пожалуйста, открой личный чат с ботом.", show_alert=True)
        return False
    remember_user_seen(call.from_user, getattr(call.message.chat, "id", None), source='bot_callback', event_key=f'callback:{call.id}')
    if check_subscription(call.from_user.id):
        return True
    try:
        bot.answer_callback_query(call.id, "Сначала подпишись на каналы.", show_alert=True)
    except Exception:
        pass
    bot.send_message(
        call.message.chat.id,
        "[[icon:lock]] Сначала подпишись на каналы и потом нажми кнопку проверки.",
        reply_markup=subscription_keyboard(),
    )
    return False



def set_state(user_id: int, state: str, **payload) -> None:
    with STATE_LOCK:
        identity_store.close_form(user_id)
        user_states[user_id] = {"state": state, "_created": time.monotonic(), **payload}


def get_state(user_id: int):
    with STATE_LOCK:
        data = user_states.get(user_id, {})
        if not data:
            data = identity_store.load_form(user_id)
            if data:
                user_states[user_id] = data
        if data and time.monotonic() - data.get("_created", 0) > USER_STATE_TTL_SECONDS:
            user_states.pop(user_id, None)
            identity_store.close_form(user_id)
            return {}
        return data


def clear_state(user_id: int) -> None:
    with STATE_LOCK:
        identity_store.close_form(user_id)
        user_states.pop(user_id, None)


def show_report_target_prompt(chat_id, user_id):
    state = get_state(user_id)
    request_id = secrets.randbelow(2**31 - 1) + 1
    identity_store.open_form(user_id, request_id, state)
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    button = types.KeyboardButton('Выбрать аккаунт', request_users=types.KeyboardButtonRequestUsers(
        request_id=request_id, max_quantity=1, request_username=True))
    icon = ui.mapping.get('user', {}).get('id')
    if icon:
        button.icon_custom_emoji_id = icon
    markup.add(button)
    markup.add(types.KeyboardButton('Отмена'))
    prompt = (f'[[icon:id]] <b>Уточнение аккаунта жалобы #{state["clarify_id"]}</b>\n\n'
        'Выбери аккаунт кнопкой или перешли сообщение с открытым автором. '
        'Уточнение проверит модератор; прежние записи не будут автоматически перенесены на выбранный аккаунт.'
        if state.get('clarify_id') else texts.REPORT)
    bot.send_message(chat_id, prompt, parse_mode='HTML', reply_markup=markup)


def remove_account_keyboard(chat_id):
    bot.send_message(chat_id, 'Выбор аккаунта завершён.', reply_markup=types.ReplyKeyboardRemove())


IDENTITY_OBSERVATION_ERROR_AT = 0.0


def log_identity_observation_error(exc):
    """Fail open for message handling; rate-limit diagnostics to one/minute."""
    global IDENTITY_OBSERVATION_ERROR_AT
    now = time.monotonic()
    with STATE_LOCK:
        if now - IDENTITY_OBSERVATION_ERROR_AT >= 60 or not IDENTITY_OBSERVATION_ERROR_AT:
            logger.warning('Passive Telegram ID observation failed (%s)', type(exc).__name__)
            IDENTITY_OBSERVATION_ERROR_AT = now


def observe_incoming_messages(messages):
    for message in messages:
        try:
            observe_message(identity_store, message)
        except Exception as exc:
            log_identity_observation_error(exc)


def install_identity_observer():
    raw = bot.raw
    if getattr(raw,'_identity_observer_installed',False):
        return
    original = raw.process_new_messages
    def process(messages):
        observe_incoming_messages(messages)
        return original(messages)
    raw.process_new_messages = process
    callbacks = raw.process_new_callback_query
    def process_callbacks(items):
        for call in items:
            try:
                user = getattr(call, 'from_user', None)
                if user:
                    identity_store.observe(user.id, getattr(user, 'username', None),
                        source='bot_callback', event_key=f'callback:{call.id}', actor_id=user.id)
            except Exception as exc:
                log_identity_observation_error(exc)
        return callbacks(items)
    raw.process_new_callback_query = process_callbacks
    raw._identity_observer_installed = True


def allow_check(user_id: int) -> bool:
    """Short in-process throttle for API lookups and Telegram traffic."""
    with STATE_LOCK:
        now = time.monotonic()
        if now - LAST_CHECKS.get(user_id, float('-inf')) < CHECK_COOLDOWN_SECONDS:
            return False
        LAST_CHECKS[user_id] = now
        if len(LAST_CHECKS) > 1024:
            oldest = min(LAST_CHECKS, key=LAST_CHECKS.get)
            LAST_CHECKS.pop(oldest, None)
        return True



def build_main_menu(user_id: int) -> types.InlineKeyboardMarkup:
    markup = ui.keyboard([
        [ui.button("Проверить аккаунт", callback_data="menu:check", icon="search", style="primary")],
        [ui.button("Подать жалобу", callback_data="menu:report", icon="report", style="primary"),
         ui.button("База жалоб", callback_data="menu:list:0", icon="list", style="primary")],
        [ui.button("Поддержка", callback_data="menu:support", icon="support", style="primary"),
         ui.button("Как это работает", callback_data="menu:help", icon="help", style=None)],
        [ui.button("Обжаловать запись", callback_data="menu:appeal", icon="appeal", style=None)],
        [ui.button("Уточнить аккаунт жалобы", callback_data="menu:clarify", icon="id", style=None)],
    ])
    if is_admin(user_id):
        markup.add(ui.button("Админ-панель", callback_data="admin:panel", icon="admin", style=None))
    if user_id in admin_chat_sessions:
        markup.add(ui.button("Завершить чат", callback_data="chat:end", icon="stop", style="danger"))
    return markup



def build_cancel_menu(with_back: bool = True) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    if with_back:
        buttons.append(ui.button("[[icon:back]] Назад", callback_data="menu:main"))
    buttons.append(ui.button("[[icon:cancel]] Отмена", callback_data="flow:cancel"))
    markup.add(*buttons)
    return markup



def build_report_wait_evidence_menu() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        ui.button("[[icon:back]] В меню", callback_data="menu:main"),
        ui.button("[[icon:cancel]] Отмена", callback_data="flow:cancel"),
    )
    return markup



def build_report_evidence_menu() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        ui.button("[[icon:ok]] Отправить жалобу", callback_data="report:submit"),
        ui.button("[[icon:delete]] Очистить вложения", callback_data="report:clear_evidence"),
    )
    markup.add(
        ui.button("[[icon:back]] В меню", callback_data="menu:main"),
        ui.button("[[icon:cancel]] Отмена", callback_data="flow:cancel"),
    )
    return markup




def show_main_menu(chat_id: int, user_id: int, *, message_id: int | None = None, text: str | None = None):
    content = text or texts.MAIN
    markup = build_main_menu(user_id)
    if message_id:
        bot.edit_message_text(content, chat_id, message_id, parse_mode="HTML", reply_markup=markup)
        return
    bot.send_message(chat_id, content, parse_mode="HTML", reply_markup=markup)



def valid_search_reference(raw_target: str) -> bool:
    value = str(raw_target or '').strip()
    return (bool(value.isdecimal() and 0 < int(value) <= MAX_USER_ID)
            or valid_target(value) and not value.isdecimal())


def public_identity_line(complaint: dict) -> str:
    user_id = complaint.get('target_user_id')
    if not identity_store._validate_user_id(user_id):
        if complaint.get('candidate_status') == 'candidate':
            return '[[icon:id]] Telegram ID: <i>есть неподтверждённые данные · проверяет модератор</i>'
        return '[[icon:id]] Telegram ID: <i>нет подтверждённой привязки</i>'
    account = identity_store.account(int(user_id))
    current_name = (account or {}).get('current_username')
    detail = (f' · последний наблюдавшийся ник: <b>{escape_html(current_name)}</b>'
              if current_name else '')
    provenance = (' · аккаунт указан заявителем через Telegram'
                  if complaint.get('identity_source') in {'bot_users_shared','bot_forward'}
                  else '')
    return f'[[icon:id]] Telegram ID: <code>{int(user_id)}</code>{detail}{provenance}'


def format_check_text(target: str) -> str:
    if not valid_search_reference(target):
        raise ValueError('Expected a Telegram @username or positive numeric ID')
    raw = str(target).strip()
    query = raw if raw.isdecimal() else normalize_target(raw)
    data = identity_store.public_matches(query)
    total = data['total']
    shown = data['rows']
    display = ('Telegram ID ' + query) if data['query_id'] is not None else query
    lines = [f'[[icon:search]] <b>Проверка {escape_html(display)}</b>']
    if not total:
        lines.append('\n[[icon:ok]] Опубликованных жалоб на этот ник или ID не найдено.')
        lines.append('Отсутствие жалоб не гарантирует безопасность сделки.')
    else:
        if data['query_id'] is not None:
            lines.append('\n[[icon:warning]] <b>Найдены опубликованные записи по указанному ID.</b>')
        else:
            lines.append('\n[[icon:warning]] <b>На этот ник есть опубликованные жалобы (в том числе по связанному Telegram ID).</b>')
        lines.append(f'Найдено записей: <b>{total}</b>.')
        lines.append('Жалоба — утверждение автора, а не доказательство вины.')
        lines.append('Наличие записи о старом нике не означает, что его нынешний владелец причастен.')

    aliases = data['aliases']
    historic = [a for a in aliases if not a['is_current'] or a['current_username'] != query]
    if historic:
        lines.append('\n[[icon:refresh]] <b>Исторический ник</b>')
        for alias in historic[:3]:
            current = alias['current_username']
            tag = f' (последний известный ник: {escape_html(current)})' if current else ''
            lines.append(f'Ранее <b>{escape_html(query)}</b> был связан с Telegram ID '
                         f'<code>{alias["user_id"]}</code>{tag}.')
        lines.append('[[icon:warning]] Сейчас этот @username может принадлежать другому человеку.')
    elif aliases:
        lines.append('\n[[icon:id]] Совпадение с ранее наблюдавшимся Telegram ID: '
                     + ', '.join(f'<code>{alias["user_id"]}</code>' for alias in aliases[:3]))
        lines.append('Проверка никнейма не удостоверяет, кто владеет им в данный момент.')

    if data['query_id'] and any(row['target_user_id'] is None for row in shown):
        lines.append('[[icon:warning]] Часть записей найдена по ID, введённому автором жалобы. '
                     'Telegram не подтверждал принадлежность этого ID указанному человеку.')

    if shown:
        lines.append('\n[[icon:pin]] <b>Последние опубликованные записи</b>')
        for row in shown:
            note = row['note'] or ''
            short_note = note if len(note) <= 130 else note[:130] + '…'
            lines.append(f'• <b>#{row["id"]}</b> · {escape_html(row["target"])} '
                         f'· {escape_html(format_timestamp_for_display(row["timestamp"]))}')
            lines.append('  ' + public_identity_line(row))
            if not row['target_user_id']:
                lines.append('  [[icon:warning]] Запись только по нику; личность не подтверждена.')
            elif data['query_alias'] and row['target'] != query:
                lines.append('  ↳ Найдено по истории ников этого Telegram ID.')
            lines.append('  ' + escape_html(short_note))
        if total > len(shown):
            lines.append(f'… и ещё {total - len(shown)} опубликованных записей.')

    parts = ['\n'.join(lines)]
    if data['query_alias']:
        if total == 0:
            profile_text = build_darknamescan_profile(query, complaints_count=0)
            if profile_text:
                parts.append(profile_text)
        social_text = format_public_profiles_text(query)
        if social_text:
            parts.append(social_text)
    return '\n\n'.join(parts)


def build_check_result_menu() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        ui.button("[[icon:refresh]] Проверить ещё", callback_data="menu:check"),
        ui.button("[[icon:back]] В меню", callback_data="menu:main"),
    )
    return markup



def show_list_page(chat_id: int, page: int = 0, *, message_id: int | None = None, user_id: int | None = None):
    total_row, _ = db_execute("SELECT COUNT(*) AS total FROM complaints WHERE status='accepted'", fetchone=True)
    total = total_row['total'] if total_row else 0
    if not total:
        show_main_menu(chat_id, user_id or 0, message_id=message_id,
                       text='[[icon:list]] Опубликованных жалоб пока нет.')
        return
    pages = max(1, (total + PUBLIC_PAGE_SIZE - 1) // PUBLIC_PAGE_SIZE)
    page = min(max(0, int(page)), pages - 1)
    rows, _ = db_execute("""SELECT c.id,c.target,c.target_user_id,c.identity_source,c.candidate_status,c.note,c.timestamp,
                               i.current_username
        FROM complaints c LEFT JOIN telegram_identities i ON i.user_id=c.target_user_id
        WHERE c.status='accepted' ORDER BY c.id DESC LIMIT ? OFFSET ?""",
        (PUBLIC_PAGE_SIZE, page * PUBLIC_PAGE_SIZE), fetchall=True)
    lines = [
        '[[icon:list]] <b>Опубликованные жалобы</b>',
        'Обращения, прошедшие модерацию. Жалоба сама по себе не доказывает вину.',
    ]
    lines.extend(complaint_card(dict(row)) for row in rows)
    if any(not row['target_user_id'] for row in rows):
        lines.append('<i>Записи без ID относятся к указанному нику. Его нынешний владелец может быть другим человеком.</i>')
    lines.append(f'Страница <b>{page + 1}/{pages}</b> · Записей: <b>{total}</b>')
    markup = types.InlineKeyboardMarkup(row_width=2)
    for row in rows:
        markup.add(ui.button(f'Подробнее · #{row["id"]}', icon='document',
                             callback_data=f'list:detail:{row["id"]}:{page}', style='primary'))
    nav = []
    if page:
        nav.append(ui.button('Назад', icon='back', callback_data=f'menu:list:{page-1}', style=None))
    if page + 1 < pages:
        nav.append(ui.button('Далее', icon='next', callback_data=f'menu:list:{page+1}', style=None))
    if nav:
        markup.add(*nav)
    if user_id == ADMIN_ID and chat_id == ADMIN_ID:
        stats = identity_store.summary()
        lines.append(f'<b>Администратору:</b> ID ждут сверки: {stats["unscanned"]}; '
                     f'кандидатов на подтверждение: {stats["candidates"]}.')
        if not identity_resolver.enabled:
            lines.append('ID накапливаются при общении с ботом, пересылке и выборе аккаунта.')
        markup.add(ui.button('Сверка Telegram ID', icon='id', callback_data='admin:identity:list:0'),
                   ui.button('Оформление', icon='palette', callback_data='admin:ui'))
    markup.add(ui.button('В меню', icon='home', callback_data='menu:main', style=None))
    text = '\n\n'.join(lines)
    if message_id:
        bot.edit_message_text(text, chat_id, message_id, parse_mode='HTML', reply_markup=markup)
    else:
        bot.send_message(chat_id, text, parse_mode='HTML', reply_markup=markup)


def show_public_detail(chat_id: int, complaint_id: int, page: int, *, message_id: int | None = None, user_id: int | None = None):
    complaint = get_complaint_by_id(complaint_id)
    if not complaint or complaint["status"] not in VISIBLE_STATUSES:
        bot.send_message(chat_id, "[[icon:warning]] Эту запись сейчас нельзя открыть из общего списка.")
        return

    label = status_label(complaint["status"])
    text = (
        f"[[icon:document]] <b>Жалоба #{complaint['id']}</b>\n\n"
        f"[[icon:user]] Ник при подаче: <b>{escape_html(complaint['target'])}</b>\n"
        f"{public_identity_line(complaint)}\n"
        f"[[icon:pin]] Метка: <b>{escape_html(label)}</b>\n"
        f"[[icon:report]] Описание:\n{escape_html(complaint['note'])}\n\n"
        f"[[icon:attachment]] Приложений (доступны модератору): <b>{len(complaint['evidence'])}</b>\n"
        f"[[icon:calendar]] Дата: <b>{escape_html(format_timestamp_for_display(complaint['timestamp']))}</b>"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        ui.button("[[icon:back]] К списку", callback_data=f"menu:list:{page}"),
        ui.button("[[icon:home]] В меню", callback_data="menu:main"),
    )
    if not complaint.get('target_user_id') and user_id in (complaint.get('from_id'),ADMIN_ID):
        markup.add(ui.button('Уточнить аккаунт',icon='id',callback_data=f'clarify:start:{complaint_id}'))
    if message_id:
        bot.edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=markup)
        return
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)



def build_admin_panel() -> types.InlineKeyboardMarkup:
    markup = ui.keyboard([
        [ui.button("На модерации", callback_data="admin:review:0", icon="report"),
         ui.button("Добавить запись", callback_data="admin:add:start", icon="add")],
        [ui.button("Telegram ID и привязки", callback_data="admin:identity:list:0", icon="id")],
        [ui.button("Рассылки", callback_data="admin:broadcast", icon="broadcast"),
         ui.button("Поддержка", callback_data="admin:support", icon="ticket")],
        [ui.button("Статистика", callback_data="admin:stats", icon="stats"),
         ui.button("Найти жалобу", callback_data="admin:find", icon="search")],
        [ui.button("Оформление", callback_data="admin:ui", icon="palette", style=None)],
        [ui.button("В меню", callback_data="menu:main", icon="home", style=None)],
    ])
    if ADMIN_ID in admin_chat_sessions:
        markup.add(ui.button("Завершить чат", callback_data="chat:end", icon="stop", style="danger"))
    return markup



def show_admin_panel(chat_id: int, *, message_id: int | None = None):
    review, _ = db_execute("SELECT COUNT(*) FROM complaints WHERE status IN ('review','pending')", fetchone=True)
    _, tickets_count = community_store.tickets(ADMIN_ID, admin_view=True)
    text = (
        "[[icon:admin]] <b>Управление ботом</b>\n"
        f"Сборка: <code>{BUILD_VERSION}</code>\n\n"
        f"Жалоб на проверке: <b>{review[0]}</b>\n"
        f"Открытых обращений: <b>{tickets_count}</b>\n"
        f"Получателей рассылок: <b>{community_store.audience_count()}</b>\n\n"
        "Модерируй записи, сверяй Telegram ID, отвечай пользователям и отправляй новости."
    )
    markup = build_admin_panel()
    if message_id:
        bot.edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=markup)
        return
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)



def show_admin_stats(chat_id: int, *, message_id: int | None = None):
    rows, _ = db_execute(
        "SELECT status, COUNT(*) AS total FROM complaints GROUP BY status ORDER BY status",
        fetchall=True,
    )
    total_row, _ = db_execute("SELECT COUNT(*) AS total FROM complaints", fetchone=True)
    total = total_row["total"] if total_row else 0
    total_users = get_total_seen_users()
    reporters_count = get_reporters_count()
    lines = [
        "[[icon:stats]] <b>Статистика по базе</b>",
        f"\n[[icon:users]] Пользователей за всё время: <b>{total_users}</b>",
        f"[[icon:report]] Людей, которые отправляли жалобы: <b>{reporters_count}</b>",
        f"[[icon:list]] Всего жалоб: <b>{total}</b>",
    ]
    for row in rows:
        lines.append(f"• {escape_html(status_label(row['status']))}: <b>{row['total']}</b>")
    markup = types.InlineKeyboardMarkup()
    markup.add(ui.button("[[icon:back]] В админ-панель", callback_data="admin:panel"))
    text = "\n".join(lines)
    if message_id:
        bot.edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=markup)
        return
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)



def run_identity_scan_batch(limit: int = IDENTITY_BATCH_SIZE) -> dict:
    """Resolve stored usernames; never automatically attribute *old* cases.

    'new' reports are linked to IDs observed when first checked. 'legacy' and
    'manual' cases require an admin to confirm the candidate's identity.
    """
    counts = {'targets': 0, 'linked': 0, 'candidates': 0, 'unavailable': 0, 'errors': 0,
              'rate_limited': False, 'retry_after': 0}
    if not identity_resolver.enabled:
        return counts
    for row in identity_store.scan_targets(limit=limit):
        if IDENTITY_STOP.is_set() or community_store.setting('identity_scan_paused', '0') == '1':
            break
        target = row['target']
        if not valid_target(target):
            identity_store.mark_invalid_target(target)
            counts['targets'] += 1
            counts['unavailable'] += 1
            continue
        try:
            identity = identity_resolver.resolve_username(target)
        except IdentityRateLimited as exc:
            logger.warning('Telegram requested a pause in username resolution')
            counts['rate_limited'] = True
            counts['retry_after'] = max(1, int(getattr(exc, 'retry_after', 60)))
            break
        except Exception as exc:
            logger.error('Could not resolve Telegram username (%s)', type(exc).__name__)
            counts['errors'] += 1
            break
        result = identity_store.apply_scan(target, identity)
        counts['targets'] += 1
        for key in ('linked', 'candidates', 'unavailable'):
            counts[key] += result[key]
    return counts


def run_identity_refresh_batch(limit: int = 2) -> int:
    """Recheck stable Telegram IDs to learn their newest visible usernames."""
    refreshed = 0
    if not identity_resolver.enabled:
        return refreshed
    for user_id in identity_store.pending_refresh_ids(limit=limit):
        if IDENTITY_STOP.is_set() or community_store.setting('identity_scan_paused', '0') == '1':
            break
        try:
            identity = identity_resolver.resolve_id(user_id)
            if not identity:
                account = identity_store.account(user_id)
                known_alias = (account or {}).get('current_username')
                if known_alias:
                    current_owner = identity_resolver.resolve_username(known_alias)
                    if current_owner and current_owner.user_id == user_id:
                        identity = current_owner
                    elif current_owner:
                        identity_store.observe(current_owner.user_id, current_owner.username,
                                               extra_usernames=current_owner.usernames,
                                               source='mtproto', checked_at=current_owner.checked_at)
            if identity and identity.user_id == user_id:
                identity_store.observe(user_id, identity.username,
                                       extra_usernames=identity.usernames,
                                       source='mtproto', checked_at=identity.checked_at)
                refreshed += 1
            else:
                identity_store.mark_refresh_unavailable(user_id)
        except IdentityRateLimited:
            logger.warning('Telegram requested a pause in account refresh')
            break
        except Exception as exc:
            logger.error('Could not refresh Telegram ID (%s)', type(exc).__name__)
            break
    return refreshed


identity_worker = IdentityMaintenance(
    resolver=lambda: identity_resolver, identities=identity_store, settings=community_store,
    scan=run_identity_scan_batch, refresh=run_identity_refresh_batch, lock=IDENTITY_JOB_LOCK,
    wake=IDENTITY_SCAN_WAKE, stop_event=IDENTITY_STOP,
    idle_seconds=IDENTITY_MAINTENANCE_SECONDS, batch_pause=IDENTITY_BACKLOG_PAUSE_SECONDS,
)


def start_manual_identity_scan(chat_id: int, target: str | None = None) -> None:
    identity_store.reconcile(alias=target)
    bot.send_message(chat_id, '[[icon:ok]] Известная история сопоставлена с жалобами. '
        'Совпадения ожидают ручной проверки. Запросы чужих аккаунтов не выполнялись.')


def identity_status_text() -> str:
    return ('<b>Накопление через Telegram включено.</b> ID сохраняются при общении с ботом, '
            'пересылке с открытым автором и выборе аккаунта. '
            'Поиск произвольного @username по сети без MTProto недоступен. '
            'Найденные связи с жалобами ждут проверки модератора.')


def _identity_ui_status(row: dict) -> str:
    if row.get('target_user_id'):
        return f'Связь с ID {row["target_user_id"]} подтверждена'
    status = row.get('candidate_status')
    if status == 'candidate':
        return f'Кандидат ID {row["candidate_user_id"]} · нужна проверка'
    return {
        'unscanned': 'ещё не проверен',
        'unavailable': 'ID не найден (на момент проверки)',
        'rejected': 'кандидат отклонён модератором',
        'invalid': 'старый ник имеет некорректный формат',
    }.get(status, 'ID не привязан')


def show_identity_page(chat_id: int, page: int = 0, *, message_id: int | None = None):
    page = max(0, min(int(page), 100000))
    stats = identity_store.summary()
    rows, total = identity_store.queue(page=page, page_size=PAGE_SIZE)
    lines = [
        '[[icon:id]] <b>Telegram ID и привязки</b>',
        f'\n[[icon:id]] Достоверно наблюдаемых ID: <b>{stats["observed_ids"]}</b>',
        f'[[icon:info]] Пока неизвестны: <b>{stats["unknown"]}</b>',
        f'\n[[icon:list]] Жалоб всего: <b>{stats["total"]}</b>',
        f'[[icon:ok]] С привязанным ID: <b>{stats["linked"]}</b>',
        f'[[icon:warning]] Кандидаты для проверки: <b>{stats["candidates"]}</b>',
        f'[[icon:search]] Ожидают сверки: <b>{stats["unscanned"]}</b>',
        f'[[icon:info]] Не определены / отклонены: <b>{stats["unresolved"]}</b>',
        '\nЧтобы посмотреть уже встречавшийся ID по нику, введи '
        '<code>/id @username</code> (только администратор). Это НЕ поиск по Telegram.'
        '\nСтарые записи нельзя автоматически приписывать текущему владельцу '
        'ника: его могли передать другому человеку.',
    ]
    if rows:
        lines.append('\n<b>Записи без подтверждённого ID:</b>')
        for row in rows:
            lines.append(f'#{row["id"]} · {escape_html(row["target"])}\n'
                         f'  [[icon:id]] {escape_html(_identity_ui_status(row))}')
    else:
        lines.append('\nВсе записи имеют ID или страница пуста.')
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    lines.append(f'\nСтраница {page+1}/{pages}.\n\n<b>Сверка:</b> ' + identity_status_text())
    markup = types.InlineKeyboardMarkup(row_width=2)
    for row in rows:
        markup.add(ui.button(f'Сверить #{row["id"]}',
                                              callback_data=f'admin:identity:detail:{row["id"]}:{page}'))
    nav = []
    if page > 0:
        nav.append(ui.button('[[icon:back]] Назад', callback_data=f'admin:identity:list:{page-1}'))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(ui.button('[[icon:next]] Далее', callback_data=f'admin:identity:list:{page+1}'))
    if nav:
        markup.add(*nav)
    markup.add(ui.button('[[icon:refresh]] Сопоставить известную историю',
                                          callback_data='admin:identity:scan', style='success'))
    markup.add(ui.button('Обновить прогресс', icon='refresh', callback_data=f'admin:identity:list:{page}'),
               ui.button('Пауза удалённой сверки', icon='pause', callback_data='admin:identity:pause', style='danger'))
    markup.add(ui.button('[[icon:back]] Админ-панель', callback_data='admin:panel'))
    markup.add(ui.button('История идентификаторов',icon='calendar',callback_data='admin:identity:history'))
    for key,label in CATEGORIES.items():
        markup.add(ui.button(label, icon='id', callback_data=f'admin:identity:filter:{key}:0'))
    markup.add(ui.button('Журнал изменений',icon='calendar',callback_data='admin:identity:audit:0'))
    text = '\n'.join(lines)
    if message_id:
        bot.edit_message_text(text, chat_id, message_id, parse_mode='HTML', reply_markup=markup)
        return
    bot.send_message(chat_id, text, parse_mode='HTML', reply_markup=markup)


def show_identity_detail(chat_id: int, complaint_id: int, page: int,
                         *, message_id: int | None = None):
    complaint = get_complaint_by_id(complaint_id)
    if not complaint:
        bot.send_message(chat_id, '[[icon:warning]] Жалоба не найдена.')
        return
    candidate = complaint.get('candidate_user_id')
    with DB_LOCK:
        findings = audit_database(conn, complaint_id)['findings']
    reason = findings[0]['reason'] if findings else 'Запись не найдена'
    text = (
        f'[[icon:id]] <b>Привязка жалобы #{complaint_id}</b>\n\n'
        f'Ник из записи: <b>{escape_html(complaint["target"])}</b>\n'
        f'Статус жалобы: {escape_html(status_label(complaint["status"]))}\n'
        f'Дата записи: {escape_html(format_timestamp_for_display(complaint["timestamp"]))}\n'
        f'Текущий ID в жалобе: <code>{complaint.get("target_user_id") or "не установлен"}</code>\n'
        f'Кандидат ID: <code>{candidate or "не найден"}</code>\n'
        f'Ник кандидата при сверке: '
        f'{escape_html(complaint.get("candidate_username") or "отсутствует")}\n'
        f'Дата сверки: {escape_html(complaint.get("candidate_checked_at") or "не проверялся")}\n'
        f'Результат: {escape_html(_identity_ui_status(complaint))}\n'
        f'Причина: {escape_html(reason)}\n\n'
        '[[icon:warning]] Найденный сегодня ID не доказывает, кто использовал ник '
        'во время старой жалобы. Подтверждай только при наличии доказательств.'
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    choices = identity_store.candidates(complaint_id)
    reviews, _ = db_execute('''SELECT user_id,evidence,reviewed_at,action,actor_id FROM identity_review_audit
        WHERE complaint_id=? ORDER BY id DESC LIMIT 3''',(complaint_id,),fetchall=True)
    for review in reviews:
        text += (f'\n\n{escape_html(review["action"])} · ID {review["user_id"]} · автор решения {review["actor_id"]}: {escape_html(review["reviewed_at"])}\n'
                 f'Основание: {escape_html(review["evidence"][:400])}')
    if complaint.get('target_user_id'):
        markup.add(ui.button('История подтверждённого ID',icon='calendar',
            callback_data=f'admin:identity:history:{complaint["target_user_id"]}'))
    for choice in choices[:8]:
        text += (f'\n\nКандидат ID <code>{choice["user_id"]}</code> · '
                 f'{escape_html(choice["source"])} · {escape_html(choice["observed_at"])}')
        markup.add(ui.button(f'Проверить ID {choice["user_id"]}', icon='id',
            callback_data=f'admin:identity:verify:{complaint_id}:{choice["user_id"]}'))
        markup.add(ui.button('История этого ID', icon='calendar',
            callback_data=f'admin:identity:history:{choice["user_id"]}'))
    if candidate and complaint['candidate_status'] == 'candidate':
        markup.add(ui.button('[[icon:ok]] Подтвердить связь',
                    callback_data=f'admin:identity:approve:{complaint_id}:{page}'),
                   ui.button('[[icon:cancel]] Не связывать',
                    callback_data=f'admin:identity:reject:{complaint_id}:{page}'))
    if not complaint.get('target_user_id'):
        markup.add(ui.button('Уточнить аккаунт',icon='user',callback_data=f'admin:identity:clarify:{complaint_id}'))
        markup.add(ui.button('[[icon:refresh]] Повторить сверку',
                    callback_data=f'admin:identity:reset:{complaint_id}:{page}'))
    else:
        markup.add(ui.button('[[icon:delete]] Отвязать ошибочный ID',
                    callback_data=f'admin:identity:unlink:{complaint_id}:{page}'))
    markup.add(ui.button('[[icon:back]] К списку',
                callback_data=f'admin:identity:list:{page}'))
    if message_id:
        bot.edit_message_text(text, chat_id, message_id, parse_mode='HTML', reply_markup=markup)
        return
    bot.send_message(chat_id, text, parse_mode='HTML', reply_markup=markup)


def show_review_page(chat_id: int, page: int = 0, *, message_id: int | None = None):
    page = max(0, min(int(page), 100000))
    offset = page * PAGE_SIZE
    review_statuses = tuple(dict.fromkeys((REVIEW_STATUS, "pending")))
    placeholders = ",".join("?" for _ in review_statuses)
    rows, _ = db_execute(
        f"""
        SELECT id, target, target_user_id, candidate_user_id, from_user, note, timestamp
        FROM complaints
        WHERE status IN ({placeholders})
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        (*review_statuses, PAGE_SIZE, offset),
        fetchall=True,
    )
    total_row, _ = db_execute(
        f"SELECT COUNT(*) AS total FROM complaints WHERE status IN ({placeholders})",
        review_statuses,
        fetchone=True,
    )
    total = total_row["total"] if total_row else 0
    if not rows:
        text = "[[icon:ok]] На модерации сейчас ничего нет."
        markup = types.InlineKeyboardMarkup()
        markup.add(ui.button("[[icon:back]] В админ-панель", callback_data="admin:panel"))
        if message_id:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)
            return
        bot.send_message(chat_id, text, reply_markup=markup)
        return

    lines = ["[[icon:add]] <b>Жалобы на модерации</b>\n"]
    for row in rows:
        short_note = row["note"] if len(row["note"]) <= 90 else row["note"][:90] + "…"
        lines.append(
            f"[[icon:document]] <b>#{row['id']}</b> | {escape_html(row['target'])}\n"
            f"[[icon:id]] ID: {row['target_user_id'] if row['target_user_id'] else 'не установлен'}"
            f"{(' · кандидат ' + str(row['candidate_user_id'])) if row['candidate_user_id'] else ''}\n"
            f"[[icon:user]] От: {escape_html(row['from_user'] or 'неизвестно')}\n"
            f"[[icon:report]] {escape_html(short_note)}"
        )
    page_count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    lines.append(f"\n[[icon:help]] Страница <b>{page + 1}/{page_count}</b>")

    markup = types.InlineKeyboardMarkup(row_width=2)
    for row in rows:
        markup.add(ui.button(f"Открыть #{row['id']}", callback_data=f"admin:reviewdetail:{row['id']}:{page}"))
    nav = []
    if page > 0:
        nav.append(ui.button("[[icon:back]] Назад", callback_data=f"admin:review:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(ui.button("Вперёд [[icon:next]]", callback_data=f"admin:review:{page + 1}"))
    if nav:
        markup.add(*nav)
    markup.add(ui.button("[[icon:back]] В админ-панель", callback_data="admin:panel"))

    text = "\n\n".join(lines)
    if message_id:
        bot.edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=markup)
        return
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)



def build_review_detail_markup(complaint_id: int, page: int) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    complaint = get_complaint_by_id(complaint_id)
    status = complaint["status"] if complaint else None
    if status in (REVIEW_STATUS, 'pending'):
        markup.add(
            ui.button("[[icon:ok]] Принять", callback_data=f"admin:approve:{complaint_id}:{page}"),
            ui.button("[[icon:cancel]] Отклонить", callback_data=f"admin:decline:{complaint_id}:{page}"),
        )
    elif status == 'accepted':
        markup.add(ui.button("[[icon:delete]] Снять с публикации", callback_data=f"admin:hide:{complaint_id}:{page}"))
    elif status in ('removed', 'declined'):
        markup.add(ui.button("[[icon:refresh]] На пересмотр", callback_data=f"admin:reopen:{complaint_id}:{page}"))
    markup.add(
        ui.button("[[icon:attachment]] Показать доказательства", callback_data=f"admin:evidence:{complaint_id}:{page}"),
        ui.button("[[icon:support]] Чат с автором", callback_data=f"admin:chat:{complaint_id}:{page}"),
    )
    markup.add(ui.button("[[icon:back]] К очереди", callback_data=f"admin:review:{page}"))
    return markup



def show_review_detail(chat_id: int, complaint_id: int, page: int, *, message_id: int | None = None):
    complaint = get_complaint_by_id(complaint_id)
    if not complaint:
        bot.send_message(chat_id, "[[icon:warning]] Жалоба не найдена.")
        return
    text = (
        f"[[icon:document]] <b>Жалоба #{complaint['id']}</b>\n\n"
        f"[[icon:user]] Ник при добавлении: <b>{escape_html(complaint['target'])}</b>\n"
        f"{public_identity_line(complaint)}\n"
        f"[[icon:attachment]] Источник привязки: <b>{escape_html(complaint.get('identity_source') or 'нет')}</b>\n"
        f"[[icon:user]] От: <b>{escape_html(complaint['from_user'] or 'неизвестно')}</b>\n"
        f"[[icon:id]] ID автора: <code>{complaint['from_id']}</code>\n"
        f"[[icon:document]] Статус: <b>{escape_html(status_label(complaint['status']))}</b>\n"
        f"[[icon:attachment]] Приложений (доступны модератору): <b>{len(complaint['evidence'])}</b>\n"
        f"[[icon:calendar]] Дата: <b>{escape_html(format_timestamp_for_display(complaint['timestamp']))}</b>\n\n"
        f"[[icon:report]] Описание:\n{escape_html(complaint['note'])}"
    )
    markup = build_review_detail_markup(complaint_id, page)
    if message_id:
        bot.edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=markup)
        return
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)



def send_admin_complaint_notification(complaint_id: int) -> None:
    complaint = get_complaint_by_id(complaint_id)
    if not complaint:
        return
    markup = build_review_detail_markup(complaint_id, 0)
    text = (
        f"[[icon:warning]] <b>Новая жалоба #{complaint_id}</b>\n\n"
        f"[[icon:user]] От: {escape_html(complaint['from_user'] or 'неизвестно')}\n"
        f"[[icon:user]] На: {escape_html(complaint['target'])}\n"
        f"[[icon:report]] Кратко: {escape_html(complaint['note'][:200])}\n"
        f"[[icon:attachment]] Доказательств приложено: <b>{len(complaint['evidence'])}</b>"
    )
    bot.send_message(ADMIN_ID, text, parse_mode="HTML", reply_markup=markup)



def change_complaint_status(complaint_id: int, new_status: str) -> bool:
    """Atomically moderate once; prevents duplicate decisions and notifications."""
    if new_status not in {"accepted", "declined"}:
        raise ValueError("Unknown moderation status")
    with DB_LOCK:
        cursor = conn.execute(
            "UPDATE complaints SET status = ? WHERE id = ? AND status IN (?, ?)",
            (new_status, complaint_id, REVIEW_STATUS, "pending"),
        )
        changed = cursor.rowcount == 1
        conn.commit()
        cursor.close()
        return changed


def reopen_complaint(complaint_id: int, *, hide: bool = False) -> bool:
    """Hide accepted complaints immediately, or reopen closed complaints."""
    with DB_LOCK:
        if hide:
            cursor = conn.execute(
                "UPDATE complaints SET status = 'removed' WHERE id = ? AND status = 'accepted'",
                (complaint_id,),
            )
        else:
            cursor = conn.execute(
                "UPDATE complaints SET status = ? WHERE id = ? AND status IN ('removed', 'declined')",
                (REVIEW_STATUS, complaint_id),
            )
        changed = cursor.rowcount == 1
        conn.commit()
        cursor.close()
        return changed


def insert_manual_scammers(targets: list[str], note: str) -> list[str]:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    added = []
    created_ids = []
    with DB_LOCK:
        cursor = conn.cursor()
        for target in targets:
            if not valid_target(target):
                raise ValueError("Некорректный ник для ручного добавления")
            cursor.execute(
                """
                INSERT INTO complaints (target, from_user, from_id, note, evidence, timestamp, status, identity_origin)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'manual')
                """,
                (target, "Администратор", ADMIN_ID, note, json.dumps([{"type": "text", "data": "Добавлено вручную"}]), timestamp, MANUAL_ADD_STATUS),
            )
            added.append(target)
            created_ids.append(cursor.lastrowid)
        conn.commit()
        cursor.close()
    for complaint_id in created_ids:
        identity_store.reconcile(complaint_id=complaint_id)
    if added and identity_resolver.enabled:
        IDENTITY_SCAN_WAKE.set()
    return added



def finalize_complaint(user_id: int, chat_id: int) -> None:
    with STATE_LOCK:
        data = get_state(user_id)
        if not data or data.get("state") != STATE_WAITING_FOR_REPORT_EVIDENCE:
            bot.send_message(chat_id, "[[icon:warning]] Данные жалобы не найдены. Начни заново через меню.")
            return
        evidence = data.get("evidence", [])
        if not evidence:
            bot.send_message(
                chat_id,
                "[[icon:warning]] Сначала добавь хотя бы одно доказательство. Подойдёт скрин, файл, видео, голосовое или текст.",
                reply_markup=build_report_wait_evidence_menu(),
            )
            return
        if not valid_search_reference(data.get("target", "")):
            bot.send_message(chat_id, "[[icon:warning]] Некорректный ник. Начни жалобу заново.")
            return
        if not 5 <= len(data.get("note", "")) <= MAX_NOTE_LENGTH:
            bot.send_message(chat_id, "[[icon:warning]] Некорректная длина описания. Начни жалобу заново.")
            return
        row, _ = db_execute(
            "SELECT COUNT(*) AS total FROM complaints WHERE from_id = ? AND datetime(timestamp) >= datetime('now', '-1 day')",
            (user_id,), fetchone=True,
        )
        if not is_admin(user_id) and row["total"] >= 5:
            bot.send_message(chat_id, "[[icon:warning]] Лимит — 5 жалоб за сутки. Попробуй позже.")
            return

        username = data.get("username") or f"user_{user_id}"
        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            complaint_id, created = identity_store.create_report(
                data, user_id, username, data['note'], evidence, REVIEW_STATUS)
        except (sqlite3.Error, ValueError) as exc:
            logger.warning('Complaint transaction failed (%s)', type(exc).__name__)
            bot.send_message(chat_id, 'Не удалось отправить жалобу. Данные не потеряны. '
                'Попробуй ещё раз; если выбор аккаунта устарел, начни новую жалобу через меню.')
            return
        if not created:
            clear_state(user_id)
            bot.send_message(chat_id, 'Эта жалоба уже отправлена на проверку.', reply_markup=build_main_menu(user_id))
            return
        clear_state(user_id)
    if identity_resolver.enabled:
        IDENTITY_SCAN_WAKE.set()
    try:
        send_admin_complaint_notification(complaint_id)
    except Exception as exc:
        logger.error("Не удалось уведомить администратора о жалобе #%s (%s)", complaint_id, type(exc).__name__)
    bot.send_message(
        chat_id,
        "[[icon:ok]] Готово. Жалоба отправлена на проверку.\n\n"
        "Когда по ней будет решение, бот напишет сюда.",
        reply_markup=build_main_menu(user_id),
    )



def start_direct_chat(admin_id: int, complaint_id: int, *, message_id: int | None = None, page: int = 0):
    if not is_admin(admin_id):
        return
    complaint = get_complaint_by_id(complaint_id)
    if not complaint:
        bot.send_message(admin_id, "[[icon:warning]] Жалоба не найдена.")
        return
    user_id = complaint["from_id"]
    if not user_id or user_id == admin_id:
        bot.send_message(admin_id, "[[icon:warning]] У этой жалобы нет подходящего автора для чата.")
        return

    if get_state(user_id) or get_state(admin_id):
        bot.send_message(admin_id, "Сейчас один из участников заполняет форму. "
                         "Дождись её завершения или ответь через раздел поддержки.")
        return
    with SESSION_LOCK:
        existing = admin_chat_sessions.get(admin_id)
        other = admin_chat_sessions.get(user_id)
        if (existing and existing != user_id) or (other and other != admin_id):
            bot.send_message(admin_id, "[[icon:support]] Один из участников уже в другом чате. Сначала заверши текущий диалог.")
            return
        admin_chat_sessions[admin_id] = user_id
        admin_chat_sessions[user_id] = admin_id
    markup = types.InlineKeyboardMarkup()
    markup.add(ui.button("[[icon:stop]] Завершить чат", callback_data="chat:end"))
    bot.send_message(admin_id, f"[[icon:support]] Чат с {complaint['from_user']} запущен.", reply_markup=markup)
    try:
        bot.send_message(user_id, "[[icon:appeal]] По твоей жалобе пришло сообщение от администратора.", reply_markup=markup)
    except Exception:
        pass
    if message_id:
        show_review_detail(admin_id, complaint_id, page, message_id=message_id)



def finish_chat(user_id: int, chat_id: int, callback_id: str | None = None):
    if user_id not in admin_chat_sessions:
        if callback_id:
            bot.answer_callback_query(callback_id, "Активного чата нет.")
        else:
            bot.send_message(chat_id, "[[icon:info]] Сейчас активного чата нет.")
        return
    with SESSION_LOCK:
        partner = admin_chat_sessions.pop(user_id, None)
        if partner is not None and admin_chat_sessions.get(partner) == user_id:
            admin_chat_sessions.pop(partner, None)
    if callback_id:
        bot.answer_callback_query(callback_id, "Чат завершён.")
    bot.send_message(chat_id, "[[icon:ok]] Диалог закрыт.", reply_markup=build_main_menu(user_id))
    if partner:
        try:
            bot.send_message(partner, "[[icon:info]] Диалог закрыт.", reply_markup=build_main_menu(partner))
        except Exception:
            pass


def leave_compose_mode(user_id: int, chat_id: int) -> None:
    """Explicit navigation must not leave an old private relay running."""
    if get_state(user_id).get("request_id"):
        remove_account_keyboard(chat_id)
    clear_state(user_id)
    if user_id in admin_chat_sessions:
        finish_chat(user_id, chat_id)


community = CommunityFeatures(
    bot=bot, types_module=types, store=community_store, ui=ui,
    set_state=set_state, get_state=get_state, clear_state=clear_state,
    remember=remember_user_seen, main_menu=build_main_menu, admin_id=ADMIN_ID,
    per_second=float(_get_env("BROADCAST_PER_SECOND", "8")),
)

def state_handler(expected):
    """Recheck state after acquiring the same user lock as menu navigation.

    TeleBot evaluates filters before calling a handler. A queued handler may
    therefore be stale by the time another worker finishes a navigation action.
    """
    def decorate(handler):
        @community.serialized
        @wraps(handler)
        def guarded(message, *args, **kwargs):
            if get_state(message.from_user.id).get("state") != expected:
                return
            data=get_state(message.from_user.id)
            before=json.loads(json.dumps({k:v for k,v in data.items() if k!='_created'}))
            payload=[getattr(message,'text',None),getattr(message,'content_type',None)]
            for kind in ('document','video','audio','voice'):
                payload.append(getattr(getattr(message,kind,None),'file_id',None))
            payload.append([getattr(x,'file_id',None) for x in (getattr(message,'photo',None) or [])])
            key=f'{message.chat.id}:{message.message_id}:'+hashlib.sha256(json.dumps(payload).encode()).hexdigest()
            handled=data.setdefault('_handled_inputs',[])
            if key in handled:return
            handled.append(key)
            if len(handled)>64:handled.pop(0)
            try:
                return handler(message, *args, **kwargs)
            except sqlite3.Error:
                before['_created']=data.get('_created',time.monotonic())
                user_states[message.from_user.id]=before
                bot.send_message(message.chat.id,'Не удалось сохранить данные. Попробуй отправить сообщение ещё раз.')
            finally:
                try:
                    identity_store.save_form(message.from_user.id, get_state(message.from_user.id))
                except sqlite3.Error as exc:
                    log_identity_observation_error(exc)
        return guarded
    return decorate


def begin_clarification(chat_id, user_id, complaint_id):
    row=get_complaint_by_id(complaint_id)
    if not row or user_id not in (row.get('from_id'),ADMIN_ID) or row.get('target_user_id'):
        bot.send_message(chat_id,'Можно уточнить только свою жалобу без установленного ID. '
            'Для исправления подтверждённой записи напиши в поддержку.')
        return
    leave_compose_mode(user_id,chat_id)
    set_state(user_id,STATE_WAITING_FOR_REPORT_TARGET,clarify_id=complaint_id)
    show_report_target_prompt(chat_id,user_id)


@bot.message_handler(commands=['clarify'])
@community.serialized
def clarify_command(message):
    if not require_subscription_for_message(message):return
    args=(message.text or '').split()
    if len(args)!=2 or not args[1].lstrip('#').isdecimal():
        bot.send_message(message.chat.id,'Укажи номер своей жалобы: /clarify #123. '
            'Ты сможешь выбрать аккаунт для проверки модератором.')
        return
    begin_clarification(message.chat.id,message.from_user.id,int(args[1].lstrip('#')))


def show_identity_filter(chat_id, category, page=0):
    if chat_id!=ADMIN_ID or category not in CATEGORIES:return
    page=max(0,min(int(page),100000))
    with DB_LOCK:
        audit=audit_database(conn)
    found=[r for r in audit['findings'] if r['category']==category]
    markup=types.InlineKeyboardMarkup()
    lines=[f'[[icon:id]] <b>{escape_html(CATEGORIES[category])}</b>',f'Записей: {len(found)}']
    for r in found[page*PAGE_SIZE:(page+1)*PAGE_SIZE]:
        lines.append(f'#{r["complaint_id"]} · {escape_html(r["reason"])}')
        markup.add(ui.button(f'Открыть #{r["complaint_id"]}',icon='id',callback_data=f'admin:identity:detail:{r["complaint_id"]}:0'))
    if page:markup.add(ui.button('Назад',icon='back',callback_data=f'admin:identity:filter:{category}:{page-1}'))
    if (page+1)*PAGE_SIZE<len(found):markup.add(ui.button('Далее',icon='next',callback_data=f'admin:identity:filter:{category}:{page+1}'))
    markup.add(ui.button('Telegram ID и привязки',icon='back',callback_data='admin:identity:list:0'))
    bot.send_message(chat_id,'\n\n'.join(lines),parse_mode='HTML',reply_markup=markup)


def show_identity_audit(chat_id,page=0):
    if chat_id!=ADMIN_ID:return
    page=max(0,min(int(page),100000))
    rows,_=db_execute('''SELECT * FROM identity_review_audit ORDER BY id DESC LIMIT ? OFFSET ?''',
                       (PAGE_SIZE,page*PAGE_SIZE),fetchall=True)
    lines=['[[icon:calendar]] <b>История решений по привязкам</b>']
    for r in rows:
        lines.append(f'#{r["complaint_id"]} · {escape_html(r["action"])} · ID {r["user_id"]}\n'
            f'Кто: {r["actor_id"]} · Когда: {escape_html(r["reviewed_at"])}\n'
            f'Основание: {escape_html(r["evidence"][:300])}')
    if not rows:lines.append('Изменений пока нет.')
    markup=types.InlineKeyboardMarkup()
    if page:markup.add(ui.button('Назад',icon='back',callback_data=f'admin:identity:audit:{page-1}'))
    if len(rows)==PAGE_SIZE:markup.add(ui.button('Далее',icon='next',callback_data=f'admin:identity:audit:{page+1}'))
    markup.add(ui.button('К привязкам',icon='back',callback_data='admin:identity:list:0'))
    bot.send_message(chat_id,'\n\n'.join(lines),parse_mode='HTML',reply_markup=markup)


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get('state') == STATE_IDENTITY_EVIDENCE
                     and not (m.text or '').startswith('/'),
                     content_types=['text'])
@state_handler(STATE_IDENTITY_EVIDENCE)
def handle_identity_evidence(message):
    if not require_private_message(message) or not is_admin(message.from_user.id):
        return
    state = get_state(message.from_user.id)
    evidence = (message.text or '').strip()
    if not 8 <= len(evidence) <= 1200:
        bot.send_message(message.chat.id, 'Укажи основание связи и ссылку или номер доказательства (8–1200 символов).')
        return
    action = state.get('action','link')
    if action == 'link':
        ok = identity_store.reviewed_link(state['complaint_id'], state['candidate_id'], message.from_user.id, evidence)
    elif action == 'unlink':
        ok = identity_store.unlink(state['complaint_id'], message.from_user.id, evidence, state.get('expected_user_id'))
    elif action == 'reject':
        ok = identity_store.reject_candidate(state['complaint_id'], message.from_user.id, evidence)
    else:
        ok = identity_store.reset_scan(state['complaint_id'], message.from_user.id, evidence)
    clear_state(message.from_user.id)
    bot.send_message(message.chat.id, 'Решение сохранено вместе с основанием проверки.' if ok else
        'Связь уже изменена. Открой запись заново.', reply_markup=build_admin_panel())


def request_identity_review(chat_id, complaint_id, candidate_id):
    row = get_complaint_by_id(complaint_id)
    if not row or row.get('target_user_id') or not candidate_id:
        bot.send_message(chat_id, 'Кандидат недоступен или связь уже подтверждена.')
        return
    set_state(chat_id, STATE_IDENTITY_EVIDENCE, complaint_id=complaint_id, candidate_id=candidate_id, action='link')
    bot.send_message(chat_id, f'[[icon:warning]] Проверка жалобы #{complaint_id}, ID {candidate_id}.\n\n'
        'Укажи основание связи с жалобой и ссылку или номер доказательства. '
        'Совпадение ника или выбор аккаунта автором жалобы недостаточны. '
        'Введённое основание останется только у модератора.', reply_markup=build_cancel_menu())


def show_identity_history(chat_id, user_id=None, page=0):
    page = max(0,min(int(page),100000))
    markup = types.InlineKeyboardMarkup()
    if user_id is None:
        rows, _ = db_execute('''SELECT user_id,MAX(observed_at) AS seen FROM identity_observations
            GROUP BY user_id ORDER BY seen DESC LIMIT 15 OFFSET ?''',(page*15,),fetchall=True)
        text = '[[icon:calendar]] <b>Последние известные ID</b>\nВыбери ID, чтобы увидеть наблюдения.'
        for row in rows:
            markup.add(ui.button(str(row['user_id']), icon='id', callback_data=f'admin:identity:history:{row["user_id"]}'))
        if page:
            markup.add(ui.button('Назад',icon='back',callback_data=f'admin:identity:accounts:{page-1}'))
        if len(rows)==15:
            markup.add(ui.button('Далее',icon='next',callback_data=f'admin:identity:accounts:{page+1}'))
    else:
        rows = identity_store.history(user_id,page=page)
        text = f'[[icon:calendar]] <b>История ID {user_id}</b>\n'
        for row in rows:
            text += f'\n{escape_html(row["username"] or "Без username")} · {escape_html(row["observed_at"])} · {escape_html(row["source"])}'
        if not rows:
            text += '\nНаблюдений Telegram пока нет.'
        if page:
            markup.add(ui.button('Предыдущие',icon='back',callback_data=f'admin:identity:history:{user_id}:{page-1}'))
        if len(rows)==12:
            markup.add(ui.button('Ещё наблюдения',icon='next',callback_data=f'admin:identity:history:{user_id}:{page+1}'))
        markup.add(ui.button('Все известные ID',icon='users',callback_data='admin:identity:history'))
    markup.add(ui.button('К сверке',icon='back',callback_data='admin:identity:list:0'))
    bot.send_message(chat_id,text,parse_mode='HTML',reply_markup=markup)


@bot.message_handler(
    func=lambda m: not private_chat(m.chat),
    content_types=["text", "photo", "document", "video", "audio", "voice", "sticker"],
)
def reject_group_messages(message):
    require_private_message(message)


@bot.message_handler(commands=["start", "menu"])
@community.serialized
def start(message):
    if not require_private_message(message):
        return
    leave_compose_mode(message.from_user.id, message.chat.id)
    if not require_subscription_for_message(message):
        return
    show_main_menu(message.chat.id, message.from_user.id)


@bot.message_handler(commands=["check"])
@community.serialized
def check_command(message):
    if not require_subscription_for_message(message):
        return
    leave_compose_mode(message.from_user.id, message.chat.id)
    args = message.text.split(maxsplit=1)
    if len(args) >= 2 and args[1].strip():
        if not valid_search_reference(args[1]):
            bot.send_message(message.chat.id, "[[icon:warning]] Пришли @username или числовой Telegram ID.")
            return
        if not allow_check(message.from_user.id):
            bot.send_message(message.chat.id, "[[icon:wait]] Между проверками должно пройти 5 секунд.")
            return
        bot.send_message(
            message.chat.id,
            format_check_text(args[1].strip()),
            parse_mode="HTML",
            reply_markup=build_check_result_menu(),
        )
        return
    set_state(message.from_user.id, STATE_WAITING_FOR_CHECK_TARGET)
    bot.send_message(
        message.chat.id,
        texts.CHECK,
        parse_mode="HTML",
        reply_markup=build_cancel_menu(),
    )


@bot.message_handler(commands=["report"])
@community.serialized
def report_command(message):
    if not require_subscription_for_message(message):
        return
    start_event=f'message:{message.chat.id}:{message.message_id}'
    if get_state(message.from_user.id).get('_start_event')==start_event:
        bot.send_message(message.chat.id,'Форма уже открыта. Продолжи заполнение или отмени её через меню.')
        return
    leave_compose_mode(message.from_user.id, message.chat.id)
    set_state(message.from_user.id, STATE_WAITING_FOR_REPORT_TARGET,
              _start_event=start_event,
              username=f"@{message.from_user.username}" if message.from_user.username else None)
    show_report_target_prompt(message.chat.id, message.from_user.id)


@bot.message_handler(commands=["list"])
@community.serialized
def list_command(message):
    if not require_subscription_for_message(message):
        return
    leave_compose_mode(message.from_user.id, message.chat.id)
    show_list_page(message.chat.id, 0, user_id=message.from_user.id)


@bot.message_handler(commands=['myid'])
@community.serialized
def myid_command(message):
    """Show the authenticated sender their own Telegram ID, not somebody else's."""
    if not require_private_message(message) or not getattr(message, 'from_user', None):
        return
    user = message.from_user
    if message.chat.id != user.id:
        return
    try:
        identity_store.observe(user.id, getattr(user, 'username', None),
            source='bot_message', checked_at=message_time(message), event_key=f'message:{message.chat.id}:{message.message_id}',
            actor_id=user.id)
    except Exception as exc:
        log_identity_observation_error(exc)
    bot.send_message(message.chat.id,
        f'[[icon:id]] <b>Твой Telegram ID</b>: <code>{int(user.id)}</code>\n\n'
        'Это ID отправителя, подтверждённый Telegram. '
        'Он не устанавливает личность людей из жалоб.', parse_mode='HTML')


@bot.message_handler(commands=['id'])
@community.serialized
def admin_id_lookup_command(message):
    """Private lookup of IDs actually observed by THIS bot; no remote username probing."""
    if (not require_private_message(message) or not is_admin(message.from_user.id)
            or message.chat.id != message.from_user.id):
        return
    parts = (message.text or '').split(maxsplit=1)
    if len(parts) != 2 or not valid_search_reference(parts[1].strip()):
        bot.send_message(message.chat.id,
            'Используй <code>/id @username</code> или <code>/id 123456789</code>.\n'
            'Команда ищет только ранее наблюдавшиеся аккаунты, не обращается к Telegram за чужими ID.',
            parse_mode='HTML')
        return
    query = parts[1].strip()
    rows = identity_store.lookup_observed(query)
    if not rows:
        text = ('[[icon:info]] <b>Наблюдаемый ID не найден</b> для '
                f'<code>{escape_html(query)}</code>.\n\n'
                'Это не доказательство, что аккаунта нет: бот не может найти ID '
                'произвольного человека по username через Bot API. '
                'Нужен открытый отправитель пересылки, выбор аккаунта или MTProto.')
    else:
        text = (f'[[icon:id]] <b>Ранее наблюдавшиеся Telegram ID</b> для '
                f'<code>{escape_html(query)}</code>:')
        for row in rows:
            current = row['current_username'] or 'нет наблюдаемого текущего ника'
            text += (f'\n\n• <code>{row["user_id"]}</code>'
                     f'\n  Последний раз: {escape_html(row["last_seen"])}'
                     f'\n  Последний наблюдаемый ник: {escape_html(current)}')
        text += ('\n\n[[icon:warning]] Старый ник мог перейти другому аккаунту. '
                 'Это наблюдения, не подтверждённые связи с жалобами. '
                 'Не используй их для обвинений без независимой проверки.')
    bot.send_message(message.chat.id,text,parse_mode='HTML')


@bot.message_handler(commands=['bindid'])
def admin_bindid_command(message):
    if not require_private_message(message) or not is_admin(message.from_user.id):
        return
    args = (message.text or '').split(maxsplit=3)
    if len(args) != 4 or not args[1].lstrip('#').isdecimal() or not args[2].isdecimal():
        bot.send_message(message.chat.id, 'Формат: <code>/bindid #12 123456789 основание и ссылка на доказательство</code>. '
                         'Указывай только ID, подтверждённый доказательствами.', parse_mode='HTML')
        return
    report_id, tg_id = int(args[1].lstrip('#')), int(args[2])
    try:
        linked = identity_store.reviewed_link(report_id, tg_id, message.from_user.id, args[3])
    except ValueError:
        bot.send_message(message.chat.id, 'Некорректный Telegram ID.')
        return
    bot.send_message(message.chat.id,
                     ('[[icon:ok]] Жалоба связана с ID. История ника сохранена.' if linked
                      else '[[icon:warning]] Жалоба не найдена.'))


@bot.message_handler(commands=['unlinkid'])
def admin_unlinkid_command(message):
    if not require_private_message(message) or not is_admin(message.from_user.id):
        return
    args = (message.text or '').split(maxsplit=2)
    if len(args) != 3 or not args[1].lstrip('#').isdecimal() or not 8<=len(args[2])<=1200:
        bot.send_message(message.chat.id, 'Формат: <code>/unlinkid #12 причина и ссылка на доказательство</code>', parse_mode='HTML')
        return
    ok = identity_store.unlink(int(args[1].lstrip('#')), message.from_user.id, args[2])
    bot.send_message(message.chat.id, '[[icon:ok]] Привязка ID снята.' if ok else '[[icon:warning]] Привязки нет.')


@bot.message_handler(commands=["privacy"])
@community.serialized
def privacy_command(message):
    if not require_private_message(message):
        return
    leave_compose_mode(message.from_user.id, message.chat.id)
    bot.send_message(message.chat.id, texts.PRIVACY, parse_mode="HTML", reply_markup=build_main_menu(message.from_user.id))


@bot.message_handler(commands=["appeal"])
@community.serialized
def appeal_command(message):
    if not require_private_message(message):
        return
    leave_compose_mode(message.from_user.id, message.chat.id)
    set_state(message.from_user.id, STATE_WAITING_FOR_APPEAL)
    bot.send_message(
        message.chat.id,
        "[[icon:appeal]] Пришли номер опубликованной жалобы и причину пересмотра. "
        "Например: <code>#12 В записи ошибочно указан мой аккаунт.</code>\n\n"
        "Для запроса удаления личных данных напиши: "
        "<code>#0 Прошу удалить мои личные данные из бота.</code>",
        parse_mode="HTML",
        reply_markup=build_cancel_menu(),
    )


@bot.message_handler(commands=["support", "helpdesk", "tickets", "broadcast", "help", "notifications", "unsubscribe", "cancel", "seticon"])
@community.serialized
def community_command(message):
    if not require_private_message(message):
        return
    if message.from_user.id in admin_chat_sessions:
        finish_chat(message.from_user.id, message.chat.id)
    community.handle_command(message)


@bot.message_handler(func=community.wants_message, content_types=CONTENT_TYPES)
@community.serialized
def community_message(message):
    if community.wants_message(message):
        if message.from_user.id in admin_chat_sessions:
            finish_chat(message.from_user.id, message.chat.id)
        community.handle_message(message)


@bot.my_chat_member_handler()
def chat_membership_changed(update):
    if getattr(update.chat, "type", None) != "private":
        return
    status = getattr(update.new_chat_member, "status", "")
    community_store.mark_blocked(update.chat.id, status in {"kicked", "left"})


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == STATE_WAITING_FOR_APPEAL, content_types=["text"])
@state_handler(STATE_WAITING_FOR_APPEAL)
def handle_appeal_text(message):
    if not require_private_message(message):
        return
    raw = (message.text or "").strip()
    matched = re.fullmatch(r"#?(\d{1,9})\s+(.{10,1200})", raw, flags=re.DOTALL)
    if not matched:
        bot.send_message(message.chat.id, "[[icon:warning]] Укажи номер жалобы и причину (от 10 до 1200 символов).", reply_markup=build_cancel_menu())
        return
    complaint_id, reason = int(matched[1]), matched[2].strip()
    if not reason or len(reason) < 10:
        bot.send_message(message.chat.id, "[[icon:warning]] Причина слишком короткая.", reply_markup=build_cancel_menu())
        return
    if complaint_id != 0:
        complaint = get_complaint_by_id(complaint_id)
        if not complaint or complaint["status"] not in VISIBLE_STATUSES:
            bot.send_message(message.chat.id, "[[icon:warning]] Публичная жалоба с таким номером не найдена.", reply_markup=build_cancel_menu())
            return
    sender = f"@{message.from_user.username}" if message.from_user.username else "без username"
    admin_text = (
        f"[[icon:appeal]] <b>Запрос на пересмотр / удаление данных</b>\n"
        f"Жалоба: <code>#{complaint_id}</code>\n"
        f"От: {escape_html(sender)} | <code>{message.from_user.id}</code>\n"
        f"Текст: {escape_html(reason)}"
    )
    try:
        bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML")
    except Exception as exc:
        logger.error("Не удалось переслать запрос на пересмотр (%s)", type(exc).__name__)
        bot.send_message(message.chat.id, "[[icon:warning]] Не удалось доставить запрос. Попробуй позже.")
        return
    clear_state(message.from_user.id)
    bot.send_message(message.chat.id, "[[icon:ok]] Запрос отправлен администратору на рассмотрение.", reply_markup=build_main_menu(message.from_user.id))


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == STATE_WAITING_FOR_CHECK_TARGET, content_types=["text"])
@state_handler(STATE_WAITING_FOR_CHECK_TARGET)
def handle_check_target(message):
    if not require_subscription_for_message(message):
        return
    target = message.text.strip()
    if not valid_search_reference(target):
        bot.send_message(
            message.chat.id,
            "[[icon:warning]] Укажи <code>@username</code> или числовой Telegram ID.",
            parse_mode="HTML",
            reply_markup=build_cancel_menu(),
        )
        return
    if not allow_check(message.from_user.id):
        bot.send_message(message.chat.id, "[[icon:wait]] Между проверками должно пройти 5 секунд.")
        return
    clear_state(message.from_user.id)
    bot.send_message(
        message.chat.id,
        format_check_text(target),
        parse_mode="HTML",
        reply_markup=build_check_result_menu(),
    )


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == STATE_WAITING_FOR_REPORT_TARGET,
                     content_types=['text','photo','video','document','audio','voice','sticker','animation','video_note','users_shared'])
@state_handler(STATE_WAITING_FOR_REPORT_TARGET)
def handle_report_target(message):
    if not require_subscription_for_message(message):
        return
    state = get_state(message.from_user.id)
    if (message.text or '').strip() == 'Отмена':
        leave_compose_mode(message.from_user.id, message.chat.id)
        show_main_menu(message.chat.id, message.from_user.id)
        return
    target = (message.text or '').strip()
    selection = None
    source = None
    if getattr(message, 'users_shared', None):
        selection = shared_user(message, state.get('request_id'))
        source = 'bot_users_shared'
        if not selection:
            bot.send_message(message.chat.id, 'Этот выбор уже не действует. Выбери аккаунт снова.')
            show_report_target_prompt(message.chat.id, message.from_user.id)
            return
        identity_store.observe(selection.user_id, getattr(selection,'username',None),
            source=source, checked_at=message_time(message), actor_id=message.from_user.id,
            event_key=f'shared:{message.chat.id}:{message.message_id}')
        selected_id = selection.user_id
        selection_event_key = f'shared:{message.chat.id}:{message.message_id}'
    else:
        origin = getattr(message,'forward_origin',None)
        selection = getattr(origin,'sender_user',None) if getattr(origin,'type',None)=='user' else None
        source = 'bot_forward'
        selected_id = getattr(selection,'id',None)
        selection_event_key = f'message:{message.chat.id}:{message.message_id}:forward' if selection else None
        if origin and not selection:
            bot.send_message(message.chat.id, 'Автор пересылки скрыт или это сообщение канала. '
                'Выбери личный аккаунт кнопкой ниже либо укажи его @username или ID.')
            return
        if selection:
            observe_message(identity_store,message)
    if selection:
        if not identity_store.claim_selection(message.from_user.id, state,
                selection_event_key, selected_id, source,
                getattr(getattr(message,'users_shared',None),'request_id',None)):
            bot.send_message(message.chat.id,'Этот выбор устарел или уже использован. Начни новую жалобу через меню.')
            return
        state['selected_id'] = selected_id
        state['selection_source'] = source
        state['selection_event_key'] = selection_event_key
        target = normalized_report_reference(getattr(selection,'username',None), selected_id)
    elif not valid_search_reference(target):
        bot.send_message(message.chat.id, 'Пришли @username или числовой Telegram ID, '
            'перешли сообщение либо выбери аккаунт кнопкой ниже.')
        return
    if state.get('clarify_id'):
        if not selection:
            bot.send_message(message.chat.id,'Для уточнения выбери аккаунт кнопкой или перешли сообщение с открытым автором.')
            return
        ok = identity_store.clarify(state['clarify_id'], message.from_user.id, state)
        clear_state(message.from_user.id)
        remove_account_keyboard(message.chat.id)
        bot.send_message(message.chat.id,'Уточнение передано на проверку. Прежняя связь не изменена.' if ok else
            'Уточнение не сохранено. Открой свою жалобу заново.', reply_markup=build_main_menu(message.from_user.id))
        return
    state['target'] = target.lower() if target.startswith('@') else str(int(target))
    state['state'] = STATE_WAITING_FOR_REPORT_NOTE
    state.pop('request_id',None)
    identity_store.save_form(message.from_user.id, state)
    remove_account_keyboard(message.chat.id)
    bot.send_message(message.chat.id,
        '[[icon:report]] <b>Новая жалоба · 2 из 3</b>\n\n'
        'Расскажи, что произошло: о чём договорились, что ты сделал и что пошло не так.\n\n'
        'Аккаунт указан для проверки. Его выбор или введённый ID не доказывает вину. '
        'Описание может стать публичным после модерации. Не добавляй пароли и личные данные.',
        parse_mode='HTML', reply_markup=build_cancel_menu())


def normalized_report_reference(username, user_id):
    from identity_db import normalized_username
    return normalized_username(username) or str(user_id)


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == STATE_WAITING_FOR_REPORT_NOTE, content_types=["text"])
@state_handler(STATE_WAITING_FOR_REPORT_NOTE)
def handle_report_note(message):
    if not require_subscription_for_message(message):
        return
    note = message.text.strip()
    if len(note) < 5:
        bot.send_message(message.chat.id, "[[icon:warning]] Слишком коротко. Напиши хотя бы пару слов о ситуации.", reply_markup=build_cancel_menu())
        return
    if len(note) > MAX_NOTE_LENGTH:
        bot.send_message(message.chat.id, f"[[icon:warning]] Максимум {MAX_NOTE_LENGTH} символов. Сократи описание.", reply_markup=build_cancel_menu())
        return
    state = get_state(message.from_user.id)
    state["note"] = note
    state["state"] = STATE_WAITING_FOR_REPORT_EVIDENCE
    state["evidence"] = []
    identity_store.save_form(message.from_user.id, state)
    bot.send_message(
        message.chat.id,
        "[[icon:attachment]] <b>Новая жалоба · 3 из 3</b>\n\n"
        "Добавь доказательства: скриншоты переписки, файл, видео, голосовое или текст. "
        "Материалы увидит только модератор.\n\n"
        "Можно прикрепить до 10 сообщений. Когда всё готово, нажми «Отправить жалобу».",
        parse_mode="HTML",
        reply_markup=build_report_evidence_menu(),
    )


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == STATE_WAITING_FOR_REPORT_EVIDENCE, content_types=["text", "photo", "document", "video", "audio", "voice"])
@state_handler(STATE_WAITING_FOR_REPORT_EVIDENCE)
def handle_report_evidence(message):
    if not require_subscription_for_message(message):
        return
    with STATE_LOCK:
        state = get_state(message.from_user.id)
        if state.get('state') != STATE_WAITING_FOR_REPORT_EVIDENCE:
            return
        evidence = state.setdefault("evidence", [])

        if len(evidence) >= MAX_EVIDENCE_ITEMS:
            bot.send_message(message.chat.id, f"[[icon:attachment]] Максимум {MAX_EVIDENCE_ITEMS} приложений. Отправь жалобу кнопкой ниже.", reply_markup=build_report_evidence_menu())
            return

        if message.content_type == "text":
            text_value = message.text.strip()
            if not text_value:
                return
            if len(text_value) > MAX_EVIDENCE_TEXT_LENGTH:
                bot.send_message(message.chat.id, f"[[icon:warning]] Текст доказательства ограничен {MAX_EVIDENCE_TEXT_LENGTH} символами.")
                return
            evidence.append({"type": "text", "data": text_value})
        elif message.content_type == "photo":
            evidence.append({"type": "photo", "data": message.photo[-1].file_id})
        elif message.content_type == "document":
            evidence.append({"type": "document", "data": message.document.file_id})
        elif message.content_type == "video":
            evidence.append({"type": "video", "data": message.video.file_id})
        elif message.content_type == "audio":
            evidence.append({"type": "audio", "data": message.audio.file_id})
        elif message.content_type == "voice":
            evidence.append({"type": "voice", "data": message.voice.file_id})
        else:
            return

        identity_store.save_form(message.from_user.id, state)
        if not state.get("evidence_ready_notified"):
            state["evidence_ready_notified"] = True
            bot.send_message(
                message.chat.id,
                "[[icon:ok]] Доказательства получил. Можешь отправить ещё или сразу нажать кнопку ниже.",
                reply_markup=build_report_evidence_menu(),
            )


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == STATE_WAITING_FOR_ADMIN_ADD_TARGETS, content_types=["text"])
@state_handler(STATE_WAITING_FOR_ADMIN_ADD_TARGETS)
def handle_admin_add_targets(message):
    if not is_admin(message.from_user.id):
        clear_state(message.from_user.id)
        return
    if not require_private_message(message):
        return
    raw_targets = [part.strip() for part in message.text.split() if part.strip()]
    if len(raw_targets) > MAX_MANUAL_TARGETS:
        bot.send_message(message.chat.id, f"[[icon:warning]] Не больше {MAX_MANUAL_TARGETS} ников за раз.")
        return
    targets = []
    for raw in raw_targets:
        if raw.startswith("@") and valid_target(raw):
            targets.append(normalize_target(raw))
    if not targets:
        bot.send_message(
            message.chat.id,
            "[[icon:warning]] Не нашёл ни одного ника в формате <code>@username</code>. Попробуй ещё раз.",
            parse_mode="HTML",
            reply_markup=build_cancel_menu(),
        )
        return
    set_state(message.from_user.id, STATE_WAITING_FOR_ADMIN_ADD_NOTE, targets=targets)
    bot.send_message(
        message.chat.id,
        "[[icon:report]] Теперь пришли примечание для этих записей. Оно сохранится в базе.",
        reply_markup=build_cancel_menu(),
    )


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == STATE_WAITING_FOR_ADMIN_FIND, content_types=["text"])
@state_handler(STATE_WAITING_FOR_ADMIN_FIND)
def handle_admin_find(message):
    if not is_admin(message.from_user.id) or not require_private_message(message):
        return
    raw = (message.text or '').strip().lstrip('#')
    if not raw.isdecimal() or not 1 <= int(raw) <= 1_000_000_000:
        bot.send_message(message.chat.id, "[[icon:warning]] Пришли номер жалобы, например <code>12</code>.", parse_mode='HTML', reply_markup=build_cancel_menu())
        return
    complaint_id = int(raw)
    clear_state(message.from_user.id)
    show_review_detail(message.chat.id, complaint_id, 0)


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == STATE_WAITING_FOR_ADMIN_ADD_NOTE, content_types=["text"])
@state_handler(STATE_WAITING_FOR_ADMIN_ADD_NOTE)
def handle_admin_add_note(message):
    if not is_admin(message.from_user.id):
        clear_state(message.from_user.id)
        return
    if not require_private_message(message):
        return
    note = message.text.strip() or "Добавлено администратором"
    if len(note) > MAX_NOTE_LENGTH:
        bot.send_message(message.chat.id, f"[[icon:warning]] Максимальная длина — {MAX_NOTE_LENGTH} символов.")
        return
    state = get_state(message.from_user.id)
    targets = state.get("targets", [])
    added = insert_manual_scammers(targets, note)
    clear_state(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "[[icon:ok]] Записи добавлены в базу.\n\n"
        f"[[icon:user]] Ники: {', '.join(added)}\n"
        f"[[icon:document]] Статус: <b>{escape_html(status_label(MANUAL_ADD_STATUS))}</b>",
        parse_mode="HTML",
        reply_markup=build_admin_panel(),
    )


@bot.callback_query_handler(func=lambda c: True)
@community.serialized
def handle_callbacks(call):
    if not call.message or not private_chat(call.message.chat):
        bot.answer_callback_query(call.id, "Открой личный чат с ботом.", show_alert=True)
        return
    if call.message.chat.id != call.from_user.id:
        bot.answer_callback_query(call.id, "Недоступно в этом чате.", show_alert=True)
        return
    data = call.data or ""
    if (data.startswith(("admin:", "bc:", "sup:list:adm:"))
            and not is_admin(call.from_user.id)):
        bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
        return
    remember_user_seen(call.from_user, getattr(call.message.chat, "id", None), source='bot_callback', event_key=f'callback:{call.id}')
    if data=='menu:report' and get_state(call.from_user.id).get('_start_event')==f'callback:{call.id}':
        bot.answer_callback_query(call.id,'Форма уже открыта.')
        return
    if data.startswith(("menu:", "admin:", "sup:", "bc:", "notify:")):
        leave_compose_mode(call.from_user.id, call.message.chat.id)
    if community.handle_callback(call):
        return
    try:
        if data == 'menu:clarify':
            if not ensure_subscription_for_callback(call):return
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id,'Чтобы уточнить аккаунт в своей прежней жалобе, '
                'отправь /clarify #номер. Затем выбери аккаунт или перешли его сообщение. '
                'Уточнение проверит модератор.',reply_markup=build_cancel_menu())
            return
        if data.startswith('clarify:start:'):
            if not ensure_subscription_for_callback(call):return
            bot.answer_callback_query(call.id)
            begin_clarification(call.message.chat.id,call.from_user.id,int(data.split(':')[2]))
            return
        if data == "subscription:recheck":
            if check_subscription(call.from_user.id):
                bot.answer_callback_query(call.id, "Готово [[icon:ok]]")
                show_main_menu(call.message.chat.id, call.from_user.id, message_id=call.message.message_id)
            else:
                bot.answer_callback_query(call.id, "Похоже, подписка пока не подтверждена.", show_alert=True)
            return

        if data == "flow:cancel":
            leave_compose_mode(call.from_user.id, call.message.chat.id)
            bot.answer_callback_query(call.id, "Отменено.")
            show_main_menu(call.message.chat.id, call.from_user.id, message_id=call.message.message_id)
            return

        if data == "menu:main":
            if not ensure_subscription_for_callback(call):
                return
            clear_state(call.from_user.id)
            bot.answer_callback_query(call.id)
            show_main_menu(call.message.chat.id, call.from_user.id, message_id=call.message.message_id)
            return

        if data == "menu:check":
            if not ensure_subscription_for_callback(call):
                return
            set_state(call.from_user.id, STATE_WAITING_FOR_CHECK_TARGET)
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                texts.CHECK,
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=build_cancel_menu(),
            )
            return

        if data == "menu:report":
            if not ensure_subscription_for_callback(call):
                return
            set_state(call.from_user.id, STATE_WAITING_FOR_REPORT_TARGET, _start_event=f'callback:{call.id}', username=f"@{call.from_user.username}" if call.from_user.username else None)
            bot.answer_callback_query(call.id)
            show_report_target_prompt(call.message.chat.id, call.from_user.id)
            return

        if data == "menu:appeal":
            bot.answer_callback_query(call.id)
            set_state(call.from_user.id, STATE_WAITING_FOR_APPEAL)
            bot.edit_message_text(
                "[[icon:appeal]] Пришли номер жалобы и причину пересмотра. Пример: "
                "<code>#12 В записи ошибочно указан мой аккаунт.</code>\n\n"
                "Запрос на удаление личных данных: "
                "<code>#0 Прошу удалить мои личные данные из бота.</code>",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=build_cancel_menu(),
            )
            return

        if data.startswith("menu:list:"):
            if not ensure_subscription_for_callback(call):
                return
            page = int(data.split(":")[2])
            bot.answer_callback_query(call.id)
            show_list_page(call.message.chat.id, page, message_id=call.message.message_id, user_id=call.from_user.id)
            return

        if data.startswith("list:detail:"):
            if not ensure_subscription_for_callback(call):
                return
            _, _, cid, page = data.split(":")
            bot.answer_callback_query(call.id)
            show_public_detail(call.message.chat.id, int(cid), int(page), message_id=call.message.message_id, user_id=call.from_user.id)
            return

        if data == "report:submit":
            if not ensure_subscription_for_callback(call):
                return
            bot.answer_callback_query(call.id)
            finalize_complaint(call.from_user.id, call.message.chat.id)
            return

        if data == "report:clear_evidence":
            if not ensure_subscription_for_callback(call):
                return
            state = get_state(call.from_user.id)
            if state.get("state") != STATE_WAITING_FOR_REPORT_EVIDENCE:
                bot.answer_callback_query(call.id, "Сейчас нечего очищать.")
                return
            state["evidence"] = []
            state["evidence_ready_notified"] = False
            identity_store.save_form(call.from_user.id, state)
            bot.answer_callback_query(call.id, "Вложения очищены.")
            bot.send_message(
                call.message.chat.id,
                "[[icon:delete]] Я убрал доказательства из жалобы. Скинь их заново, когда будешь готов.",
                reply_markup=build_report_wait_evidence_menu(),
            )
            return

        if data == "admin:panel":
            if not is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
                return
            bot.answer_callback_query(call.id)
            clear_state(call.from_user.id)
            show_admin_panel(call.message.chat.id, message_id=call.message.message_id)
            return

        if data == "admin:stats":
            if not is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
                return
            bot.answer_callback_query(call.id)
            show_admin_stats(call.message.chat.id, message_id=call.message.message_id)
            return

        if data.startswith('admin:identity:'):
            if not is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, 'Нет доступа.', show_alert=True)
                return
            parts = data.split(':')
            action = parts[2]
            if action == 'clarify' and len(parts)==4:
                bot.answer_callback_query(call.id)
                begin_clarification(call.message.chat.id,call.from_user.id,int(parts[3]))
                return
            if action == 'filter' and len(parts)==5:
                bot.answer_callback_query(call.id)
                show_identity_filter(call.message.chat.id,parts[3],int(parts[4]))
                return
            if action == 'audit' and len(parts)==4:
                bot.answer_callback_query(call.id)
                show_identity_audit(call.message.chat.id,int(parts[3]))
                return
            if action == 'accounts'  and len(parts)==4:
                bot.answer_callback_query(call.id)
                show_identity_history(call.message.chat.id,page=int(parts[3]))
                return
            if action == 'history' and len(parts) in (3,4,5):
                bot.answer_callback_query(call.id)
                show_identity_history(call.message.chat.id, int(parts[3]) if len(parts)>=4 else None,
                                      int(parts[4]) if len(parts)==5 else 0)
                return
            if action == 'verify' and len(parts)==5:
                complaint_id, candidate_id = int(parts[3]), int(parts[4])
                bot.answer_callback_query(call.id)
                if any(c['user_id']==candidate_id for c in identity_store.candidates(complaint_id)):
                    request_identity_review(call.message.chat.id,complaint_id,candidate_id)
                else:
                    bot.send_message(call.message.chat.id,'Кандидат уже обработан. Открой запись заново.')
                return
            if action == 'list' and len(parts) == 4:
                bot.answer_callback_query(call.id)
                show_identity_page(call.message.chat.id, int(parts[3]),
                                   message_id=call.message.message_id)
                return
            if action == 'scan' and len(parts) == 3:
                bot.answer_callback_query(call.id,
                    'Сопоставляю сохранённые наблюдения.')
                start_manual_identity_scan(call.message.chat.id)
                return
            if action == 'pause' and len(parts) == 3:
                identity_worker.pause()
                bot.answer_callback_query(call.id, 'Сверка остановится после текущего запроса.')
                show_identity_page(call.message.chat.id, message_id=call.message.message_id)
                return
            if action in ('detail', 'approve', 'reject', 'reset', 'unlink') and len(parts) == 5:
                complaint_id, page = int(parts[3]), int(parts[4])
                if action == 'detail':
                    bot.answer_callback_query(call.id)
                    show_identity_detail(call.message.chat.id, complaint_id, page,
                                         message_id=call.message.message_id)
                    return
                if action == 'approve':
                    bot.answer_callback_query(call.id)
                    row = get_complaint_by_id(complaint_id)
                    request_identity_review(call.message.chat.id,complaint_id,
                                            row.get('candidate_user_id') if row else None)
                    return
                else:
                    row = get_complaint_by_id(complaint_id)
                    if not row:
                        bot.answer_callback_query(call.id,'Жалоба не найдена.')
                        return
                    set_state(call.from_user.id, STATE_IDENTITY_EVIDENCE, action=action,
                        complaint_id=complaint_id, expected_user_id=row.get('target_user_id'))
                    bot.answer_callback_query(call.id)
                    bot.send_message(call.message.chat.id,'Укажи причину решения и ссылку или номер доказательства. '
                        'Изменение будет записано в журнал.',reply_markup=build_cancel_menu())
                    return
                bot.answer_callback_query(call.id, response, show_alert=not ok)
                if ok and action == 'reset':
                    complaint = get_complaint_by_id(complaint_id)
                    if complaint:
                        if valid_target(complaint['target']):
                            start_manual_identity_scan(call.message.chat.id,
                                                       normalize_target(complaint['target']))
                        else:
                            bot.send_message(call.message.chat.id, '[[icon:warning]] У старого ника некорректный формат.')
                show_identity_detail(call.message.chat.id, complaint_id, page,
                                     message_id=call.message.message_id)
                return
            bot.answer_callback_query(call.id, 'Некорректная кнопка.')
            return

        if data == "admin:add:start":
            if not is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
                return
            set_state(call.from_user.id, STATE_WAITING_FOR_ADMIN_ADD_TARGETS)
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "[[icon:add]] Пришли один или несколько ников через пробел в формате <code>@user1 @user2</code>",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=build_cancel_menu(),
            )
            return

        if data == "admin:find":
            if not is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
                return
            set_state(call.from_user.id, STATE_WAITING_FOR_ADMIN_FIND)
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "[[icon:search]] Пришли ID жалобы, например <code>12</code>.",
                call.message.chat.id, call.message.message_id,
                parse_mode='HTML', reply_markup=build_cancel_menu(),
            )
            return

        if data.startswith("admin:review:"):
            if not is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
                return
            page = int(data.split(":")[2])
            bot.answer_callback_query(call.id)
            show_review_page(call.message.chat.id, page, message_id=call.message.message_id)
            return

        if data.startswith("admin:reviewdetail:"):
            if not is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
                return
            _, _, cid, page = data.split(":")
            bot.answer_callback_query(call.id)
            show_review_detail(call.message.chat.id, int(cid), int(page), message_id=call.message.message_id)
            return

        if data.startswith("admin:approve:"):
            if not is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
                return
            _, _, cid, page = data.split(":")
            complaint_id = int(cid)
            page_num = int(page)
            if not change_complaint_status(complaint_id, "accepted"):
                bot.answer_callback_query(call.id, "Жалоба уже обработана или отсутствует.", show_alert=True)
                return
            complaint = get_complaint_by_id(complaint_id)
            bot.answer_callback_query(call.id, "Жалоба принята.")
            show_review_detail(call.message.chat.id, complaint_id, page_num, message_id=call.message.message_id)
            if complaint and complaint["from_id"] and complaint["from_id"] != ADMIN_ID:
                try:
                    bot.send_message(
                        complaint["from_id"],
                        f"[[icon:ok]] Жалобу #{complaint_id} по нику {complaint['target']} приняли.",
                        reply_markup=build_main_menu(complaint["from_id"]),
                    )
                except Exception:
                    pass
            return

        if data.startswith("admin:decline:"):
            if not is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
                return
            _, _, cid, page = data.split(":")
            complaint_id = int(cid)
            page_num = int(page)
            if not change_complaint_status(complaint_id, "declined"):
                bot.answer_callback_query(call.id, "Жалоба уже обработана или отсутствует.", show_alert=True)
                return
            complaint = get_complaint_by_id(complaint_id)
            bot.answer_callback_query(call.id, "Жалоба отклонена.")
            show_review_page(call.message.chat.id, page_num, message_id=call.message.message_id)
            if complaint and complaint["from_id"] and complaint["from_id"] != ADMIN_ID:
                try:
                    bot.send_message(
                        complaint["from_id"],
                        f"[[icon:cancel]] Жалобу #{complaint_id} по нику {complaint['target']} не приняли.",
                        reply_markup=build_main_menu(complaint["from_id"]),
                    )
                except Exception:
                    pass
            return

        if data.startswith(("admin:hide:", "admin:reopen:")):
            if not is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
                return
            _, operation, cid, page = data.split(":")
            changed = reopen_complaint(int(cid), hide=operation == 'hide')
            if not changed:
                bot.answer_callback_query(call.id, "Статус уже изменён или жалоба не найдена.", show_alert=True)
                return
            bot.answer_callback_query(call.id, "Жалоба снята с публикации." if operation == 'hide' else "Жалоба возвращена на проверку.")
            show_review_detail(call.message.chat.id, int(cid), int(page), message_id=call.message.message_id)
            return

        if data.startswith("admin:evidence:"):
            if not is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
                return
            _, _, cid, page = data.split(":")
            complaint = get_complaint_by_id(int(cid))
            if not complaint:
                bot.answer_callback_query(call.id, "Жалоба не найдена.")
                return
            bot.answer_callback_query(call.id)
            if not complaint["evidence"]:
                bot.send_message(call.message.chat.id, "[[icon:attachment]] У этой жалобы нет доказательств.")
                return
            bot.send_message(
                call.message.chat.id,
                f"[[icon:attachment]] Доказательства по жалобе #{complaint['id']} ({len(complaint['evidence'])} шт.)"
            )
            for ev in complaint["evidence"]:
                try:
                    if ev["type"] == "photo":
                        bot.send_photo(call.message.chat.id, ev["data"])
                    elif ev["type"] == "document":
                        bot.send_document(call.message.chat.id, ev["data"])
                    elif ev["type"] == "video":
                        bot.send_video(call.message.chat.id, ev["data"])
                    elif ev["type"] == "audio":
                        bot.send_audio(call.message.chat.id, ev["data"])
                    elif ev["type"] == "voice":
                        bot.send_voice(call.message.chat.id, ev["data"])
                    elif ev["type"] == "text":
                        bot.send_message(call.message.chat.id, f"[[icon:support]] Текст:\n{escape_html(ev['data'])}", parse_mode="HTML")
                except Exception:
                    pass
            return

        if data.startswith("admin:chat:"):
            if not is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
                return
            _, _, cid, page = data.split(":")
            bot.answer_callback_query(call.id)
            start_direct_chat(call.from_user.id, int(cid), message_id=call.message.message_id, page=int(page))
            return

        if data == "chat:end":
            finish_chat(call.from_user.id, call.message.chat.id, callback_id=call.id)
            return

        bot.answer_callback_query(call.id)
    except Exception as exc:
        logger.error("Ошибка при обработке callback (%s)", type(exc).__name__)
        try:
            bot.answer_callback_query(call.id, "[[icon:cancel]] Ошибка при обработке кнопки.", show_alert=True)
        except Exception:
            pass


@bot.message_handler(func=lambda m: m.from_user.id in admin_chat_sessions, content_types=['text', 'photo', 'document', 'video', 'audio', 'voice', 'sticker'])
@community.serialized
def relay_chat_message(message):
    if not require_private_message(message):
        return
    remember_user_seen(message.from_user, getattr(message.chat, "id", None), observed_at=message_time(message), event_key=f'message:{message.chat.id}:{message.message_id}')
    with SESSION_LOCK:
        partner = admin_chat_sessions.get(message.from_user.id)
    if not partner or get_state(message.from_user.id):
        return
    try:
        bot.copy_message(partner, message.chat.id, message.message_id)
    except Exception:
        bot.send_message(message.chat.id, "[[icon:warning]] Сообщение не отправилось. Попробуй ещё раз.")


@bot.message_handler(func=lambda m: True, content_types=['text'])
@community.serialized
def fallback_text_handler(message):
    if get_state(message.from_user.id):
        return
    if not require_subscription_for_message(message):
        return
    value = (message.text or "").strip()
    if valid_search_reference(value) and (value.startswith("@") or value.isdecimal()):
        if not allow_check(message.from_user.id):
            bot.send_message(message.chat.id, "Подожди 5 секунд перед следующей проверкой.")
            return
        bot.send_message(message.chat.id, format_check_text(value), parse_mode="HTML", reply_markup=build_check_result_menu())
        return
    show_main_menu(message.chat.id, message.from_user.id,
                   text="Выбери действие ниже. Для проверки можно просто отправить @username или Telegram ID.")


def main() -> None:
    with InstanceLock(DB_PATH):
        run_bot()


def remove_webhook_with_retry():
    """Retry an idempotent startup request after transient transport failures."""
    for attempt in range(3):
        try:
            return bot.remove_webhook()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if attempt == 2:
                raise
            logger.warning('Telegram startup transport retry %s/2 (%s)', attempt + 1, type(exc).__name__)
            time.sleep(attempt + 1)


def run_bot() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("LamaScambaseBot %s starting (polling)", BUILD_VERSION)
    if not identity_resolver.enabled:
        logger.warning('MTProto ID resolution disabled: TELEGRAM_API_ID/HASH not configured')
    remove_webhook_with_retry()
    install_identity_observer()
    community.start()
    logger.info("Hybrid Bot API observation enabled; remote backlog scanning stopped")
    try:
        bot.set_my_commands([types.BotCommand(command, description) for command, description in [
            ("start", "Главное меню"), ("check", "Проверить аккаунт"), ("myid", "Мой Telegram ID"), ("report", "Подать жалобу"),
            ("list", "База опубликованных жалоб"), ("support", "Написать в поддержку"),
            ("clarify", "Уточнить аккаунт своей жалобы"),
            ("tickets", "Мои обращения"), ("notifications", "Настройки уведомлений"),
            ("privacy", "Твои данные"), ("help", "Как пользоваться ботом"), ("cancel", "Отменить ввод"),
        ]])
    except Exception as exc:
        logger.warning("Cannot update command menu (%s)", type(exc).__name__)
    try:
        bot.infinity_polling(skip_pending=False, timeout=20, long_polling_timeout=20,
                             allowed_updates=["message", "callback_query", "my_chat_member"])
    finally:
        identity_worker.stop()
        community.stop()


if __name__ == "__main__":
    main()
