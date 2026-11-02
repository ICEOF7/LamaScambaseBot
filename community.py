"""Telegram screens and state handlers for support, broadcasts and presentation."""
from __future__ import annotations
from hybrid_identity import message_time
import html
import time
import threading
from functools import wraps
from datetime import datetime, timezone

import texts
from delivery import DeliveryWorker, broadcast_markup, classify_error
from message_content import CONTENT_TYPES, capture_message, content_excerpt, parse_buttons, send_content
from ui import ICON_CANDIDATES, PACK_NAME

JOB_LABELS = {"draft":"Черновик", "queued":"В очереди", "running":"Отправляется", "paused":"На паузе",
              "cancelled":"Отменена", "completed":"Завершена", "failed":"Ошибка"}
TICKET_LABELS = {"waiting_admin":"Ждём ответа поддержки", "waiting_user":"Есть ответ поддержки", "closed":"Закрыто"}
ERROR_LABELS = {
    "formatting_not_preserved":"Telegram принял сообщение, но не сохранил нужное форматирование или кнопки. Отправка остановлена; повторов не будет.",
    "emoji_not_allowed":"Telegram отклонил premium-эмодзи. Проверь Premium у владельца бота.",
    "content_rejected":"Telegram отклонил сообщение или кнопки. Проверь текст в предпросмотре новой рассылки.",
    "invalid_token":"Telegram отклонил токен бота. Проверь приватный .env на сервере.",
    "transport_ambiguous":"Не удалось подтвердить доставку. Спорное сообщение повторно не отправляется.",
    "restart_ambiguous":"Перезапуск прервал подтверждение доставки. Спорные сообщения не будут повторены.",
    "rate_limit":"Telegram ограничил скорость отправки.",
}


def esc(value):
    return html.escape(str(value), quote=False)


def date(value):
    return datetime.fromtimestamp(float(value), timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


class CommunityFeatures:
    def __init__(self, *, bot, types_module, store, ui, set_state, get_state, clear_state,
                 remember, main_menu, admin_id, per_second=8):
        self.bot, self.raw = bot, bot.raw
        self.types, self.store, self.ui = types_module, store, ui
        self.set_state, self.get_state, self.clear_state = set_state, get_state, clear_state
        self.remember, self.main_menu, self.admin_id = remember, main_menu, admin_id
        self.worker = DeliveryWorker(store, self.raw, types_module, ui, per_second=per_second)
        self._form_locks = [threading.RLock() for _ in range(64)]
        self._emoji_stop = threading.Event()
        self._emoji_thread = None
        self._emoji_retry = 60

    def serialized(self, handler):
        @wraps(handler)
        def wrapped(update, *args, **kwargs):
            with self._form_locks[update.from_user.id % len(self._form_locks)]:
                return handler(update, *args, **kwargs)
        return wrapped

    def refresh_emoji_pack(self):
        try:
            if self.ui.enabled:
                self.ui.load_pack(self.raw)
            self._emoji_retry = 60
            return 3600
        except Exception as exc:
            self.ui.last_error = "Не удалось загрузить TgAndroidIcons: " + type(exc).__name__
            result = getattr(exc, 'result_json', None)
            parameters = result.get('parameters') if isinstance(result, dict) else None
            retry_after = parameters.get('retry_after', 0) if isinstance(parameters, dict) else 0
            try:
                delay = max(self._emoji_retry, int(retry_after))
            except (TypeError, ValueError):
                delay = self._emoji_retry
            self._emoji_retry = min(900, self._emoji_retry * 2)
            return delay

    def _retry_emoji_pack(self, initial_delay):
        delay = initial_delay
        while not self._emoji_stop.wait(delay):
            delay = self.refresh_emoji_pack()

    def start(self):
        if self.ui.enabled and not (self._emoji_thread and self._emoji_thread.is_alive()):
            delay = self.refresh_emoji_pack()
            self._emoji_stop.clear()
            self._emoji_thread = threading.Thread(target=self._retry_emoji_pack, args=(delay,),
                                                  name='telegram-emoji-refresh', daemon=True)
            self._emoji_thread.start()
        self.worker.start()

    def stop(self):
        self._emoji_stop.set()
        if self._emoji_thread and self._emoji_thread is not threading.current_thread():
            self._emoji_thread.join(timeout=20)
        self.worker.stop()

    def button(self, label, route, icon=None, style="auto"):
        return self.ui.button(label, callback_data=route, icon=icon, style=style)

    def back(self, target="menu:main"):
        return self.ui.keyboard([[self.button("Назад", target, "back", style=None)]])

    def screen(self, chat_id, text, rows=None, *, message_id=None):
        markup = self.ui.keyboard(rows or [[self.button("В меню", "menu:main", "home", style=None)]])
        if message_id:
            try:
                return self.bot.edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=markup)
            except Exception as exc:
                result = getattr(exc, "result_json", {}) or {}
                desc = str(result.get("description", "")).lower()
                if "message is not modified" in desc:
                    return None
                raise
        return self.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)

    def notification_screen(self, uid, *, message_id=None):
        enabled = self.store.notifications(uid)
        self.screen(uid, "[[icon:notify]] <b>Уведомления</b>\n\n"
                    + ("Новости бота <b>включены</b>." if enabled else "Новости бота <b>выключены</b>.")
                    + "\nОтветы поддержки и решения по твоим жалобам приходят независимо от этой настройки.", [
                        [self.button("Выключить новости" if enabled else "Включить новости",
                                     "notify:off" if enabled else "notify:on", "mute" if enabled else "notify")],
                        [self.button("В меню", "menu:main", "home", style=None)]
                    ], message_id=message_id)

    def support_home(self, uid, *, message_id=None):
        self.screen(uid, texts.SUPPORT, [
            [self.button("Написать в поддержку", "sup:new", "support", "success")],
            [self.button("Мои обращения", "menu:my_tickets", "ticket")],
            [self.button("Обжаловать запись", "menu:appeal", "appeal", style=None)],
            [self.button("В меню", "menu:main", "home", style=None)],
        ], message_id=message_id)

    def tickets_screen(self, uid, *, admin=False, closed=False, page=0, message_id=None):
        records, total = self.store.tickets(uid, admin_view=admin, closed=closed, page=page)
        prefix = "adm" if admin else "own"
        mode = "closed" if closed else "open"
        title = "Обращения пользователей" if admin else "Мои обращения"
        text = f"[[icon:ticket]] <b>{title}</b>\n\n" + ("Закрытые" if closed else "Открытые") + f": <b>{total}</b>."
        if not records:
            text += "\n\nЗдесь пока нет обращений."
        rows = []
        for ticket in records:
            label = f'#{ticket["id"]} · {ticket["subject"][:38]}'
            rows.append([self.button(label, f'sup:ticket:{ticket["id"]}:0', "ticket")])
            text += f'\n\n<b>#{ticket["id"]}</b> · {esc(ticket["subject"])}\n{TICKET_LABELS.get(ticket["status"], "Обрабатывается")}'
        nav = []
        if page > 0:
            nav.append(self.button("Назад", f"sup:list:{prefix}:{mode}:{page-1}", "back", style=None))
        if (page+1)*6 < total:
            nav.append(self.button("Далее", f"sup:list:{prefix}:{mode}:{page+1}", "next", style=None))
        rows.append(nav)
        rows.append([self.button("Открытые" if closed else "Закрытые", f"sup:list:{prefix}:{'open' if closed else 'closed'}:0", "list")])
        rows.append([self.button("В админку" if admin else "В поддержку", "admin:panel" if admin else "menu:support", "back", style=None)])
        self.screen(uid, text, rows, message_id=message_id)

    def ticket_screen(self, uid, tid, *, page=0, message_id=None):
        ticket = self.store.ticket(tid, uid, page=page)
        text = f'[[icon:ticket]] <b>Обращение #{tid}</b>\n{esc(ticket["subject"])}\n\n'
        text += f'<b>Статус:</b> {TICKET_LABELS.get(ticket["status"], "Обрабатывается")}\n'
        if uid == self.admin_id:
            text += f'Автор: <code>{ticket["owner_id"]}</code>\n'
        text += f'Создано: {date(ticket["created_at"])}\n\n<b>Переписка</b>'
        rows = []
        for message in ticket["messages"]:
            author = "Поддержка" if message["role"] == "admin" else "Пользователь"
            if message["author_id"] == uid:
                author = "Ты"
            text += f'\n\n<b>{author}</b> · {date(message["created_at"])}\n{esc(content_excerpt(message["payload"], 220))}'
            if message["delivery_status"] in {"blocked", "failed", "uncertain"}:
                text += "\n<i>Уведомление не подтверждено; сообщение сохранено здесь.</i>"
            rows.append([self.button(f'Открыть сообщение #{message["id"]}', f'sup:message:{message["id"]}', "document", style=None)])
        if not ticket["messages"]:
            text += "\nНа этой странице нет сообщений."
        nav = []
        if page > 0:
            nav.append(self.button("Новее", f"sup:ticket:{tid}:{page-1}", "back", style=None))
        if (page+1)*5 < ticket["total"]:
            nav.append(self.button("Ранее", f"sup:ticket:{tid}:{page+1}", "next", style=None))
        rows.append(nav)
        if ticket["status"] == "closed":
            rows.append([self.button("Открыть снова", f"sup:reopen:{tid}", "refresh", "success")])
        else:
            rows.append([self.button("Ответить", f"sup:reply:{tid}", "support", "success"),
                         self.button("Закрыть", f"sup:close:{tid}", "ok", "danger")])
        rows.append([self.button("Обновить", f"sup:ticket:{tid}:{page}", "refresh", style=None)])
        rows.append([self.button("К обращениям", "admin:support" if uid == self.admin_id else "menu:my_tickets", "back", style=None)])
        self.screen(uid, text, rows, message_id=message_id)

    def broadcast_list(self, uid, page=0, *, message_id=None):
        records = self.store.jobs(uid, page)
        text = ("[[icon:broadcast]] <b>Рассылки</b>\n\n"
                f"Доступно получателей: <b>{self.store.audience_count()}</b>.\n"
                "Только пользователи личного чата, у которых включены новости.\n"
                "Текст и кнопки сначала попадут в черновик, не в рассылку.")
        rows = [[self.button("Создать рассылку", "bc:new", "add", "success")]]
        for job in records:
            rows.append([self.button(f'#{job["id"]} · {JOB_LABELS.get(job["status"], job["status"])}', f'bc:view:{job["id"]}', "broadcast")])
        nav = []
        if page:
            nav.append(self.button("Назад", f"bc:list:{page-1}", "back", style=None))
        if len(records) == 6:
            nav.append(self.button("Далее", f"bc:list:{page+1}", "next", style=None))
        rows.append(nav)
        rows.append([self.button("В админку", "admin:panel", "back", style=None)])
        self.screen(uid, text, rows, message_id=message_id)

    def broadcast_screen(self, uid, jid, *, message_id=None):
        job = self.store.job(jid, uid)
        text = f'[[icon:broadcast]] <b>Рассылка #{jid}</b>\n{JOB_LABELS.get(job["status"], job["status"])}\n\n'
        rows = []
        if job["status"] == "draft":
            text += ("<b>Сообщение:</b>\n" + esc(content_excerpt(job["payload"], 280)) if job["payload"] else "Сообщение ещё не добавлено.")
            text += f'\n\nКнопок: <b>{len(job["buttons"])}</b>. Получателей сейчас: <b>{self.store.audience_count()}</b>.'
            rows.append([self.button("Изменить сообщение", f"bc:edit:{jid}", "report"), self.button("Кнопки", f"bc:buttons:{jid}", "link")])
            if job["payload"]:
                rows.append([self.button("Предпросмотр", f"bc:preview:{jid}", "preview")])
                if job["preview_revision"] == job["revision"]:
                    rows.append([self.button("Перейти к отправке", f"bc:confirm:{jid}", "send", "success")])
            rows.append([self.button("Удалить черновик", f"bc:cancel:{jid}", "delete", "danger")])
        else:
            c = job["counts"]
            text += (f'Получателей: <b>{job["total"]}</b>\nДоставлено: <b>{c.get("sent",0)}</b>\n'
                     f'В очереди: <b>{c.get("pending",0)}</b> · отправляется: <b>{c.get("inflight",0)}</b>\n'
                     f'Недоступны / заблокировали: <b>{c.get("blocked",0)}</b>\nОшибки: <b>{c.get("failed",0)}</b>\n'
                     f'Не подтверждено: <b>{c.get("uncertain",0)}</b> · пропущено: <b>{c.get("skipped",0)}</b>')
            if job["last_error"]:
                text += "\n\n" + ERROR_LABELS.get(job["last_error"], "Проверь результат доставки перед продолжением.")
            if c.get("uncertain", 0):
                text += "\n\n<i>«Не подтверждено» не означает «не доставлено». Эти сообщения автоматически не повторяются.</i>"
            if job["status"] in {"queued", "running"}:
                rows.append([self.button("Пауза", f"bc:pause:{jid}", "pause"), self.button("Остановить", f"bc:cancel:{jid}", "stop", "danger")])
            elif job["status"] == "paused":
                rows.append([self.button("Продолжить очередь", f"bc:resume:{jid}", "play", "success"), self.button("Остановить", f"bc:cancel:{jid}", "stop", "danger")])
            rows.append([self.button("Обновить", f"bc:view:{jid}", "refresh")])
        rows.append([self.button("Все рассылки", "admin:broadcast", "back", style=None)])
        self.screen(uid, text, rows, message_id=message_id)

    def ui_screen(self, uid, *, message_id=None):
        if uid != self.admin_id:
            raise PermissionError("Нет доступа.")
        missing = sorted(set(ICON_CANDIDATES)-set(self.ui.mapping))
        text = (f'[[icon:palette]] <b>Оформление</b>\n\nПак: <b>{PACK_NAME}</b>\n'
                f'В каталоге: <b>{len(self.ui.catalog)}</b> · настроено ролей: <b>{len(self.ui.mapping)}/{len(ICON_CANDIDATES)}</b>.\n'
                f'Использование premium-иконок: <b>{"включено настройкой" if self.ui.enabled else "выключено в .env"}</b>.\n\n'
                'Основные кнопки — синие; подтверждение — зелёное; отмена и опасные действия — красные. '
                'Назад и вторичная навигация — нейтральные.')
        labels = {'unreadable': 'ответ API не удалось проверить', 'not_checked': 'ещё не проверено', 'confirmed': 'подтверждено ответом Telegram',
                  'missing': 'Telegram не сохранил оформление', 'rejected': 'Telegram запретил custom emoji'}
        text += ('\n\n<b>Фактическая отправка:</b>\nЭмодзи: '
                 + labels.get(self.ui.delivery_status.get('emoji'), 'не проверено')
                 + '\nКнопки: ' + labels.get(self.ui.delivery_status.get('buttons'), 'не проверено'))
        text += '\nЭто проверка ответа API; вид в клиенте проверь по тестовому сообщению.'
        if not self.ui.enabled:
            text += '\n\nВ приватном .env включи CUSTOM_EMOJI_ENABLED=true и перезапусти бота.'
        if self.ui.last_error:
            text += "\n\n" + esc(self.ui.last_error)
        if missing:
            text += "\n\nБез назначенной иконки: <code>" + ", ".join(missing) + "</code>."
        text += ('\n\nЕсли у одного символа несколько вариантов в паке, выбери нужный в каталоге и назначь: '
                 '<code>/seticon search ID_ЭМОДЗИ</code>. Принимаются только ID из этого пака. '
                 'Без доступного premium-эмодзи интерфейс остаётся текстовым, без подмены другим паком.')
        self.screen(uid, text, [[self.button("Проверить отправку мне", "admin:ui:probe", "preview", "success")],
                               [self.button("Загрузить / обновить пак", "admin:ui:refresh", "refresh")],
                               [self.button("Каталог иконок", "admin:ui:gallery:0", "palette")],
                               [self.button("В админку", "admin:panel", "back", style=None)]], message_id=message_id)

    def ui_gallery(self, uid, page=0, *, message_id=None):
        if uid != self.admin_id:
            raise PermissionError("Нет доступа.")
        items = self.ui.catalog[page*8:(page+1)*8]
        text = "[[icon:palette]] <b>TgAndroidIcons · каталог</b>\n\n"
        if not items:
            text += "Каталог пуст. Сначала загрузи пак в настройках оформления."
        for item in items:
            icon = f'<tg-emoji emoji-id="{item["id"]}">{esc(item["emoji"])}</tg-emoji>'
            text += f'{icon}  <code>{item["id"]}</code>\n\n'
        rows = []
        nav = []
        if page:
            nav.append(self.button("Назад", f"admin:ui:gallery:{page-1}", "back", style=None))
        if (page+1)*8 < len(self.ui.catalog):
            nav.append(self.button("Далее", f"admin:ui:gallery:{page+1}", "next", style=None))
        rows.append(nav)
        rows.append([self.button("К оформлению", "admin:ui", "back", style=None)])
        self.screen(uid, text, rows, message_id=message_id)

    def handle_command(self, message):
        if message.chat.type != "private":
            return
        uid = message.from_user.id
        self.remember(message.from_user, message.chat.id, observed_at=message_time(message), event_key=f'message:{message.chat.id}:{message.message_id}')
        text = message.text or ""
        command = text.split()[0].split("@")[0].lower()
        try:
            if command == "/seticon":
                if uid != self.admin_id:
                    raise PermissionError("Нет доступа.")
                parts = text.split()
                if len(parts) != 3:
                    raise ValueError("Формат: /seticon search ID_ЭМОДЗИ. ID смотри в админке → Оформление → Каталог.")
                self.ui.assign(parts[1], parts[2])
                self.ui_screen(uid)
                return
            self.clear_state(uid)
            if command in {"/support", "/helpdesk"}:
                self.support_home(uid)
            elif command == "/tickets":
                self.tickets_screen(uid)
            elif command == "/broadcast":
                self.broadcast_list(uid)
            elif command == "/notifications":
                self.notification_screen(uid)
            elif command == "/unsubscribe":
                self.store.set_notifications(uid, False)
                self.notification_screen(uid)
            elif command == "/help":
                self.help_screen(uid)
            elif command == "/cancel":
                self.screen(uid, "Действие отменено. Сохранённые обращения и черновики остались на месте.",
                            [[self.button("В меню", "menu:main", "home")]])
        except (ValueError, PermissionError) as exc:
            self.bot.send_message(uid, str(exc), reply_markup=self.back())

    def help_screen(self, uid, *, message_id=None):
        self.screen(uid, texts.HELP, [
            [self.button("Проверить аккаунт", "menu:check", "search")],
            [self.button("Поддержка", "menu:support", "support"), self.button("Твои данные", "menu:privacy", "lock", style=None)],
            [self.button("Уведомления", "menu:notifications", "notify", style=None)],
            [self.button("В меню", "menu:main", "home", style=None)],
        ], message_id=message_id)

    def wants_message(self, message):
        if getattr(message.chat, "type", None) != "private":
            return False
        text = getattr(message, "text", "") or ""
        state = self.get_state(message.from_user.id).get("state", "")
        if state.startswith(("community_support_", "community_bc_")):
            return True
        if text.startswith("/"):
            return False
        reply = getattr(message, "reply_to_message", None)
        return bool(reply and self.store.replied_ticket(message.from_user.id, reply.message_id))

    def handle_message(self, message):
        if not self.wants_message(message):
            return
        uid = message.from_user.id
        self.remember(message.from_user, message.chat.id, observed_at=message_time(message), event_key=f'message:{message.chat.id}:{message.message_id}')
        state = self.get_state(uid)
        reply = getattr(message, "reply_to_message", None)
        tid = self.store.replied_ticket(uid, reply.message_id) if reply else None
        if tid is not None:
            self.set_state(uid, "community_support_reply", ticket_id=tid)
            state = self.get_state(uid)
        elif not state:
            return
        kind = state.get("state", "")
        try:
            if kind.startswith("community_bc_") and uid != self.admin_id:
                raise PermissionError("Нет доступа.")
            if kind == "community_support_subject":
                subject = (getattr(message, "text", "") or "").strip()
                if not 3 <= len(subject) <= 80 or any(ord(ch) < 32 for ch in subject):
                    raise ValueError("Напиши короткую тему текстом: от 3 до 80 символов, одной строкой.")
                self.set_state(uid, "community_support_body", subject=subject)
                self.screen(uid, f'[[icon:support]] <b>{esc(subject)}</b>\n\nРасскажи, что произошло и какая помощь нужна. '
                            'Можно прислать текст или вложение с подписью. Первый ответ создаст обращение; затем можно добавить ещё материалы.',
                            [[self.button("Отменить", "menu:support", "cancel", "danger")]])
            elif kind == "community_support_body":
                payload = capture_message(message, allow_album=True)
                tid, created = self.store.create_ticket(uid, state["subject"], payload, message.chat.id, message.message_id)
                self.set_state(uid, "community_support_reply", ticket_id=tid)
                self.worker.wake()
                self.screen(uid, f'[[icon:ok]] <b>Обращение #{tid} {"создано" if created else "уже сохранено"}</b>\n\n'
                            'Ответ придёт в этот чат. Можешь сразу дописать подробности или отправить дополнительные вложения.', [
                                [self.button("Открыть обращение", f"sup:ticket:{tid}:0", "ticket")],
                                [self.button("Готово", "menu:support", "ok", "success")]])
            elif kind == "community_support_reply":
                tid = int(state["ticket_id"])
                payload = capture_message(message, allow_album=True)
                _, created = self.store.add_reply(tid, uid, payload, message.chat.id, message.message_id)
                self.worker.wake()
                self.screen(uid, f'[[icon:ok]] Сообщение {"добавлено" if created else "уже добавлено"} в обращение <b>#{tid}</b>.\n'
                            'Можно отправить ещё сообщение или закончить.', [
                                [self.button("К обращению", f"sup:ticket:{tid}:0", "ticket"), self.button("Готово", "admin:support" if uid == self.admin_id else "menu:support", "ok", "success")]])
            elif kind == "community_bc_content":
                payload = capture_message(message)
                jid = int(state["job_id"])
                self.store.save_draft(jid, uid, payload=payload)
                self.clear_state(uid)
                self.broadcast_screen(uid, jid)
            elif kind == "community_bc_buttons":
                text = getattr(message, "text", "") or ""
                if not text.strip():
                    raise ValueError("Пришли список кнопок текстом.")
                buttons = [] if text.strip() == "-" else parse_buttons(text, ICON_CANDIDATES)
                jid = int(state["job_id"])
                self.store.save_draft(jid, uid, buttons=buttons)
                self.clear_state(uid)
                self.broadcast_screen(uid, jid)
        except (ValueError, PermissionError) as exc:
            if isinstance(exc, PermissionError):
                self.clear_state(uid)
            self.bot.send_message(uid, str(exc), reply_markup=self.back("admin:broadcast" if kind.startswith("community_bc_") else "menu:support"))

    def handle_callback(self, call):
        data = call.data or ""
        public = {"menu:support", "menu:my_tickets", "menu:help", "menu:privacy", "menu:notifications", "notify:on", "notify:off"}
        recognized = data in public or data.startswith(("sup:", "bc:", "admin:ui")) or data in {"admin:broadcast", "admin:support"}
        if not recognized:
            return False
        if not call.message or call.message.chat.type != "private":
            self.bot.answer_callback_query(call.id, "Открой личный чат с ботом.", show_alert=True)
            return True
        uid, mid = call.from_user.id, call.message.message_id
        if call.message.chat.id != uid:
            self.bot.answer_callback_query(call.id, "Недоступно в этом чате.", show_alert=True)
            return True
        if (data.startswith(("bc:", "admin:")) or data.startswith("sup:list:adm:")) and uid != self.admin_id:
            self.bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
            return True
        try:
            self.bot.answer_callback_query(call.id)
            self.clear_state(uid)
            if data == "menu:support":
                self.support_home(uid, message_id=mid)
            elif data == "menu:my_tickets":
                self.tickets_screen(uid, message_id=mid)
            elif data == "admin:support":
                self.tickets_screen(uid, admin=True, message_id=mid)
            elif data == "menu:help":
                self.help_screen(uid, message_id=mid)
            elif data == "menu:privacy":
                self.screen(uid, texts.PRIVACY, [[self.button("Обжаловать запись", "menu:appeal", "appeal")],
                                               [self.button("Поддержка", "menu:support", "support")],
                                               [self.button("В меню", "menu:main", "home", style=None)]], message_id=mid)
            elif data in {"menu:notifications", "notify:on", "notify:off"}:
                if data != "menu:notifications":
                    self.store.set_notifications(uid, data == "notify:on")
                self.notification_screen(uid, message_id=mid)
            elif data == "sup:new":
                self.set_state(uid, "community_support_subject")
                self.screen(uid, "[[icon:support]] <b>Новое обращение · 1 из 2</b>\n\n"
                            "Напиши короткую тему. Например: «Не находится аккаунт» или «Ошибка при отправке жалобы».",
                            [[self.button("Отменить", "menu:support", "cancel", "danger")]], message_id=mid)
            elif data.startswith("sup:list:"):
                _, _, scope, mode, page = data.split(":")
                if scope not in {"adm", "own"} or mode not in {"open", "closed"}:
                    raise ValueError("Неизвестный раздел обращений.")
                self.tickets_screen(uid, admin=scope=="adm", closed=mode=="closed", page=max(0,int(page)), message_id=mid)
            elif data.startswith("sup:ticket:"):
                _, _, tid, page = data.split(":")
                self.ticket_screen(uid, int(tid), page=max(0,int(page)), message_id=mid)
            elif data.startswith("sup:message:"):
                message = self.store.ticket_message(int(data.split(":")[2]), uid)
                send_content(self.raw, self.types, uid, message["payload"], reply_markup=self.back(f'sup:ticket:{message["ticket_id"]}:0'))
            elif data.startswith("sup:reply:"):
                tid = int(data.split(":")[2])
                ticket = self.store.ticket(tid, uid)
                if ticket["status"] == "closed":
                    raise ValueError("Обращение закрыто. Сначала открой его снова.")
                self.set_state(uid, "community_support_reply", ticket_id=tid)
                self.screen(uid, f'[[icon:support]] <b>Ответ в обращение #{tid}</b>\n\n'
                            'Отправь текст или вложение. Несколько сообщений подряд попадут в это обращение.',
                            [[self.button("К обращению", f"sup:ticket:{tid}:0", "back", style=None)]], message_id=mid)
            elif data.startswith("sup:close:"):
                tid = int(data.split(":")[2])
                self.store.ticket(tid, uid)
                self.screen(uid, f'[[icon:ticket]] <b>Закрыть обращение #{tid}?</b>\n\nПереписка сохранится. При необходимости обращение можно открыть снова.',
                            [[self.button("Закрыть обращение", f"sup:close_yes:{tid}", "ok", "danger")],
                             [self.button("Не закрывать", f"sup:ticket:{tid}:0", "back", style=None)]], message_id=mid)
            elif data.startswith(("sup:close_yes:", "sup:reopen:")):
                tid = int(data.split(":")[2])
                self.store.close_ticket(tid, uid, reopen=data.startswith("sup:reopen:"))
                self.ticket_screen(uid, tid, message_id=mid)
            elif data == "admin:broadcast":
                self.broadcast_list(uid, message_id=mid)
            elif data.startswith("bc:list:"):
                self.broadcast_list(uid, max(0, int(data.split(":")[2])), message_id=mid)
            elif data == "bc:new":
                jid = self.store.create_broadcast(uid)
                self.set_state(uid, "community_bc_content", job_id=jid)
                self.screen(uid, texts.BROADCAST_INPUT, [[self.button("К черновику", f"bc:view:{jid}", "back", style=None)]], message_id=mid)
            elif data.startswith("bc:view:"):
                self.broadcast_screen(uid, int(data.split(":")[2]), message_id=mid)
            elif data.startswith(("bc:edit:", "bc:buttons:")):
                jid = int(data.split(":")[2])
                job = self.store.job(jid, uid)
                if job["status"] != "draft":
                    raise ValueError("Редактировать можно только черновик.")
                buttons = data.startswith("bc:buttons:")
                self.set_state(uid, "community_bc_buttons" if buttons else "community_bc_content", job_id=jid)
                self.screen(uid, texts.BUTTONS_INPUT if buttons else texts.BROADCAST_INPUT,
                            [[self.button("К черновику", f"bc:view:{jid}", "back", style=None)]], message_id=mid)
            elif data.startswith("bc:preview:"):
                jid = int(data.split(":")[2])
                job = self.store.job(jid, uid)
                if job["status"] != "draft" or not job["payload"]:
                    raise ValueError("Сначала добавь сообщение в черновик.")
                result = send_content(self.raw, self.types, uid, job["payload"], reply_markup=broadcast_markup(self.ui, job["buttons"]))
                if not getattr(result, "message_id", None):
                    raise ValueError("Telegram не подтвердил отправку предпросмотра.")
                if not self.store.mark_preview(jid, uid, job["revision"]):
                    raise ValueError("Черновик изменился. Открой новый предпросмотр.")
                self.broadcast_screen(uid, jid)
            elif data.startswith("bc:confirm:"):
                jid = int(data.split(":")[2])
                job = self.store.job(jid, uid)
                if job["status"] != "draft" or not job["payload"] or job["preview_revision"] != job["revision"]:
                    raise ValueError("Сначала открой предпросмотр актуальной версии.")
                self.screen(uid, f'[[icon:send]] <b>Отправить рассылку #{jid}?</b>\n\n'
                            f'Доступно получателей: <b>{self.store.audience_count()}</b>. '
                            'После запуска изменить сообщение нельзя. Рассылка бесплатная, без платного ускорения Telegram.',
                            [[self.button("Отправить всем", f'bc:send:{jid}:{job["revision"]}', "send", "success")],
                             [self.button("Вернуться к черновику", f"bc:view:{jid}", "back", style=None)]], message_id=mid)
            elif data.startswith("bc:send:"):
                _, _, jid, revision = data.split(":")
                self.store.enqueue(int(jid), uid, int(revision))
                self.worker.wake()
                self.broadcast_screen(uid, int(jid), message_id=mid)
            elif data.startswith("bc:cancel:"):
                jid = int(data.split(":")[2])
                self.store.job(jid, uid)
                self.screen(uid, f'[[icon:stop]] <b>Отменить рассылку #{jid}?</b>\n\n'
                            'Уже доставленные сообщения останутся у пользователей. Запрос, который Telegram уже получил, может успеть завершиться.',
                            [[self.button("Да, отменить", f"bc:cancel_yes:{jid}", "stop", "danger")],
                             [self.button("Назад", f"bc:view:{jid}", "back", style=None)]], message_id=mid)
            elif data.startswith(("bc:pause:", "bc:resume:", "bc:cancel_yes:")):
                _, action, jid = data.split(":")
                self.store.control_job(int(jid), uid, "cancel" if action == "cancel_yes" else action)
                self.worker.wake()
                self.broadcast_screen(uid, int(jid), message_id=mid)
            elif data == "admin:ui":
                self.ui_screen(uid, message_id=mid)
            elif data == "admin:ui:probe":
                if uid != self.admin_id:
                    raise PermissionError("Нет доступа.")
                if self.ui.enabled and not self.ui.catalog:
                    self.ui.load_pack(self.raw)
                rows = [[self.button('Проверка', 'menu:check', 'search', 'primary'),
                         self.button('Поддержка', 'menu:support', 'support', 'success')],
                        [self.button('В админку', 'admin:panel', 'back', 'danger')]]
                self.screen(uid, '[[icon:search]] <b>Проверка TgAndroidIcons</b>\n\n'
                                 '[[icon:document]] Иконки должны быть из одного пака. '
                                 'Проверь также синий, зелёный и красный цвета кнопок. '
                                 'Это сообщение отправлено только тебе.', rows)
                self.ui_screen(uid, message_id=mid)
            elif data == "admin:ui:refresh":
                self.ui.load_pack(self.raw)
                self.ui_screen(uid, message_id=mid)
            elif data.startswith("admin:ui:gallery:"):
                self.ui_gallery(uid, max(0,int(data.split(":")[3])), message_id=mid)
            else:
                raise ValueError("Кнопка устарела. Открой нужный раздел через меню.")
        except (ValueError, PermissionError) as exc:
            self.bot.send_message(uid, str(exc), reply_markup=self.back("admin:panel" if uid == self.admin_id else "menu:support"))
        except Exception as exc:
            kind, _ = classify_error(exc)
            explanation = ERROR_LABELS.get(kind, "Telegram не ответил. Сохранённые обращения и черновики остались в боте.")
            self.bot.send_message(uid, "Не удалось выполнить действие. " + explanation,
                                  reply_markup=self.back("admin:panel" if uid == self.admin_id else "menu:support"))
        return True
