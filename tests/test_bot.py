"""Isolated tests: no real Telegram token, account, network or database is used."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class FakeButton:
    def __init__(self, text, callback_data=None, url=None, style=None, icon_custom_emoji_id=None):
        self.text, self.callback_data, self.url = text, callback_data, url
        self.style, self.icon_custom_emoji_id = style, icon_custom_emoji_id

    def to_dict(self):
        return {k:v for k,v in vars(self).items() if v is not None}


class FakeMarkup:
    def __init__(self, **kwargs):
        self.buttons = []
        self.keyboard = []

    def add(self, *buttons):
        self.buttons.extend(buttons)
        self.keyboard.append(list(buttons))

    def to_dict(self):
        return {'inline_keyboard': [[b.to_dict() for b in row] for row in self.keyboard]}


class FakeEntity:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def to_dict(self):
        return vars(self).copy()

    @classmethod
    def de_json(cls, value):
        import json
        return cls(**(json.loads(value) if isinstance(value, str) else value))


class FakeBot:
    def __init__(self, token, **kwargs):
        self.token = token
        self.calls = []
        self.member_status = 'member'
        self.fail_admin_notifications = False
        self.polling_started = False
        self.message_handlers = []
        self.callback_handlers = []
        self.next_message_id = 500
        self.fail_next = None

    def _result(self):
        self.next_message_id += 1
        return types.SimpleNamespace(message_id=self.next_message_id)

    def message_handler(self, *args, **kwargs):
        def register(fn):
            self.message_handlers.append((args, kwargs, fn))
            return fn
        return register

    def callback_query_handler(self, *args, **kwargs):
        def register(fn):
            self.callback_handlers.append((args, kwargs, fn))
            return fn
        return register

    def my_chat_member_handler(self, *args, **kwargs):
        return lambda fn: fn

    def send_message(self, chat_id, text, **kwargs):
        if self.fail_next:
            error, self.fail_next = self.fail_next, None
            raise error
        if chat_id == 9999 and self.fail_admin_notifications:
            raise RuntimeError('admin notification unavailable')
        self.calls.append(('send_message', chat_id, text, kwargs))
        result = self._result()
        result.text = text
        result.entities = kwargs.get('entities', [])
        result.reply_markup = kwargs.get('reply_markup')
        return result

    def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self.calls.append(('edit_message_text', chat_id, text, kwargs))
        return self._result()

    def answer_callback_query(self, callback_query_id, text='', **kwargs):
        self.calls.append(('answer_callback_query', callback_query_id, text, kwargs))
        return True

    def get_chat_member(self, channel, user_id):
        return types.SimpleNamespace(status=self.member_status)

    def copy_message(self, recipient, chat_id, message_id, **kwargs):
        self.calls.append(('copy_message', recipient, chat_id, message_id))
        return self._result()

    def get_sticker_set(self, name):
        from ui import ICON_CANDIDATES
        stickers = [types.SimpleNamespace(custom_emoji_id=str(7000000+i), emoji=values[0])
                    for i, values in enumerate(ICON_CANDIDATES.values())]
        return types.SimpleNamespace(sticker_type='custom_emoji', stickers=stickers)

    def set_my_commands(self, commands):
        self.commands = commands
        return True

    def send_photo(self, chat_id, file_id, **kwargs):
        self.calls.append(('send_photo', chat_id, file_id, kwargs))
        result = self._result()
        result.caption = kwargs.get('caption', '')
        result.caption_entities = kwargs.get('caption_entities', [])
        result.has_media_spoiler = bool(kwargs.get('has_spoiler', False))
        result.show_caption_above_media = bool(kwargs.get('show_caption_above_media', False))
        result.reply_markup = kwargs.get('reply_markup')
        return result

    def send_document(self, chat_id, file_id, **kwargs):
        self.calls.append(('send_document', chat_id, file_id, kwargs))
        result = self._result()
        result.caption = kwargs.get('caption', '')
        result.caption_entities = kwargs.get('caption_entities', [])
        result.has_media_spoiler = bool(kwargs.get('has_spoiler', False))
        result.show_caption_above_media = bool(kwargs.get('show_caption_above_media', False))
        result.reply_markup = kwargs.get('reply_markup')
        return result

    def send_video(self, chat_id, file_id, **kwargs):
        self.calls.append(('send_video', chat_id, file_id, kwargs))
        result = self._result()
        result.caption = kwargs.get('caption', '')
        result.caption_entities = kwargs.get('caption_entities', [])
        result.has_media_spoiler = bool(kwargs.get('has_spoiler', False))
        result.show_caption_above_media = bool(kwargs.get('show_caption_above_media', False))
        result.reply_markup = kwargs.get('reply_markup')
        return result

    def remove_webhook(self):
        self.calls.append(('remove_webhook',))

    def process_new_messages(self, messages):
        return None

    def process_new_callback_query(self, callbacks):
        return None

    def infinity_polling(self, **kwargs):
        self.polling_started = True
        self.calls.append(('infinity_polling', kwargs))


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        stub = types.ModuleType('telebot')
        stub.TeleBot = FakeBot
        fake_types = types.ModuleType('telebot.types')
        fake_types.InlineKeyboardMarkup = FakeMarkup
        fake_types.InlineKeyboardButton = FakeButton
        fake_types.MessageEntity = FakeEntity
        fake_types.KeyboardButton = lambda text, **kwargs: FakeEntity(text=text, **kwargs)
        fake_types.KeyboardButtonRequestUsers = FakeEntity
        fake_types.ReplyKeyboardMarkup = FakeMarkup
        fake_types.ReplyKeyboardRemove = FakeEntity
        fake_types.BotCommand = lambda command, description: types.SimpleNamespace(command=command, description=description)
        stub.types = fake_types
        values = {
            'API_TOKEN': 'DEMO_NOT_REAL',
            'ADMIN_ID': '9999',
            'DB_PATH': str(Path(self.tmp.name) / 'only_test.sqlite3'),
            'VISIBLE_STATUSES': 'accepted',
            'MANUAL_ADD_STATUS': 'pending',
            'REVIEW_STATUS': 'review',
            'USERNAME_LOOKUP_ENABLED': 'false',
            'CHANNEL_USERNAMES': '',
            'TELEGRAM_API_ID': '',
            'TELEGRAM_API_HASH': '',
        }
        self.env_patch = patch.dict(os.environ, values)
        self.modules_patch = patch.dict(sys.modules, {'telebot': stub, 'telebot.types': fake_types})
        self.env_patch.start()
        self.modules_patch.start()
        spec = importlib.util.spec_from_file_location('lamascambase_test_app', ROOT / 'app.py')
        self.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app)
        self.bot = self.app.bot

    def tearDown(self):
        self.app.conn.close()
        self.modules_patch.stop()
        self.env_patch.stop()
        self.tmp.cleanup()

    def user(self, uid=101, username='tester'):
        return types.SimpleNamespace(id=uid, username=username)

    def msg(self, uid=101, text='', kind='text', chat_type='private'):
        return types.SimpleNamespace(
            from_user=self.user(uid), chat=types.SimpleNamespace(id=uid if chat_type == 'private' else -400, type=chat_type),
            text=text, content_type=kind, message_id=53,
        )

    def callback(self, uid=101, data='menu:main', chat_type='private'):
        return types.SimpleNamespace(
            id='cb_1', data=data, from_user=self.user(uid),
            message=types.SimpleNamespace(chat=types.SimpleNamespace(id=uid if chat_type == 'private' else -400, type=chat_type), message_id=44),
        )

    def insert(self, target='@example', status='review', uid=101, note='Проверяем описание'):
        _, rid = self.app.db_execute('''INSERT INTO complaints
            (target, from_user, from_id, note, evidence, timestamp, status)
            VALUES (?, ?, ?, ?, '[]', ?, ?)''',
            (target, '@tester', uid, note, datetime.now(timezone.utc).isoformat(), status), commit=True)
        return rid

    def test_start_calls_telegram_polling(self):
        self.app.main()
        self.assertTrue(self.bot.polling_started)
        self.assertEqual([c[0] for c in self.bot.calls], ['remove_webhook', 'infinity_polling'])

    def test_target_is_strictly_validated(self):
        for value in ['@abc', '@ABC_123', 'alice_123']:
            self.assertTrue(self.app.valid_target(value), value)
        for value in ['@', '@a', '@hello evil', '@name/../', '@пользователь', '@'+'a'*50, '<script>']:
            self.assertFalse(self.app.valid_target(value), value)
        self.assertEqual(self.app.normalize_target('@EXAMPLE'), '@example')

    def test_unapproved_complaints_never_public_by_default(self):
        self.insert(status='review')
        self.insert(status='pending')
        self.assertEqual(self.app.get_visible_counts('@example'), {'accepted': 0})
        self.assertIn('не найдено', self.app.format_check_text('@example'))
        self.assertNotIn('Проверяем описание', self.app.format_check_text('@example'))
        approved_id = self.insert(status='accepted')
        result = self.app.format_check_text('@example')
        self.assertIn('На этот ник есть опубликованные жалобы', result)
        self.assertIn('Жалоба — утверждение автора', result)
        self.assertIn('#'+str(approved_id), result)

    def test_list_has_functional_detail_buttons(self):
        rid = self.insert(status='accepted')
        self.insert(status='pending')
        self.app.show_list_page(101, user_id=101)
        sent = [c for c in self.bot.calls if c[0] == 'send_message'][-1]
        buttons = sent[3]['reply_markup'].buttons
        self.assertIn(f'list:detail:{rid}:0', [b.callback_data for b in buttons])
        self.assertEqual(len([b for b in buttons if (b.callback_data or '').startswith('list:detail:')]), 1)

    def test_manual_pending_in_queue_but_not_public(self):
        self.app.insert_manual_scammers(['@example'], 'Ручная запись требует проверки')
        self.app.show_review_page(9999)
        text = [c[2] for c in self.bot.calls if c[0] == 'send_message'][-1]
        self.assertIn('@example', text)
        self.assertEqual(self.app.get_visible_counts('@example'), {'accepted': 0})

    def test_one_time_moderation_prevents_reapproval_and_reversal(self):
        rid = self.insert()
        self.assertTrue(self.app.change_complaint_status(rid, 'accepted'))
        self.assertFalse(self.app.change_complaint_status(rid, 'accepted'))
        self.assertFalse(self.app.change_complaint_status(rid, 'declined'))
        self.assertFalse(self.app.change_complaint_status(99999999, 'accepted'))
        self.assertEqual(self.app.get_complaint_by_id(rid)['status'], 'accepted')

    def test_hide_and_reopen_moderation(self):
        rid = self.insert(status='accepted')
        self.assertTrue(self.app.reopen_complaint(rid, hide=True))
        self.assertFalse(self.app.reopen_complaint(rid, hide=True))
        self.assertEqual(self.app.get_visible_counts('@example'), {'accepted': 0})
        self.assertEqual(self.app.get_complaint_by_id(rid)['status'], 'removed')
        self.assertTrue(self.app.reopen_complaint(rid))
        self.assertEqual(self.app.get_complaint_by_id(rid)['status'], 'review')
        self.assertFalse(self.app.reopen_complaint(rid))

    def test_callback_hide_is_admin_only(self):
        rid = self.insert(status='accepted')
        self.app.handle_callbacks(self.callback(uid=101,data=f'admin:hide:{rid}:0'))
        self.assertEqual(self.app.get_complaint_by_id(rid)['status'], 'accepted')
        self.app.handle_callbacks(self.callback(uid=9999,data=f'admin:hide:{rid}:0'))
        self.assertEqual(self.app.get_complaint_by_id(rid)['status'], 'removed')

    def test_old_public_config_is_clamped_to_accepted(self):
        original = self.app.VISIBLE_STATUSES
        self.app.VISIBLE_STATUSES = ('accepted',)
        self.insert(status='pending')
        self.assertEqual(self.app.get_visible_counts('@example'), {'accepted': 0})
        self.app.VISIBLE_STATUSES = original

    def test_nonadmin_cannot_open_admin_panel_or_approve(self):
        rid = self.insert()
        self.app.handle_callbacks(self.callback(uid=101, data=f'admin:approve:{rid}:0'))
        self.assertEqual(self.app.get_complaint_by_id(rid)['status'], 'review')
        self.assertTrue(any('Нет доступа' in c[2] for c in self.bot.calls if c[0] == 'answer_callback_query'))

    def test_private_chat_only_for_admin_callbacks(self):
        rid = self.insert()
        self.app.handle_callbacks(self.callback(uid=9999, data=f'admin:approve:{rid}:0', chat_type='supergroup'))
        self.assertEqual(self.app.get_complaint_by_id(rid)['status'], 'review')
        self.assertTrue(any('личный чат' in c[2] for c in self.bot.calls if c[0] == 'answer_callback_query'))

    def test_admin_callback_approves_once_and_notifies_once(self):
        rid = self.insert()
        call = self.callback(uid=9999, data=f'admin:approve:{rid}:0')
        self.app.handle_callbacks(call)
        self.app.handle_callbacks(call)
        self.assertEqual(self.app.get_complaint_by_id(rid)['status'], 'accepted')
        notices = [c for c in self.bot.calls if c[0] == 'send_message' and c[1] == 101 and 'приняли' in c[2]]
        self.assertEqual(len(notices), 1)

    def test_report_requires_evidence_and_prevents_double_submission(self):
        self.app.set_state(101, self.app.STATE_WAITING_FOR_REPORT_EVIDENCE,
                           target='@example', username='@tester', note='Проблема с заказом', evidence=[])
        self.app.finalize_complaint(101, 101)
        self.assertEqual(self.app.db_execute('SELECT COUNT(*) FROM complaints',fetchone=True)[0][0], 0)
        self.app.get_state(101)['evidence'].append({'type':'text', 'data':'Подтверждаю текстом'})
        self.app.finalize_complaint(101, 101)
        self.app.finalize_complaint(101, 101)
        self.assertEqual(self.app.db_execute('SELECT COUNT(*) FROM complaints',fetchone=True)[0][0], 1)
        self.assertEqual(self.app.get_complaint_by_id(1)['status'], 'review')
        self.assertTrue(any(c[1] == 101 and 'Готово' in c[2] for c in self.bot.calls if c[0]=='send_message'))

    def test_report_end_to_end_fsm(self):
        self.app.report_command(self.msg(text='/report'))
        self.assertEqual(self.app.get_state(101)['state'],self.app.STATE_WAITING_FOR_REPORT_TARGET)
        self.app.handle_report_target(self.msg(text='@EXAMPLE'))
        self.app.handle_report_note(self.msg(text='Товар оплачен, но не выдан'))
        self.app.handle_report_evidence(self.msg(text='Скрин переписки сохранён'))
        self.app.handle_callbacks(self.callback(data='report:submit'))
        self.assertEqual(self.app.get_state(101), {})
        self.assertEqual(self.app.get_visible_counts('@example'), {'accepted': 0})
        self.app.handle_callbacks(self.callback(uid=9999, data='admin:approve:1:0'))
        self.assertEqual(self.app.get_visible_counts('@example'), {'accepted': 1})
        self.app.handle_callbacks(self.callback(uid=9999, data='admin:hide:1:0'))
        self.assertEqual(self.app.get_visible_counts('@example'), {'accepted': 0})

    def test_notification_failure_does_not_drop_saved_report(self):
        self.bot.fail_admin_notifications = True
        self.app.set_state(101, self.app.STATE_WAITING_FOR_REPORT_EVIDENCE,
                           target='@example', username='@tester', note='Проблема с заказом',
                           evidence=[{'type':'text','data':'Текст'}])
        self.app.finalize_complaint(101, 101)
        self.assertIsNotNone(self.app.get_complaint_by_id(1))
        self.assertTrue(any(c[1] == 101 and 'Готово' in c[2] for c in self.bot.calls if c[0]=='send_message'))

    def test_report_submission_limit(self):
        for _ in range(5):
            self.insert(uid=101)
        self.app.set_state(101, self.app.STATE_WAITING_FOR_REPORT_EVIDENCE,
                           target='@example', username='@tester', note='Проблема с заказом',
                           evidence=[{'type':'text','data':'Текст'}])
        self.app.finalize_complaint(101, 101)
        self.assertEqual(self.app.db_execute('SELECT COUNT(*) FROM complaints',fetchone=True)[0][0], 5)
        self.assertTrue(any('Лимит' in c[2] for c in self.bot.calls if c[0]=='send_message'))

    def test_evidence_limit_and_max_length(self):
        self.app.set_state(101,self.app.STATE_WAITING_FOR_REPORT_EVIDENCE,
                           target='@example',note='Проблема с заказом',
                           evidence=[{'type':'text','data':'x'}] * self.app.MAX_EVIDENCE_ITEMS)
        self.app.handle_report_evidence(self.msg(text='extra'))
        self.assertEqual(len(self.app.get_state(101)['evidence']), self.app.MAX_EVIDENCE_ITEMS)
        self.app.get_state(101)['evidence'].clear()
        self.app.handle_report_evidence(self.msg(text='z'*(self.app.MAX_EVIDENCE_TEXT_LENGTH+1)))
        self.assertEqual(len(self.app.get_state(101)['evidence']), 0)

    def test_expiring_state_removes_old_evidence(self):
        self.app.set_state(101,self.app.STATE_WAITING_FOR_REPORT_EVIDENCE, evidence=[{'type':'text','data':'x'}])
        self.app.get_state(101)['_created'] = time.monotonic()-self.app.USER_STATE_TTL_SECONDS-5
        self.assertEqual(self.app.get_state(101), {})
        self.assertNotIn(101,self.app.user_states)

    def test_username_search_uses_only_verified_api_and_not_identity(self):
        self.app.USERNAME_LOOKUP_ENABLED = True
        self.app.USERNAME_LOOKUP_PLATFORMS = ('github',)
        self.app.USERNAME_LOOKUP_MAX_VARIANTS = 1
        response = types.SimpleNamespace(status_code=200,json=lambda:{'login':'example','html_url':'https://github.com/example'})
        with patch.object(self.app,'_username_request',return_value=response) as request:
            report = self.app.scan_public_profiles('@example')
            self.assertTrue(request.called)
        self.assertEqual(len(report['matches']),1)
        self.assertEqual(report['matches'][0]['username'],'example')
        self.assertIn('НЕ доказывает',self.app.format_public_profiles_text('@example'))

    def test_username_search_does_not_label_unconfirmed_users(self):
        self.app.USERNAME_LOOKUP_ENABLED = True
        self.app.USERNAME_LOOKUP_PLATFORMS = ('github',)
        self.app.USERNAME_LOOKUP_MAX_VARIANTS = 1
        response = types.SimpleNamespace(status_code=200,json=lambda:{'login':'someoneelse','html_url':'https://github.com/someoneelse'})
        with patch.object(self.app,'_username_request',return_value=response):
            report = self.app.scan_public_profiles('@example')
        self.assertEqual(report['matches'],[])

    def test_appeal_reaches_admin_and_never_automatically_changes_status(self):
        rid = self.insert(status='accepted')
        self.app.appeal_command(self.msg(text='/appeal'))
        self.app.handle_appeal_text(self.msg(text=f'#{rid} На самом деле это чужой аккаунт, запись ошибочна'))
        self.assertEqual(self.app.get_state(101), {})
        self.assertEqual(self.app.get_complaint_by_id(rid)['status'], 'accepted')
        notices = [c for c in self.bot.calls if c[0]=='send_message' and c[1]==9999]
        self.assertTrue(any('Запрос на пересмотр' in c[2] for c in notices))

    def test_data_deletion_request_without_complaint(self):
        self.app.appeal_command(self.msg(text='/appeal'))
        self.app.handle_appeal_text(self.msg(text='#0 Прошу удалить мои личные данные из бота.'))
        self.assertEqual(self.app.get_state(101), {})
        self.assertTrue(any('#0' in c[2] for c in self.bot.calls if c[0]=='send_message' and c[1]==9999))

    def test_group_messages_not_allowed(self):
        self.app.reject_group_messages(self.msg(text='/report',chat_type='supergroup'))
        self.assertTrue(any('личном чате' in c[2] for c in self.bot.calls if c[0]=='send_message'))

    def test_check_is_throttled(self):
        self.assertTrue(self.app.allow_check(101))
        self.assertFalse(self.app.allow_check(101))
        self.assertTrue(self.app.allow_check(102))

    def test_escaping_public_notes(self):
        self.insert(status='accepted',note='<b>fake</b>&more')
        value = self.app.format_check_text('@example')
        self.assertIn('&lt;b&gt;fake&lt;/b&gt;&amp;more',value)
        self.assertNotIn('<b>fake</b>', value)

    def test_admin_manual_record_cannot_open_self_chat(self):
        self.app.insert_manual_scammers(['@example'], 'Модерация')
        self.app.start_direct_chat(9999, 1)
        self.assertNotIn(9999,self.app.admin_chat_sessions)

    def test_concurrent_user_session_conflicts_are_blocked(self):
        c1 = self.insert(uid=101)
        c2 = self.insert(uid=102)
        self.app.start_direct_chat(9999,c1)
        self.app.start_direct_chat(9999,c2)
        self.assertEqual(self.app.admin_chat_sessions[9999],101)
        self.assertNotIn(102,self.app.admin_chat_sessions)


if __name__ == '__main__':
    unittest.main()
