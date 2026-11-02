"""Offline behavioural tests. All identities, message IDs and emoji IDs are synthetic."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import test_bot as legacy
from community_store import CommunityStore
from delivery import DeliveryWorker, broadcast_markup, classify_error
from message_content import capture_message, parse_buttons, send_content, utf16_length, verify_content_result, ContentMismatchError
from ui import UI, UIBot, ICON_CANDIDATES


class ApiError(Exception):
    def __init__(self, code, description, retry_after=None):
        super().__init__('Synthetic Telegram error')
        self.result_json = {'error_code':code, 'description':description}
        if retry_after is not None:
            self.result_json['parameters'] = {'retry_after':retry_after}


class CommunityTests(unittest.TestCase):
    setUp = legacy.AppTest.setUp
    tearDown = legacy.AppTest.tearDown
    user = legacy.AppTest.user
    msg = legacy.AppTest.msg
    callback = legacy.AppTest.callback
    insert = legacy.AppTest.insert

    @property
    def store(self):
        return self.app.community_store

    def seen(self, uid):
        self.app.remember_user_seen(self.user(uid), uid)

    def message(self, uid=101, text='Текст сообщения', mid=1):
        m = self.msg(uid=uid, text=text)
        m.message_id = mid
        m.entities = []
        return m

    def draft(self, uids=(101,), *, buttons=None):
        for uid in uids:
            self.seen(uid)
        jid = self.store.create_broadcast(9999)
        self.store.save_draft(jid,9999,payload=capture_message(self.message()), buttons=buttons or [])
        job = self.store.job(jid,9999)
        self.store.mark_preview(jid,9999,job['revision'])
        return jid

    def queued(self, uids=(101,)):
        jid = self.draft(uids)
        self.store.enqueue(jid,9999,self.store.job(jid,9999)['revision'])
        return jid

    def worker(self):
        return DeliveryWorker(self.store,self.bot.raw,self.app.types,self.app.ui,throttle=False)

    def ticket(self, owner=101, mid=1, subject='Вопрос о проверке'):
        self.seen(owner)
        return self.store.create_ticket(owner,subject,capture_message(self.message(owner,mid=mid)),owner,mid)[0]

    def test_buttons_send_native_colors_not_colored_unicode(self):
        ui=self.app.ui
        self.assertEqual(ui.button('Проверить',callback_data='menu:check').to_dict()['style'],'primary')
        self.assertEqual(ui.button('Отправить',callback_data='report:submit').to_dict()['style'],'success')
        self.assertEqual(ui.button('Отменить',callback_data='flow:cancel').to_dict()['style'],'danger')
        self.assertNotIn('style',ui.button('Назад',callback_data='menu:main').to_dict())

    def test_pack_is_resolved_and_ids_come_only_from_returned_set(self):
        self.app.ui.load_pack(self.bot.raw)
        ids={s['id'] for s in self.app.ui.catalog}
        button=self.app.ui.button('Проверить',callback_data='menu:check',icon='search')
        self.assertIn(button.to_dict()['icon_custom_emoji_id'],ids)
        self.assertNotIn('🔎',button.text)
        self.assertEqual(set(self.app.ui.mapping),set(ICON_CANDIDATES))

    def test_emoji_cache_and_assignments_survive_new_ui_instance(self):
        self.app.ui.load_pack(self.bot.raw)
        last=self.app.ui.catalog[-1]['id']
        self.app.ui.assign('search',last)
        other=UI(self.app.types,self.store)
        self.assertEqual(other.mapping['search']['id'],last)
        with self.assertRaises(ValueError):
            other.assign('search','88888888888888')
        with self.assertRaises(ValueError):
            other.assign('fake_role',last)

    def test_missing_pack_uses_text_not_unrelated_emojis(self):
        button=self.app.ui.button('[[icon:search]] Проверить',callback_data='menu:check')
        self.assertEqual(button.text,'Проверить')
        self.assertNotIn('icon_custom_emoji_id',button.to_dict())
        self.bot.send_message(101,'[[icon:search]] Проверить <аккаунт>')
        sent=self.bot.calls[-1]
        self.assertNotIn('[[icon:',sent[2])
        self.assertIn('&lt;аккаунт&gt;',sent[2])

    def test_ui_token_rendering_keeps_html_and_pack_id(self):
        self.app.ui.load_pack(self.bot.raw)
        self.bot.send_message(101,'[[icon:search]] <b>Поиск</b>',parse_mode='HTML')
        sent=self.bot.calls[-1]
        self.assertIn('<tg-emoji emoji-id=',sent[2])
        self.assertIn('<b>Поиск</b>',sent[2])

    def test_ui_permission_error_falls_back_without_breaking_buttons(self):
        self.app.ui.load_pack(self.bot.raw)
        self.bot.fail_next=ApiError(400,'CUSTOM_EMOJI_NOT_ALLOWED')
        markup=self.app.ui.keyboard([[self.app.ui.button('Поиск',callback_data='menu:check',icon='search')]])
        self.bot.send_message(101,'[[icon:search]] <b>Поиск</b>',parse_mode='HTML',reply_markup=markup)
        sent=self.bot.calls[-1]
        self.assertTrue(self.app.ui.premium_rejected)
        self.assertNotIn('tg-emoji',sent[2])
        self.assertNotIn('icon_custom_emoji_id',sent[3]['reply_markup'].to_dict()['inline_keyboard'][0][0])
        self.assertEqual(sent[3]['reply_markup'].to_dict()['inline_keyboard'][0][0]['style'],'primary')

    def test_ui_transport_error_is_not_retried_as_plain_text(self):
        self.bot.fail_next=TimeoutError('synthetic')
        with self.assertRaises(TimeoutError):
            self.bot.send_message(101,'[[icon:search]] Поиск')
        self.assertEqual(self.bot.calls,[])

    def test_photo_broadcast_button_opens_existing_check_flow(self):
        error=ApiError(400,'Bad Request: there is no text in the message to edit')
        with patch.object(self.bot.raw,'edit_message_text',side_effect=error):
            self.app.handle_callbacks(self.callback(data='menu:check'))
        self.assertEqual(self.app.get_state(101)['state'],self.app.STATE_WAITING_FOR_CHECK_TARGET)
        self.assertTrue(any(c[0]=='send_message' and 'Проверка аккаунта' in c[2] for c in self.bot.calls))

    def test_ui_pagination_arrow_has_visible_label(self):
        for i in range(8):
            self.insert(target=f'@demo_{i}',status='accepted')
        self.app.show_identity_page(9999)
        sent=[c for c in self.bot.calls if c[0]=='send_message'][-1]
        self.assertTrue(all(b.text for b in sent[3]['reply_markup'].buttons))

    def test_no_legacy_emoji_literals_in_active_app(self):
        source=(Path(__file__).resolve().parents[1]/'app.py').read_text()
        for glyph in ('🔎','📝','📂','🛠','❌','✅','✂️','🗑','🚨','🙋','🏷'):
            self.assertNotIn(glyph,source)

    def test_new_public_routes_visible_in_main_and_admin_routes_hidden(self):
        regular=self.app.build_main_menu(101)
        routes={b.callback_data for b in regular.buttons}
        self.assertIn('menu:support',routes)
        self.assertIn('menu:help',routes)
        self.assertNotIn('admin:panel',routes)
        admin={b.callback_data for b in self.app.build_admin_panel().buttons}
        self.assertTrue({'admin:broadcast','admin:support','admin:ui'}.issubset(admin))

    def test_native_entities_preserve_utf16_offsets_and_custom_ids(self):
        m=self.message(text='🙂 Привет 🚀')
        m.entities=[legacy.FakeEntity(type='bold',offset=3,length=6),
                    legacy.FakeEntity(type='custom_emoji',offset=10,length=2,custom_emoji_id='7000001')]
        payload=capture_message(m)
        self.assertEqual(payload['entities'],[e.to_dict() for e in m.entities])
        self.assertEqual(payload['text'],m.text)
        m.entities[0].offset=999
        self.assertEqual(payload['entities'][0]['offset'],3)
        send_content(self.bot.raw,self.app.types,101,payload)
        sent=self.bot.calls[-1]
        self.assertEqual(sent[2],'🙂 Привет 🚀')
        self.assertNotIn('parse_mode',sent[3])
        self.assertEqual(sent[3]['entities'][1].custom_emoji_id,'7000001')

    def test_literal_html_in_broadcast_is_not_reinterpreted(self):
        payload=capture_message(self.message(text='<b>текст</b> & <script>'))
        send_content(self.bot.raw,self.app.types,101,payload)
        self.assertEqual(self.bot.calls[-1][2],'<b>текст</b> & <script>')
        self.assertNotIn('parse_mode',self.bot.calls[-1][3])

    def test_media_caption_entities_roundtrip(self):
        m=self.message()
        m.content_type='photo'
        m.photo=[types.SimpleNamespace(file_id='SYNTHETIC_PHOTO_SMALL'),types.SimpleNamespace(file_id='SYNTHETIC_PHOTO_LARGE')]
        m.caption='Фото 🚀'
        m.caption_entities=[legacy.FakeEntity(type='custom_emoji',offset=5,length=2,custom_emoji_id='7000001')]
        m.has_media_spoiler=True
        payload=capture_message(m)
        send_content(self.bot.raw,self.app.types,101,payload)
        sent=self.bot.calls[-1]
        self.assertEqual(sent[:3],('send_photo',101,'SYNTHETIC_PHOTO_LARGE'))
        self.assertTrue(sent[3]['has_spoiler'])
        self.assertEqual(sent[3]['caption_entities'][0].offset,5)

    def test_invalid_entity_offsets_rejected_before_queueing(self):
        m=self.message(text='test')
        m.entities=[legacy.FakeEntity(type='bold',offset=2,length=10)]
        with self.assertRaises(ValueError):capture_message(m)

    def test_telegram_text_limit_counts_surrogate_pairs(self):
        self.assertEqual(utf16_length('🙂'),2)
        with self.assertRaises(ValueError):capture_message(self.message(text='🙂'*2049))
        self.assertEqual(len(capture_message(self.message(text='a'*4096))['text']),4096)

    def test_album_rejected_in_broadcast_but_supported_as_ticket_messages(self):
        m=self.message()
        m.media_group_id='synthetic_group'
        with self.assertRaises(ValueError):capture_message(m)
        self.assertEqual(capture_message(m,allow_album=True)['text'],m.text)

    def test_button_parser_links_routes_colors_and_icons(self):
        rows=parse_buttons('Проверить | menu:check | blue | search\nПомощь | menu:support | success | support\nСайт | https://example.com',ICON_CANDIDATES)
        self.assertEqual(rows[0]['style'],'primary')
        self.assertEqual(rows[1]['callback_data'],'menu:support')
        self.assertEqual(rows[2]['url'],'https://example.com')
        markup=broadcast_markup(self.app.ui,rows).to_dict()['inline_keyboard']
        self.assertEqual(markup[-1][0]['callback_data'],'menu:notifications')

    def test_broadcast_buttons_reject_arbitrary_internal_commands(self):
        for target in ('admin:approve:1:0','bc:send:1:1','sup:ticket:9:0','javascript:alert(1)','https://name:password'+chr(64)+'example.com','https://example.com/ bad'):
            with self.subTest(target=target),self.assertRaises(ValueError):
                parse_buttons('Кнопка | '+target,ICON_CANDIDATES)

    def test_button_length_count_and_color_limits(self):
        for value in ('a'*61+' | menu:check','X | menu:check | purple','X | menu:check | primary | nonexistent','\n'.join(['X | menu:check']*9)):
            with self.assertRaises(ValueError):parse_buttons(value,ICON_CANDIDATES)

    def test_migration_is_additive_and_repeatable(self):
        rid=self.insert(status='accepted',note='Сохраняем данные')
        self.app.identity_store.manual_link(rid,606060, actor_id=9999, evidence='Проверяемое доказательство #100')
        self.store.initialize()
        self.store.initialize()
        row=self.app.get_complaint_by_id(rid)
        self.assertEqual(row['note'],'Сохраняем данные')
        self.assertEqual(row['target_user_id'],606060)
        self.assertEqual(row['status'],'accepted')

    def test_audience_excludes_group_chats_and_accused_accounts(self):
        self.seen(101)
        self.app.remember_user_seen(self.user(102),-10)
        self.insert(uid=103,target='@someone')
        self.app.identity_store.observe(104,'@someone',source='bot_message')
        self.assertEqual(self.store.audience_count(),1)

    def test_unsubscribe_survives_later_interactions_and_block_unblock(self):
        self.seen(101)
        self.store.set_notifications(101,False)
        self.store.mark_blocked(101)
        self.seen(101)
        self.assertFalse(self.store.notifications(101))
        self.assertEqual(self.store.audience_count(),0)
        self.store.set_notifications(101,True)
        self.assertEqual(self.store.audience_count(),1)

    def test_preview_is_required_and_edit_invalidates_it(self):
        self.seen(101)
        jid=self.store.create_broadcast(9999)
        self.store.save_draft(jid,9999,payload=capture_message(self.message()))
        revision=self.store.job(jid,9999)['revision']
        with self.assertRaises(ValueError):self.store.enqueue(jid,9999,revision)
        self.store.mark_preview(jid,9999,revision)
        self.store.save_draft(jid,9999,buttons=[])
        with self.assertRaises(ValueError):self.store.enqueue(jid,9999,revision)

    def test_preview_race_does_not_authorize_a_changed_draft(self):
        jid=self.draft()
        revision=self.store.job(jid,9999)['revision']
        self.store.save_draft(jid,9999,payload=capture_message(self.message(text='Изменённый текст')))
        self.assertFalse(self.store.mark_preview(jid,9999,revision))
        self.assertEqual(self.store.job(jid,9999)['preview_revision'],-1)

    def test_double_send_confirmation_never_creates_second_queue(self):
        jid=self.draft((101,102))
        revision=self.store.job(jid,9999)['revision']
        self.assertEqual(self.store.enqueue(jid,9999,revision),2)
        with self.assertRaises(ValueError):self.store.enqueue(jid,9999,revision)
        self.assertEqual(self.store.job(jid,9999)['total'],2)

    def test_all_broadcast_mutations_enforce_admin(self):
        jid=self.draft()
        actions=[lambda:self.store.create_broadcast(101),lambda:self.store.job(jid,101),
                 lambda:self.store.save_draft(jid,101,buttons=[]),lambda:self.store.mark_preview(jid,101,1),
                 lambda:self.store.enqueue(jid,101,1),lambda:self.store.control_job(jid,101,'cancel')]
        for action in actions:
            with self.assertRaises(PermissionError):action()

    def test_audience_snapshot_does_not_expand_mid_send(self):
        jid=self.queued((101,))
        self.seen(102)
        self.assertEqual(self.store.job(jid,9999)['total'],1)

    def test_unsubscribe_after_enqueue_skips_delivery(self):
        jid=self.queued()
        self.store.set_notifications(101,False)
        self.assertIsNone(self.store.claim_broadcast())
        self.assertEqual(self.store.job(jid,9999)['counts'],{'skipped':1})

    def test_claims_are_unique_and_inflight_is_not_completed(self):
        jid=self.queued()
        item=self.store.claim_broadcast()
        self.assertIsNotNone(item)
        self.assertIsNone(self.store.claim_broadcast())
        self.assertEqual(self.store.job(jid,9999)['status'],'running')

    def test_worker_success_records_telegram_message_id_and_finishes(self):
        jid=self.queued((101,102))
        worker=self.worker()
        self.assertTrue(worker.run_once())
        self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())
        job=self.store.job(jid,9999)
        self.assertEqual(job['counts'],{'sent':2})
        self.assertEqual(job['status'],'completed')
        rows=self.app.conn.execute('SELECT telegram_message_id FROM broadcast_deliveries').fetchall()
        self.assertTrue(all(r[0]>0 for r in rows))

    def test_worker_429_honors_retry_after_and_does_not_advance_recipient(self):
        jid=self.queued()
        self.bot.fail_next=ApiError(429,'Too Many Requests',retry_after=3)
        worker=self.worker()
        self.assertTrue(worker.run_once())
        self.assertEqual(self.store.job(jid,9999)['counts'],{'pending':1})
        self.assertFalse(worker.run_once())
        future=time.time()+10
        with patch('time.time',return_value=future):
            self.assertTrue(worker.run_once())
        self.assertEqual(self.store.job(jid,9999)['counts'],{'sent':1})

    def test_worker_forbidden_marks_blocked_and_continues(self):
        jid=self.queued((101,102))
        self.bot.fail_next=ApiError(403,'Forbidden: bot was blocked by user')
        worker=self.worker()
        worker.run_once();worker.run_once();worker.run_once()
        self.assertEqual(self.store.job(jid,9999)['counts'],{'blocked':1,'sent':1})
        self.assertEqual(self.store.audience_count(),1)

    def test_ambiguous_timeout_is_not_automatically_resent(self):
        jid=self.queued((101,102))
        self.bot.fail_next=TimeoutError('synthetic')
        worker=self.worker();worker.run_once()
        self.assertEqual(self.store.job(jid,9999)['status'],'paused')
        self.assertEqual(self.store.job(jid,9999)['counts'],{'pending':1,'uncertain':1})
        self.store.control_job(jid,9999,'resume')
        worker.run_once();worker.run_once()
        sends=[c[1] for c in self.bot.calls if c[0]=='send_message']
        self.assertEqual(sends,[102])

    def test_crash_recovery_pauses_ambiguous_and_keeps_sent(self):
        jid=self.queued((101,102,103))
        first=self.store.claim_broadcast()
        self.store.finish_broadcast(first,'sent',message_id=500)
        self.store.claim_broadcast()
        self.store.recover()
        job=self.store.job(jid,9999)
        self.assertEqual(job['status'],'paused')
        self.assertEqual(job['counts'],{'sent':1,'uncertain':1,'pending':1})

    def test_pause_resume_and_cancel_work_without_new_queue(self):
        jid=self.queued((101,102))
        self.store.control_job(jid,9999,'pause')
        self.assertIsNone(self.store.claim_broadcast())
        self.store.control_job(jid,9999,'resume')
        self.assertIsNotNone(self.store.claim_broadcast())
        self.store.control_job(jid,9999,'cancel')
        self.assertEqual(self.store.job(jid,9999)['counts'],{'inflight':1,'skipped':1})

    def test_failed_premium_broadcast_is_paused_without_plain_fallback(self):
        jid=self.queued((101,102))
        self.bot.fail_next=ApiError(400,'CUSTOM_EMOJI_NOT_ALLOWED')
        self.worker().run_once()
        job=self.store.job(jid,9999)
        self.assertEqual(job['status'],'paused')
        self.assertEqual(job['counts'],{'failed':1,'pending':1})
        self.assertEqual(self.bot.calls,[])

    def test_ticket_create_is_idempotent_for_same_telegram_message(self):
        tid=self.ticket()
        again,created=self.store.create_ticket(101,'Та же тема',capture_message(self.message()),101,1)
        self.assertFalse(created)
        self.assertEqual(again,tid)
        self.assertEqual(self.store.ticket(tid,101)['total'],1)

    def test_ticket_reply_duplicate_is_not_stored_twice(self):
        tid=self.ticket()
        payload=capture_message(self.message(mid=2))
        first,created=self.store.add_reply(tid,101,payload,101,2)
        second,created_again=self.store.add_reply(tid,101,payload,101,2)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first,second)

    def test_ticket_cross_user_access_is_rejected_for_every_operation(self):
        tid=self.ticket()
        mid=self.store.ticket(tid,101)['messages'][0]['id']
        for action in [lambda:self.store.ticket(tid,102),lambda:self.store.ticket_message(mid,102),
                       lambda:self.store.add_reply(tid,102,capture_message(self.message(102)),102,3),
                       lambda:self.store.close_ticket(tid,102),lambda:self.store.close_ticket(tid,102,reopen=True)]:
            with self.assertRaises(PermissionError):action()

    def test_ticket_user_cannot_spoof_source_chat(self):
        with self.assertRaises(ValueError):
            self.store.create_ticket(101,'Тема обращения',capture_message(self.message()),102,1)

    def test_ticket_admin_answer_changes_status_and_notifies_owner(self):
        tid=self.ticket()
        self.store.add_reply(tid,9999,capture_message(self.message(9999,text='Ответ поддержки',mid=9)),9999,9)
        self.assertEqual(self.store.ticket(tid,101)['status'],'waiting_user')
        rows=self.app.conn.execute('SELECT recipient_id FROM support_outbox ORDER BY id').fetchall()
        self.assertEqual([r[0] for r in rows],[9999,101])

    def test_ticket_close_reopen_does_not_delete_history(self):
        tid=self.ticket()
        self.assertTrue(self.store.close_ticket(tid,101))
        with self.assertRaises(ValueError):
            self.store.add_reply(tid,101,capture_message(self.message(mid=2)),101,2)
        self.assertFalse(self.store.close_ticket(tid,101))
        self.assertTrue(self.store.close_ticket(tid,101,reopen=True))
        self.store.add_reply(tid,101,capture_message(self.message(mid=2)),101,2)
        self.assertEqual(self.store.ticket(tid,101)['total'],2)

    def test_ticket_open_limit_and_daily_limit(self):
        for i in range(1,4):self.ticket(mid=i)
        with self.assertRaises(ValueError):self.ticket(mid=4)
        self.store.close_ticket(1,101)
        self.ticket(mid=4)
        self.store.close_ticket(2,101)
        self.ticket(mid=5)
        self.store.close_ticket(3,101)
        with self.assertRaises(ValueError):self.ticket(mid=6)

    def test_ticket_hourly_message_limit(self):
        tid=self.ticket()
        payload=capture_message(self.message())
        for mid in range(2,51):self.store.add_reply(tid,101,payload,101,mid)
        with self.assertRaises(ValueError):self.store.add_reply(tid,101,payload,101,51)

    def test_ticket_data_and_outbox_survive_reopening_connection(self):
        tid=self.ticket()
        self.app.conn.commit()
        connection=sqlite3.connect(self.app.DB_PATH)
        connection.row_factory=sqlite3.Row
        restored=CommunityStore(connection,threading.RLock(),9999)
        restored.initialize()
        self.assertEqual(restored.ticket(tid,101)['subject'],'Вопрос о проверке')
        self.assertEqual(restored.claim_support()['recipient_id'],9999)
        connection.close()

    def test_support_delivery_not_suppressed_by_news_unsubscribe(self):
        tid=self.ticket()
        self.store.set_notifications(101,False)
        self.store.add_reply(tid,9999,capture_message(self.message(9999,text='Помощь',mid=2)),9999,2)
        worker=self.worker();worker.run_once();worker.run_once()
        recipients=[c[1] for c in self.bot.calls if c[0]=='send_message']
        self.assertEqual(recipients,[9999,101])

    def test_support_notification_failure_keeps_the_ticket_and_history(self):
        tid=self.ticket()
        self.bot.fail_next=TimeoutError('synthetic')
        self.worker().run_once()
        ticket=self.store.ticket(tid,101)
        self.assertEqual(ticket['total'],1)
        self.assertEqual(ticket['messages'][0]['delivery_status'],'uncertain')

    def test_support_priority_and_native_entity_delivery(self):
        self.queued()
        m=self.message(text='🚀 Текст',mid=20)
        m.entities=[legacy.FakeEntity(type='custom_emoji',offset=0,length=2,custom_emoji_id='7000001')]
        tid,_=self.store.create_ticket(101,'Тема сообщения',capture_message(m),101,20)
        self.worker().run_once()
        sent=self.bot.calls[-1]
        self.assertEqual(sent[1],9999)
        self.assertEqual(sent[3]['entities'][0].custom_emoji_id,'7000001')
        self.assertNotIn('parse_mode',sent[3])

    def test_support_reply_via_native_telegram_reply(self):
        tid=self.ticket()
        self.worker().run_once()
        delivered=self.app.conn.execute('SELECT telegram_message_id FROM support_outbox').fetchone()[0]
        reply=self.message(9999,text='Ответ через Reply',mid=33)
        reply.reply_to_message=types.SimpleNamespace(message_id=delivered)
        self.assertTrue(self.app.community.wants_message(reply))
        self.app.community.handle_message(reply)
        self.assertEqual(self.store.ticket(tid,101)['total'],2)

    def test_user_cannot_reply_to_another_users_delivery_id(self):
        self.ticket();self.worker().run_once()
        delivered=self.app.conn.execute('SELECT telegram_message_id FROM support_outbox').fetchone()[0]
        reply=self.message(102,mid=33)
        reply.reply_to_message=types.SimpleNamespace(message_id=delivered)
        self.assertFalse(self.app.community.wants_message(reply))

    def test_support_available_without_required_channel_subscription(self):
        self.app.CHANNEL_USERNAMES=['@synthetic_channel']
        self.bot.member_status='left'
        self.app.handle_callbacks(self.callback(data='menu:support'))
        self.assertTrue(any('Поддержка' in c[2] for c in self.bot.calls if c[0]=='edit_message_text'))
        self.app.handle_callbacks(self.callback(data='sup:new'))
        self.assertEqual(self.app.get_state(101)['state'],'community_support_subject')

    def test_support_wizard_create_user_reply_admin_reply_and_close(self):
        self.app.handle_callbacks(self.callback(data='sup:new'))
        self.app.community_message(self.message(text='Не находится аккаунт',mid=1))
        self.app.community_message(self.message(text='Проверка возвращает ошибку',mid=2))
        self.assertEqual(self.store.ticket(1,101)['total'],1)
        self.app.community_message(self.message(text='Ошибка повторилась',mid=3))
        self.assertEqual(self.store.ticket(1,101)['total'],2)
        self.app.handle_callbacks(self.callback(uid=9999,data='sup:reply:1'))
        self.app.community_message(self.message(9999,text='Проверим ситуацию',mid=4))
        self.assertEqual(self.store.ticket(1,101)['status'],'waiting_user')
        self.app.handle_callbacks(self.callback(data='sup:close:1'))
        self.assertNotEqual(self.store.ticket(1,101)['status'],'closed')
        self.app.handle_callbacks(self.callback(data='sup:close_yes:1'))
        self.assertEqual(self.store.ticket(1,101)['status'],'closed')

    def test_broadcast_wizard_preview_confirmation_and_repeat_click(self):
        self.seen(101)
        self.app.handle_callbacks(self.callback(uid=9999,data='bc:new'))
        self.app.community_message(self.message(9999,text='Новость',mid=11))
        self.assertEqual(self.store.job(1,9999)['status'],'draft')
        self.app.handle_callbacks(self.callback(uid=9999,data='bc:buttons:1'))
        self.app.community_message(self.message(9999,text='Проверить | menu:check | primary | search',mid=12))
        self.app.handle_callbacks(self.callback(uid=9999,data='bc:preview:1'))
        job=self.store.job(1,9999)
        self.assertEqual(job['preview_revision'],job['revision'])
        self.assertEqual(job['status'],'draft')
        self.app.handle_callbacks(self.callback(uid=9999,data='bc:confirm:1'))
        self.assertEqual(self.store.job(1,9999)['status'],'draft')
        callback=f'bc:send:1:{job["revision"]}'
        self.app.handle_callbacks(self.callback(uid=9999,data=callback))
        total=self.store.job(1,9999)['total']
        self.app.handle_callbacks(self.callback(uid=9999,data=callback))
        self.assertEqual(self.store.job(1,9999)['total'],total)

    def test_preview_failure_never_allows_send(self):
        jid=self.draft()
        self.store.save_draft(jid,9999,payload=capture_message(self.message(text='Новое содержимое')))
        self.bot.fail_next=ApiError(400,'CUSTOM_EMOJI_NOT_ALLOWED')
        self.app.handle_callbacks(self.callback(uid=9999,data=f'bc:preview:{jid}'))
        self.assertEqual(self.store.job(jid,9999)['preview_revision'],-1)
        self.assertEqual(self.store.job(jid,9999)['status'],'draft')

    def test_regular_user_cannot_trigger_new_admin_routes(self):
        jid=self.draft()
        for route in ('admin:broadcast','admin:support','admin:ui','bc:new',f'bc:preview:{jid}',f'bc:send:{jid}:1','sup:list:adm:open:0'):
            self.app.handle_callbacks(self.callback(uid=101,data=route))
        self.assertEqual(self.store.job(jid,9999)['status'],'draft')
        notices=[c for c in self.bot.calls if c[0]=='answer_callback_query']
        self.assertTrue(all('Нет доступа' in c[2] for c in notices))

    def test_ticket_callback_cannot_leak_another_users_subject(self):
        tid=self.ticket(subject='PRIVATE_SYNTHETIC_SUBJECT')
        self.app.handle_callbacks(self.callback(uid=102,data=f'sup:ticket:{tid}:0'))
        self.assertFalse(any('PRIVATE_SYNTHETIC_SUBJECT' in c[2] for c in self.bot.calls if c[0]=='send_message'))

    def test_menu_navigation_exits_composer_without_dropping_saved_draft(self):
        jid=self.draft()
        self.app.set_state(9999,'community_bc_content',job_id=jid)
        self.app.handle_callbacks(self.callback(uid=9999,data='menu:list:0'))
        self.assertEqual(self.app.get_state(9999),{})
        self.assertEqual(self.store.job(jid,9999)['status'],'draft')

    def test_plain_username_message_runs_check_from_main_menu(self):
        self.app.fallback_text_handler(self.message(text='@example'))
        self.assertTrue(any('Проверка @example' in c[2] for c in self.bot.calls if c[0]=='send_message'))

    def test_membership_block_events_update_audience(self):
        self.seen(101)
        event=types.SimpleNamespace(chat=types.SimpleNamespace(id=101,type='private'),new_chat_member=types.SimpleNamespace(status='kicked'))
        self.app.chat_membership_changed(event)
        self.assertEqual(self.store.audience_count(),0)
        event.new_chat_member.status='member'
        self.app.chat_membership_changed(event)
        self.assertEqual(self.store.audience_count(),1)

    def test_known_error_codes_are_sanitized(self):
        error=ApiError(400,'Bad Request: secret-sensitive-user-text')
        self.assertEqual(classify_error(error),('content_rejected',0))
        self.assertEqual(classify_error(TimeoutError('secret')),('transport_ambiguous',0))


    def test_acknowledged_message_without_custom_emoji_is_not_accepted(self):
        payload={'kind':'text','text':'🚀 Привет','entities':[{'type':'custom_emoji','offset':0,'length':2,'custom_emoji_id':'7000001'}]}
        response=types.SimpleNamespace(message_id=501,text=payload['text'],entities=[])
        with self.assertRaises(ContentMismatchError) as raised:
            verify_content_result(response,payload)
        self.assertEqual(raised.exception.message_id,501)

    def test_acknowledged_message_without_button_style_is_not_accepted(self):
        payload=capture_message(self.message())
        wanted=self.app.ui.keyboard([[self.app.ui.button('Проверить',callback_data='menu:check')]])
        received=self.app.ui.keyboard([[self.app.ui.button('Проверить',callback_data='menu:check',style=None)]])
        response=types.SimpleNamespace(message_id=501,text=payload['text'],entities=[],reply_markup=received)
        with self.assertRaises(ContentMismatchError):verify_content_result(response,payload,wanted)

    def test_extra_telegram_detected_entities_do_not_break_delivery(self):
        payload={'kind':'text','text':'https://example.com','entities':[]}
        response=types.SimpleNamespace(message_id=501,text=payload['text'],entities=[legacy.FakeEntity(type='url',offset=0,length=19)])
        self.assertIs(verify_content_result(response,payload),response)

    def test_silent_format_loss_in_preview_does_not_authorize_broadcast(self):
        jid=self.draft()
        m=self.message(text='🚀 Привет')
        m.entities=[legacy.FakeEntity(type='custom_emoji',offset=0,length=2,custom_emoji_id='7000001')]
        self.store.save_draft(jid,9999,payload=capture_message(m))
        original=self.bot.raw.send_message
        def stripped(*args,**kwargs):
            result=original(*args,**kwargs)
            result.entities=[]
            return result
        with patch.object(self.bot.raw,'send_message',side_effect=stripped):
            self.app.handle_callbacks(self.callback(uid=9999,data=f'bc:preview:{jid}'))
        self.assertEqual(self.store.job(jid,9999)['preview_revision'],-1)

    def test_silent_format_loss_in_delivery_stops_without_resending(self):
        jid=self.queued((101,102))
        original=self.bot.raw.send_message
        def stripped(*args,**kwargs):
            result=original(*args,**kwargs)
            result.reply_markup=None
            return result
        with patch.object(self.bot.raw,'send_message',side_effect=stripped):
            self.worker().run_once()
        job=self.store.job(jid,9999)
        self.assertEqual(job['status'],'paused')
        self.assertEqual(job['last_error'],'formatting_not_preserved')
        row=self.app.conn.execute('SELECT telegram_message_id FROM broadcast_deliveries WHERE user_id=101').fetchone()
        self.assertGreater(row[0],0)
        self.assertEqual(len([c for c in self.bot.calls if c[0]=='send_message']),1)

    def test_navigation_clears_legacy_relay_before_support_composer(self):
        self.app.admin_chat_sessions.update({101:9999,9999:101})
        self.app.handle_callbacks(self.callback(data='sup:new'))
        self.assertFalse(self.app.admin_chat_sessions)
        self.assertEqual(self.app.get_state(101)['state'],'community_support_subject')

    def test_check_command_with_argument_exits_ticket_composer(self):
        self.app.set_state(101,'community_support_reply',ticket_id=7)
        self.app.check_command(self.message(text='/check @example'))
        self.assertEqual(self.app.get_state(101),{})

    def test_corrupt_ui_cache_does_not_stop_bot(self):
        self.store.set_setting('emoji_catalog','{"invalid":"cache"}')
        self.store.set_setting('emoji_overrides','[]')
        result=UI(self.app.types,self.store)
        self.assertEqual(result.mapping,{})


    def test_two_fast_support_messages_create_one_ticket(self):
        from concurrent.futures import ThreadPoolExecutor
        self.app.set_state(101,'community_support_body',subject='Один вопрос')
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures=[executor.submit(self.app.community_message,self.message(mid=mid)) for mid in (1,2)]
            for future in futures:future.result(timeout=3)
        self.assertEqual(self.app.conn.execute('SELECT COUNT(*) FROM support_tickets').fetchone()[0],1)
        self.assertEqual(self.store.ticket(1,101)['total'],2)

    def test_unknown_slash_text_in_broadcast_is_preserved(self):
        jid=self.store.create_broadcast(9999)
        self.app.set_state(9999,'community_bc_content',job_id=jid)
        self.app.community_message(self.message(9999,text='/example arbitrary text',mid=11))
        self.assertEqual(self.store.job(jid,9999)['payload']['text'],'/example arbitrary text')

    def test_every_supported_media_type_uses_native_delivery_method(self):
        from unittest.mock import MagicMock
        for kind in ('photo','video','animation','document','audio','voice','sticker','video_note'):
            with self.subTest(kind=kind):
                message=self.message()
                message.content_type=kind
                item=types.SimpleNamespace(file_id='SYNTHETIC_MEDIA')
                setattr(message,kind,[item] if kind=='photo' else item)
                message.caption='Тест 🚀'
                message.caption_entities=[legacy.FakeEntity(type='custom_emoji',offset=5,length=2,custom_emoji_id='7000001')]
                payload=capture_message(message)
                result=types.SimpleNamespace(message_id=500,caption=payload.get('caption',''),caption_entities=message.caption_entities)
                raw=types.SimpleNamespace()
                method=MagicMock(return_value=result)
                setattr(raw,'send_'+kind,method)
                send_content(raw,self.app.types,101,payload)
                args,kwargs=method.call_args
                self.assertEqual(args,(101,'SYNTHETIC_MEDIA'))
                self.assertNotIn('parse_mode',kwargs)
                if kind not in {'sticker','video_note'}:
                    self.assertEqual(kwargs['caption_entities'][0].custom_emoji_id,'7000001')


if __name__ == '__main__':
    unittest.main()
