"""One visual language for system messages; user content is never re-formatted.

Emoji IDs are obtained from Telegram's TgAndroidIcons set, never guessed.
Only [[icon:...]] tokens authored by the application are rendered here.
"""
from __future__ import annotations

import copy
import html
import json
import logging
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from typing import Any

PACK_NAME = "TgAndroidIcons"
TOKEN = re.compile(r"\[\[icon:([a-z_]+)\]\]")
ICON_CANDIDATES = {
    "search": ("🔎", "🔍"), "report": ("📝", "✏️", "✍️"),
    "list": ("📂", "📁", "📋"), "appeal": ("📩", "✉️", "📨"),
    "admin": ("🛠", "⚙️", "🔧"), "home": ("🏠", "🏡"),
    "back": ("⬅️", "🔙", "◀️"), "next": ("➡️", "▶️"),
    "cancel": ("❌", "✖️"), "ok": ("✅", "✔️", "☑️"),
    "warning": ("⚠️", "❗", "❕"), "info": ("ℹ️", "❕"),
    "lock": ("🔒", "🔐"), "id": ("🆔", "👤"),
    "refresh": ("🔄", "🔁", "🔃"), "pin": ("📌", "📍"),
    "document": ("📄", "🧾", "📃"), "calendar": ("📅", "🗓"),
    "help": ("📖", "📘", "❓"), "attachment": ("📎", "🖇"),
    "add": ("➕", "✚"), "stats": ("📊", "📈"),
    "users": ("👥", "👤"), "user": ("👤", "👥"),
    "support": ("💬", "🎧", "🗨"), "ticket": ("🎫", "🎟", "📩"),
    "broadcast": ("📢", "📣", "🔊"), "send": ("📤", "✉️", "📨"),
    "stop": ("🛑", "⛔", "❌"), "delete": ("🗑", "🧹", "❌"),
    "wait": ("⏳", "⌛", "🕐"), "link": ("🔗", "🌐"),
    "globe": ("🌐", "🌍"), "notify": ("🔔", "📢"),
    "mute": ("🔕", "🔇"), "preview": ("👁", "👀", "🔎"),
    "palette": ("🎨", "🖌"), "pause": ("⏸", "⏸️", "🛑"),
    "play": ("▶️", "⏯", "✅"), "empty": ("📭", "📪", "📂"),
}
LEGACY_ICONS = {
    "🔎":"search", "🔍":"search", "📝":"report", "📂":"list", "📁":"list",
    "📩":"appeal", "🛠":"admin", "⚙️":"admin", "🏠":"home", "👋":"home",
    "⬅️":"back", "➡️":"next", "❌":"cancel", "✅":"ok", "🟢":"ok",
    "⚠️":"warning", "❗":"warning", "ℹ️":"info", "🔒":"lock", "🔐":"lock",
    "🆔":"id", "🔄":"refresh", "🔁":"refresh", "📌":"pin", "📄":"document",
    "🧾":"document", "📅":"calendar", "📖":"help", "📘":"help", "📎":"attachment",
    "➕":"add", "🆕":"add", "📊":"stats", "👥":"users", "👤":"user",
    "💬":"support", "📢":"broadcast", "🛑":"stop", "🧹":"delete", "⏳":"wait",
    "🔗":"link", "🌐":"globe", "📭":"empty", "🧠":"info", "🎯":"user",
    "📦":"list", "🟡":"warning", "🔴":"warning", "🟠":"warning", "📮":"appeal",
    "🔔":"notify", "🗑️":"delete", "🎫":"ticket", "📤":"send", "👁️":"preview",
}


def plain_icon_text(text: str) -> str:
    return TOKEN.sub("", str(text)).strip()


def _normal_emoji(value: str) -> str:
    return value.replace("\ufe0f", "").replace("\ufe0e", "")


def default_style(callback: str) -> str | None:
    if any(callback.startswith(p) for p in (
        "admin:decline:", "admin:hide:", "admin:identity:reject:",
        "admin:identity:unlink:", "bc:cancel:", "sup:close:", "sup:close_yes:",
    )) or callback in {"flow:cancel", "chat:end", "report:clear_evidence", "notify:off"}:
        return "danger"
    if any(callback.startswith(p) for p in (
        "admin:approve:", "admin:identity:approve:", "admin:identity:accept:", "admin:identity:confirm:",
        "bc:send:", "bc:resume:", "sup:new", "sup:reply:", "sup:reopen:",
    )) or callback in {"report:submit", "subscription:recheck", "notify:on"}:
        return "success"
    if callback in {"menu:main", "admin:panel"} or callback.startswith("menu:list:"):
        return None
    return "primary"


class UI:
    def __init__(self, types_module, store, *, enabled: bool = True):
        self.types = types_module
        self.store = store
        self.enabled = enabled
        self.premium_rejected = False
        self.last_error = ""
        self.delivery_status = {"emoji": "not_checked", "buttons": "not_checked"}
        self.delivery_checked_at = ""
        self._pack_lock = threading.Lock()
        self.catalog: list[dict[str, str]] = []
        self.overrides: dict[str, str] = {}
        self.mapping: dict[str, dict[str, str]] = {}
        try:
            self.catalog = json.loads(store.setting("emoji_catalog", "[]"))
            self.overrides = json.loads(store.setting("emoji_overrides", "{}"))
        except (ValueError, TypeError):
            self.catalog, self.overrides = [], {}
        if not isinstance(self.catalog, list):
            self.catalog = []
        if not isinstance(self.overrides, dict):
            self.overrides = {}
        self.catalog = [
            {"id": str(r["id"]), "emoji": r["emoji"]}
            for r in self.catalog if isinstance(r, dict)
            and 1 <= len(str(r.get("id", ""))) <= 20
            and str(r.get("id", "")).isascii()
            and str(r.get("id", "")).isdigit()
            and 0 < int(r["id"]) < 2**64
            and isinstance(r.get("emoji"), str) and 0 < len(r["emoji"]) <= 64
        ]
        self.overrides = {k: v for k, v in self.overrides.items()
                          if k in ICON_CANDIDATES and isinstance(v, str)
                          and v.isascii() and v.isdigit()}
        self._map()

    def _map(self) -> None:
        valid = {str(row.get("id")): row for row in self.catalog
                 if str(row.get("id", "")).isdigit() and int(row["id"]) > 0 and isinstance(row.get("emoji"), str) and row["emoji"]}
        mapped = {}
        for role, candidates in ICON_CANDIDATES.items():
            override = self.overrides.get(role)
            if override in valid:
                mapped[role] = valid[override]
                continue
            for emoji in candidates:
                match = next((v for v in valid.values()
                              if _normal_emoji(v["emoji"]) == _normal_emoji(emoji)), None)
                if match:
                    mapped[role] = match
                    break
        self.mapping = mapped

    def load_pack(self, bot) -> int:
        with self._pack_lock:
            return self._load_pack(bot)

    def _load_pack(self, bot) -> int:
        """Authenticated getStickerSet. Unavailable network never invents an ID."""
        pack = bot.get_sticker_set(PACK_NAME)
        if getattr(pack, "name", PACK_NAME).lower() != PACK_NAME.lower():
            raise ValueError("Telegram returned another emoji set")
        if getattr(pack, "sticker_type", None) != "custom_emoji":
            raise ValueError("TgAndroidIcons is not a custom emoji set")
        catalog = []
        for sticker in pack.stickers:
            eid = str(getattr(sticker, "custom_emoji_id", "") or "")
            emoji = getattr(sticker, "emoji", None)
            if (eid.isascii() and eid.isdigit() and len(eid) <= 20 and 0 < int(eid) < 2**64
                    and isinstance(emoji, str) and 0 < len(emoji) <= 64):
                catalog.append({"id": eid, "emoji": emoji})
        if not catalog:
            raise ValueError("Telegram returned an empty custom emoji catalog")
        self.catalog = catalog
        self.store.set_setting("emoji_pack_loaded_at", datetime.now(timezone.utc).isoformat())
        self.store.set_setting("emoji_catalog", json.dumps(catalog, ensure_ascii=False))
        self._map()
        self.premium_rejected = False
        self.last_error = ""
        return len(catalog)

    def assign(self, role: str, emoji_id: str) -> None:
        if role not in ICON_CANDIDATES or not any(x["id"] == emoji_id for x in self.catalog):
            raise ValueError("Укажи название иконки и ID именно из загруженного TgAndroidIcons.")
        self.overrides[role] = emoji_id
        self.store.set_setting("emoji_overrides", json.dumps(self.overrides))
        self._map()

    def icon(self, role: str, *, force: bool = False) -> str:
        item = self.mapping.get(role)
        if not item or not self.enabled or (self.premium_rejected and not force):
            return ""
        return f'<tg-emoji emoji-id="{item["id"]}">{html.escape(item["emoji"])}</tg-emoji>'

    def render(self, text: str, *, is_html: bool = False, plain: bool = False) -> str:
        value = str(text) if is_html else html.escape(str(text), quote=False)
        return TOKEN.sub(lambda m: "" if plain else self.icon(m[1]), value)

    def button(self, text: str, callback_data: str | None = None, url: str | None = None,
               *, icon: str | None = None, style: str | None = "auto"):
        match = TOKEN.search(text)
        role = icon or (match[1] if match else None)
        label = TOKEN.sub("", text).strip()
        if not role:
            for glyph, key in sorted(LEGACY_ICONS.items(), key=lambda p: -len(p[0])):
                if label.startswith(glyph):
                    label, role = label[len(glyph):].strip(), key
                    break
        if not label or len(label) > 100:
            raise ValueError("Название кнопки должно содержать от 1 до 100 символов.")
        if bool(callback_data) == bool(url):
            raise ValueError("Button must contain exactly one of callback_data or url")
        if callback_data and len(callback_data.encode("utf-8")) > 64:
            raise ValueError("Callback exceeds Telegram's 64-byte limit")
        chosen_style = default_style(callback_data or "") if style == "auto" else style
        if chosen_style not in {None, "primary", "success", "danger"}:
            raise ValueError("Unsupported button color")
        item = self.mapping.get(role or "")
        kwargs: dict[str, Any] = {"callback_data": callback_data, "url": url}
        if chosen_style:
            kwargs["style"] = chosen_style
        if item and self.enabled and not self.premium_rejected:
            kwargs["icon_custom_emoji_id"] = item["id"]
        return self.types.InlineKeyboardButton(label, **kwargs)

    def keyboard(self, rows):
        markup = self.types.InlineKeyboardMarkup(row_width=2)
        for row in rows:
            if row:
                markup.add(*row)
        return markup

    def without_icons(self, markup):
        if markup is None:
            return None
        clone = copy.deepcopy(markup)
        for row in getattr(clone, "keyboard", []):
            for button in row:
                if isinstance(button, dict):
                    button.pop("icon_custom_emoji_id", None)
                else:
                    button.icon_custom_emoji_id = None
        return clone

    def observe_result(self, message, text, reply_markup=None):
        """Inspect Bot API acknowledgement, not the plain-text fallback glyph.

        Never resend an acknowledged message when Telegram drops formatting.
        These diagnostics describe API data, not how a particular client renders it.
        """
        if not getattr(message, 'message_id', None):
            return
        expected = Counter(re.findall(r'<tg-emoji\s+emoji-id="([0-9]+)"', text))
        if expected:
            actual = Counter()
            for entity in getattr(message, 'entities', None) or []:
                data = entity.to_dict() if hasattr(entity, 'to_dict') else (entity if isinstance(entity, dict) else vars(entity))
                if data.get('type') == 'custom_emoji':
                    actual[str(data.get('custom_emoji_id'))] += 1
            self.delivery_status['emoji'] = 'confirmed' if all(actual[k] >= n for k, n in expected.items()) else 'missing'
        if reply_markup is not None:
            data = (reply_markup.to_dict() if hasattr(reply_markup, 'to_dict')
                    else json.loads(reply_markup.to_json()))
            desired = data.get('inline_keyboard', [])
            significant = any('style' in b or 'icon_custom_emoji_id' in b for row in desired for b in row)
            if significant:
                received_markup = getattr(message, 'reply_markup', None)
                received = received_markup.to_dict().get('inline_keyboard', []) if received_markup else []
                same = len(desired) == len(received)
                if same:
                    for wanted, got in zip(desired, received):
                        if len(wanted) != len(got):
                            same = False
                            break
                        for wb, rb in zip(wanted, got):
                            if any(wb[k] != rb.get(k) for k in ('text', 'callback_data', 'url', 'style', 'icon_custom_emoji_id') if k in wb):
                                same = False
                self.delivery_status['buttons'] = 'confirmed' if same else 'missing'
        self.delivery_checked_at = datetime.now(timezone.utc).isoformat()

    def is_emoji_rejection(self, exc: Exception) -> bool:
        result = getattr(exc, "result_json", None)
        if not isinstance(result, dict):
            return False
        description = str(result.get("description", "")).lower()
        return str(result.get("error_code", "")) == "400" and any(
            word in description for word in ("custom emoji", "custom_emoji", "emoji_id", "premium_required")
        )


class UIBot:
    """Wrap system UI sends, not native user messages or broadcast payloads."""
    def __init__(self, raw, ui: UI):
        self.raw = raw
        self.ui = ui

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def __setattr__(self, name, value):
        if name in {"raw", "ui"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self.raw, name, value)

    def _call(self, method, text, args, kwargs):
        templated = bool(TOKEN.search(text))
        params = dict(kwargs)
        original_mode = params.get("parse_mode")
        if templated:
            if params.get("entities"):
                raise ValueError("UI tokens cannot be mixed with native entities")
            prepared = self.ui.render(text, is_html=original_mode == "HTML")
            params["parse_mode"] = "HTML"
        else:
            prepared = text
        try:
            result = method(prepared, *args, **params)
        except Exception as exc:
            if not self.ui.is_emoji_rejection(exc):
                raise
            self.ui.premium_rejected = True
            self.ui.delivery_status["emoji"] = "rejected"
            self.ui.last_error = "Telegram отклонил custom emoji. Проверь Premium у владельца бота."
            logging.getLogger("lamascambase.ui").warning("Custom emoji rejected; system UI uses text-only fallback")
            params["reply_markup"] = self.ui.without_icons(params.get("reply_markup"))
            if templated:
                prepared = self.ui.render(text, is_html=original_mode == "HTML", plain=True)
            prepared = re.sub(r"<tg-emoji\b[^>]*>.*?</tg-emoji>", "", prepared, flags=re.S)
            return method(prepared, *args, **params)
        try:
            self.ui.observe_result(result, prepared, params.get("reply_markup"))
        except Exception as exc:
            self.ui.delivery_status = {"emoji": "unreadable", "buttons": "unreadable"}
            logging.getLogger('lamascambase.ui').warning('Cannot inspect UI acknowledgement (%s)', type(exc).__name__)
        return result

    def send_message(self, chat_id, text, **kwargs):
        return self._call(lambda txt, **kw: self.raw.send_message(chat_id, txt, **kw), text, (), kwargs)

    def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        try:
            return self._call(lambda txt, **kw: self.raw.edit_message_text(txt, chat_id, message_id, **kw),
                              text, (), kwargs)
        except Exception as exc:
            result = getattr(exc, "result_json", None)
            if not isinstance(result, dict):
                raise
            description = str(result.get("description", "")).lower()
            if str(result.get("error_code", "")) == "400" and "message is not modified" in description:
                return None
            if str(result.get("error_code", "")) == "400" and any(phrase in description for phrase in (
                "there is no text in the message to edit", "there is no text in message to edit",
                "message to edit not found", "message can't be edited",
            )):
                return self.send_message(chat_id, text, **kwargs)
            raise

    def answer_callback_query(self, callback_query_id, text="", **kwargs):
        return self.raw.answer_callback_query(callback_query_id, plain_icon_text(text), **kwargs)
