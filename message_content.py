"""Persist Telegram-native content without HTML conversion or UTF-16 offset changes."""
from __future__ import annotations
import json
from urllib.parse import urlsplit

MEDIA = ("photo", "video", "animation", "document", "audio", "voice", "sticker", "video_note")
CONTENT_TYPES = ["text", *MEDIA]
PUBLIC_ROUTES = {
    "menu:main", "menu:check", "menu:report", "menu:list:0", "menu:appeal",
    "menu:support", "menu:my_tickets", "menu:help", "menu:privacy", "menu:notifications",
}


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _entities(items, text):
    result = []
    maximum = utf16_length(text)
    boundaries = {0}
    offset_units = 0
    for char in text:
        offset_units += utf16_length(char)
        boundaries.add(offset_units)
    for item in items or []:
        value = item.to_dict() if hasattr(item, "to_dict") else dict(item)
        value = json.loads(json.dumps(value, ensure_ascii=False))
        offset, length = value.get("offset"), value.get("length")
        if (type(offset) is not int or type(length) is not int or offset < 0 or length <= 0
                or offset + length > maximum or offset not in boundaries
                or offset + length not in boundaries):
            raise ValueError("Некорректные границы форматирования текста.")
        if value.get("type") == "custom_emoji" and not str(value.get("custom_emoji_id", "")).isdigit():
            raise ValueError("Telegram не передал ID премиум-эмодзи.")
        result.append(value)
    return result


def capture_message(message, *, allow_album=False) -> dict:
    kind = getattr(message, "content_type", "text")
    if kind not in CONTENT_TYPES:
        raise ValueError("Пришли текст, фото, видео, файл, аудио, голосовое или стикер.")
    if getattr(message, "media_group_id", None) and not allow_album:
        raise ValueError("Для одной рассылки пришли одно сообщение. Альбом сначала раздели на отдельные фото.")
    if kind == "text":
        text = getattr(message, "text", None) or ""
        if not text.strip() or utf16_length(text) > 4096:
            raise ValueError("Сообщение должно содержать от 1 до 4096 символов Telegram.")
        return {"kind": kind, "text": text, "entities": _entities(getattr(message, "entities", None), text)}
    item = getattr(message, kind, None)
    if kind == "photo":
        item = item[-1] if item else None
    file_id = getattr(item, "file_id", None)
    if not isinstance(file_id, str) or not file_id:
        raise ValueError("Telegram не передал файл. Отправь его ещё раз.")
    caption = getattr(message, "caption", None) or ""
    if utf16_length(caption) > 1024:
        raise ValueError("Подпись к файлу слишком длинная: максимум 1024 символа Telegram.")
    result = {"kind": kind, "file_id": file_id, "caption": caption,
              "caption_entities": _entities(getattr(message, "caption_entities", None), caption)}
    if kind in {"photo", "video", "animation"}:
        result["has_spoiler"] = bool(getattr(message, "has_media_spoiler", False))
        result["show_caption_above_media"] = bool(getattr(message, "show_caption_above_media", False))
    return result


class ContentMismatchError(RuntimeError):
    """Telegram accepted a message but did not return the requested formatting."""
    def __init__(self, message_id=None):
        super().__init__("Telegram did not preserve requested content or markup")
        self.message_id = message_id


def _entity_signature(entity):
    data = entity.to_dict() if hasattr(entity, "to_dict") else dict(entity)
    user = data.get("user") or {}
    if hasattr(user, "to_dict"):
        user = user.to_dict()
    return (data.get("type"), data.get("offset"), data.get("length"),
            data.get("url"), data.get("language"), str(data.get("custom_emoji_id") or ""),
            user.get("id"), data.get("unix_time"), data.get("date_time_format"))


def verify_content_result(message, payload, reply_markup=None):
    """Verify the Bot API acknowledgement; this cannot test rendering on a client."""
    mid = getattr(message, "message_id", None)
    if not isinstance(mid, int) or mid <= 0:
        raise RuntimeError("Telegram returned no message_id")
    kind = payload["kind"]
    if kind == "text" or kind not in {"sticker", "video_note"}:
        field = "text" if kind == "text" else "caption"
        entities_field = "entities" if kind == "text" else "caption_entities"
        if (getattr(message, field, None) or "") != payload.get(field, ""):
            raise ContentMismatchError(mid)
        actual = {_entity_signature(e) for e in getattr(message, entities_field, None) or []}
        expected = {_entity_signature(e) for e in payload.get(entities_field, [])}
        if not expected.issubset(actual):
            raise ContentMismatchError(mid)
    for wanted_key, actual_key in (("has_spoiler", "has_media_spoiler"),
                                   ("show_caption_above_media", "show_caption_above_media")):
        if wanted_key in payload and bool(payload[wanted_key]) != bool(getattr(message, actual_key, False)):
            raise ContentMismatchError(mid)
    if reply_markup is not None:
        expected = reply_markup.to_dict().get("inline_keyboard", [])
        actual_markup = getattr(message, "reply_markup", None)
        actual = actual_markup.to_dict().get("inline_keyboard", []) if actual_markup else []
        if len(expected) != len(actual):
            raise ContentMismatchError(mid)
        for wanted, received in zip(expected, actual):
            if len(wanted) != len(received):
                raise ContentMismatchError(mid)
            for wb, rb in zip(wanted, received):
                for key in ("text", "url", "callback_data", "style", "icon_custom_emoji_id"):
                    if key in wb and wb[key] != rb.get(key):
                        raise ContentMismatchError(mid)
    return message


def send_content(bot, types_module, chat_id: int, payload: dict, *, reply_markup=None):
    """No parse_mode, prefixes, escaping or source-message dependency."""
    kind = payload["kind"]
    params = {"reply_markup": reply_markup}
    entity_type = types_module.MessageEntity
    if kind == "text":
        params["entities"] = [entity_type.de_json(e) for e in payload.get("entities", [])]
        return verify_content_result(bot.send_message(chat_id, payload["text"], **params), payload, reply_markup)
    if kind not in MEDIA:
        raise ValueError("Unsupported stored message type")
    if kind not in {"sticker", "video_note"}:
        params["caption"] = payload.get("caption", "")
        params["caption_entities"] = [entity_type.de_json(e) for e in payload.get("caption_entities", [])]
    for field in ("has_spoiler", "show_caption_above_media"):
        if field in payload:
            params[field] = payload[field]
    return verify_content_result(getattr(bot, "send_" + kind)(chat_id, payload["file_id"], **params), payload, reply_markup)


def parse_buttons(text: str, icon_names) -> list[dict]:
    """One row per line: title | URL/public callback | color | icon role."""
    rows = [line.strip() for line in text.splitlines() if line.strip()]
    if len(rows) > 8:
        raise ValueError("Можно добавить не больше 8 кнопок.")
    parsed = []
    aliases = {"menu:list": "menu:list:0", "menu:tickets": "menu:my_tickets"}
    colors = {"blue":"primary", "green":"success", "red":"danger", "синий":"primary",
              "зелёный":"success", "зеленый":"success", "красный":"danger"}
    for line in rows:
        fields = [p.strip() for p in line.split("|")]
        if not 2 <= len(fields) <= 4:
            raise ValueError("Формат: Название | ссылка или menu:check | primary | search")
        title, target = fields[:2]
        if not title or len(title) > 60 or any(ord(c) < 32 for c in title):
            raise ValueError("Название кнопки: от 1 до 60 символов без переноса строки.")
        target = aliases.get(target, target)
        color = fields[2] if len(fields) >= 3 and fields[2] else "primary"
        color = colors.get(color.lower(), color.lower())
        if color not in {"primary", "success", "danger"}:
            raise ValueError("Цвет кнопки: primary (синий), success (зелёный), danger (красный).")
        icon = fields[3] if len(fields) >= 4 and fields[3] else ("search" if target == "menu:check" else "link")
        if icon not in icon_names:
            raise ValueError("Неизвестная иконка. Например: search, report, support, link, home.")
        result = {"text": title, "style": color, "icon": icon}
        if target in PUBLIC_ROUTES:
            result["callback_data"] = target
        else:
            try:
                url = urlsplit(target)
                valid = (url.scheme in {"https", "http"} and bool(url.hostname)
                         and not url.username and not url.password and url.port != 0)
            except ValueError:
                valid = False
            if not valid or len(target) > 2000 or any(c.isspace() or ord(c) < 32 for c in target):
                raise ValueError("Нужна полная http(s)-ссылка или разрешённый раздел menu:…; админские действия запрещены.")
            result["url"] = target
        parsed.append(result)
    return parsed


def content_excerpt(payload: dict, limit: int = 180) -> str:
    text = payload.get("text") or payload.get("caption") or ""
    labels = {"photo":"Фото", "video":"Видео", "animation":"Анимация", "document":"Файл",
              "audio":"Аудио", "voice":"Голосовое", "sticker":"Стикер", "video_note":"Видеосообщение"}
    if payload["kind"] != "text":
        text = labels.get(payload["kind"], "Вложение") + (": " + text if text else "")
    return text[:limit] + ("…" if len(text) > limit else "")
