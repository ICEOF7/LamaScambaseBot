"""One durable delivery worker. No paid broadcasts; no blind retries after timeouts."""
from __future__ import annotations

import logging
import math
import threading
import time

from message_content import send_content, ContentMismatchError

log = logging.getLogger("lamascambase.delivery")


def broadcast_markup(ui, buttons):
    rows = [[ui.button(**button)] for button in buttons]
    if not any(b.get("callback_data") == "menu:notifications" for b in buttons):
        rows.append([ui.button("Уведомления", callback_data="menu:notifications", icon="notify", style=None)])
    return ui.keyboard(rows)


def support_markup(ui, item, *, admin_id):
    tid = item["ticket_id"]
    label = "Сообщение пользователя" if item["recipient_id"] == admin_id else "Ответ поддержки"
    return ui.keyboard([
        [ui.button(f"{label} · обращение #{tid}", callback_data=f"sup:ticket:{tid}:0", icon="ticket")],
        [ui.button("Ответить", callback_data=f"sup:reply:{tid}", icon="support", style="success"),
         ui.button("Открыть обращение", callback_data=f"sup:ticket:{tid}:0", icon="list")],
    ])


def classify_error(exc: Exception) -> tuple[str, int]:
    """Return a non-sensitive code and Telegram's retry_after, never an exception URL."""
    if isinstance(exc, ContentMismatchError):
        return "formatting_not_preserved", 0
    raw = getattr(exc, "result_json", None)
    if not isinstance(raw, dict):
        return "transport_ambiguous", 0
    try:
        code = int(raw.get("error_code", 0))
    except (ValueError, TypeError):
        return "transport_ambiguous", 0
    description = str(raw.get("description", "")).lower()
    if code == 429:
        params = raw.get("parameters") or {}
        try:
            retry = max(1, min(86400, int(params.get("retry_after", 30))))
        except (ValueError, TypeError):
            retry = 30
        return "rate_limit", retry
    if code == 403 or (code == 400 and any(s in description for s in ("chat not found", "user is deactivated"))):
        return "recipient_unavailable", 0
    if code == 400:
        if any(w in description for w in ("custom emoji", "custom_emoji", "emoji_id", "premium")):
            return "emoji_not_allowed", 0
        return "content_rejected", 0
    if code == 401:
        return "invalid_token", 0
    return "transport_ambiguous", 0


class DeliveryWorker:
    def __init__(self, store, bot, types_module, ui, *, per_second=8.0, throttle=True):
        self.store, self.bot, self.types, self.ui = store, bot, types_module, ui
        self.per_second = max(1.0, min(20.0, float(per_second)))
        self.throttle = throttle
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = None
        self._start_lock = threading.Lock()
        self._completion = None
        self._global_last = 0.0
        self._chat_last: dict[int, float] = {}

    def start(self):
        with self._start_lock:
            if self.thread and self.thread.is_alive():
                return
            self.store.recover()
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run, name="community-delivery", daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def wake(self):
        self.wake_event.set()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                worked = self.run_once()
            except Exception as exc:
                log.error("Delivery iteration failed (%s)", type(exc).__name__)
                self.stop_event.wait(3)
                worked = False
            if not worked:
                self.wake_event.wait(timeout=1)
                self.wake_event.clear()

    def _pace(self, recipient):
        if not self.throttle:
            return
        now = time.monotonic()
        delay = max(0, self._global_last + 1/self.per_second - now,
                    self._chat_last.get(recipient, 0) + 1.1 - now)
        if delay:
            self.stop_event.wait(delay)
        self._global_last = time.monotonic()
        self._chat_last[recipient] = self._global_last
        if len(self._chat_last) > 2048:
            self._chat_last = {k:v for k,v in self._chat_last.items() if self._global_last-v < 60}

    def _flush_completion(self):
        if self._completion is None:
            return True
        is_support, item, status, details = self._completion
        try:
            if is_support:
                self.store.finish_support(item, status, **details)
            else:
                self.store.finish_broadcast(item, status, **details)
        except Exception as exc:
            log.error("Delivery result awaits database commit (%s)", type(exc).__name__)
            return False
        self._completion = None
        return True

    def _complete(self, is_support, item, status, **details):
        self._completion = (is_support, item, status, details)
        return self._flush_completion()

    def run_once(self):
        if self.stop_event.is_set():
            return False
        if self._completion is not None:
            return self._flush_completion()
        try:
            retry_until = float(self.store.setting("delivery_retry_after", "0"))
            if not math.isfinite(retry_until) or retry_until < 0:
                raise ValueError("invalid cooldown")
        except (TypeError, ValueError, OverflowError):
            retry_until = 0
            self.store.set_setting("delivery_retry_after", "0")
            log.warning("Invalid saved delivery cooldown was reset")
        if retry_until > time.time():
            return False
        item = self.store.claim_support()
        is_support = item is not None
        if item is None:
            item = self.store.claim_broadcast()
        if item is None:
            return False
        recipient = item["recipient_id"] if is_support else item["user_id"]
        self._pace(recipient)
        if self.stop_event.is_set():
            if is_support:
                self.store.finish_support(item, "pending")
            else:
                self.store.release_broadcast(item)
            return False
        if not is_support and not self.store.broadcast_still_allowed(item):
            self.store.release_broadcast(item)
            return True
        try:
            markup = (support_markup(self.ui, item, admin_id=self.store.admin_id) if is_support
                      else broadcast_markup(self.ui, item["buttons"]))
            result = send_content(self.bot, self.types, recipient, item["payload"], reply_markup=markup)
            message_id = getattr(result, "message_id", None)
            if not isinstance(message_id, int) or message_id <= 0:
                raise RuntimeError("missing_message_id")
        except Exception as exc:
            kind, retry = classify_error(exc)
            details = {"error": kind}
            if isinstance(exc, ContentMismatchError):
                details["message_id"] = exc.message_id
            pause = False
            if kind == "rate_limit":
                until = time.time() + retry + 1
                self.store.set_setting("delivery_retry_after", str(until))
                if item["attempts"] < 8:
                    status = "pending"
                    details["retry_at"] = until
                else:
                    status, pause = "failed", True
            elif kind == "recipient_unavailable":
                status = "blocked"
                self.store.mark_blocked(recipient)
            elif kind == "transport_ambiguous":
                status, pause = "uncertain", True
            else:
                status, pause = "failed", True
            if not is_support:
                details["pause"] = pause
            saved = self._complete(is_support, item, status, **details)
            log.warning("%s delivery: %s", "support" if is_support else "broadcast", kind)
            return saved
        return self._complete(is_support, item, "sent", message_id=message_id)
