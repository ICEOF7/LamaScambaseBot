"""R2 regressions: public cards, complete ID backlog, API-acknowledged appearance.

All IDs and credentials are synthetic. No Telegram request is sent.
"""
from __future__ import annotations

import asyncio
import html
import json
import re
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

import test_bot as base
from identity_maintenance import IdentityMaintenance
from report_view import excerpt
from telegram_identity import TelegramIdentity, TelegramResolver, IdentityRateLimited
from ui import UI


class ListIDHotfix(unittest.TestCase):
    setUp = base.AppTest.setUp
    tearDown = base.AppTest.tearDown
    insert = base.AppTest.insert
    callback = base.AppTest.callback
    user = base.AppTest.user

    def sent_text(self):
        return [c[2] for c in self.bot.calls if c[0] in {'send_message','edit_message_text'}][-1]

    def fake_resolver(self, fail=None):
        self.lookups = []
        def resolve(name):
            self.lookups.append(name)
            if fail:
                raise fail
            return TelegramIdentity(8000000 + len(self.lookups), name, (name,), '2026-10-29T00:00:00+00:00')
        self.app.identity_resolver = types.SimpleNamespace(enabled=True, resolve_username=resolve,
                                                         resolve_id=lambda uid: None)
        return self.app.identity_resolver

    def test_public_list_has_one_disclaimer_not_five_unknown_id_rows(self):
        for n in range(5):
            self.insert('@reportuser'+str(n), status='accepted')
        self.app.show_list_page(101, user_id=101)
        text = self.sent_text()
        self.assertNotIn('ID не установлен', text)
        self.assertEqual(text.count('Записи без ID'), 1)
        self.assertIn('Опубликованные жалобы', text)
        self.assertNotIn('\n\n\n', text)

    def test_confirmed_id_is_next_to_target_and_latest_alias_is_visible(self):
        rid = self.insert('@oldalias', status='accepted')
        self.app.identity_store.manual_link(rid, 80808080, actor_id=9999, evidence='Проверяемое доказательство #100')
        self.app.identity_store.observe(80808080, '@newalias')
        self.app.show_list_page(101, user_id=101)
        text = self.sent_text()
        self.assertRegex(text, r'@oldalias</b> · ID <code>80808080</code>')
        self.assertIn('Последний известный ник: <b>@newalias</b>', text)
        self.assertNotIn('Записи без ID', text)

    def test_candidate_ids_stay_private_in_text_buttons_and_search(self):
        rid = self.insert('@oldrecord', status='accepted')
        result = TelegramIdentity(80808081, '@oldrecord', ('@oldrecord',), '2026-10-29T00:00:00+00:00')
        self.app.identity_store.apply_scan('@oldrecord', result)
        self.app.show_list_page(101, user_id=101)
        self.assertNotIn('80808081', str(self.bot.calls))
        self.assertEqual(self.app.identity_store.public_matches('80808081')['total'], 0)
        self.assertIsNone(self.app.get_complaint_by_id(rid)['target_user_id'])

    def test_admin_gets_configuration_warning_and_direct_maintenance_controls(self):
        self.insert(status='accepted')
        self.app.show_list_page(9999, user_id=9999)
        self.assertIn('ID накапливаются', self.sent_text())
        buttons = self.bot.calls[-1][3]['reply_markup'].buttons
        self.assertIn('admin:identity:list:0', [b.callback_data for b in buttons])
        self.assertIn('admin:ui', [b.callback_data for b in buttons])
        self.bot.calls.clear()
        self.app.show_list_page(101, user_id=101)
        self.assertNotIn('TELEGRAM_API_ID', self.sent_text())
        self.assertFalse(any((b.callback_data or '').startswith('admin:') for b in self.bot.calls[-1][3]['reply_markup'].buttons))

    def test_id_confirmation_button_is_green(self):
        button = self.app.ui.button('Подтвердить связь', callback_data='admin:identity:approve:1:0')
        self.assertEqual(button.style, 'success')

    def test_admin_controls_require_matching_private_chat(self):
        self.insert(status='accepted')
        self.app.show_list_page(101, user_id=9999)
        self.assertNotIn('Администратору', self.sent_text())

    def test_old_page_clamps_to_last_existing_page(self):
        for n in range(6):
            self.insert('@listing'+str(n), status='accepted')
        self.app.show_list_page(101, page=999, user_id=101)
        self.assertIn('Страница <b>2/2</b>', self.sent_text())
        self.assertIn('@listing0', self.sent_text())

    def test_list_never_resolves_or_links_any_id_on_view(self):
        self.insert(status='accepted')
        self.fake_resolver(AssertionError('Public view made a network call'))
        self.app.show_list_page(101, user_id=101)
        self.assertEqual(self.lookups, [])

    def test_long_text_is_trimmed_at_whole_word_and_whitespace_collapsed(self):
        self.assertEqual(excerpt('one  two\nthree four', limit=8), 'one two…')
        self.assertEqual(excerpt('one two', limit=7), 'one two')
        self.assertEqual(excerpt('x'*1000), 'Длинное описание — открой запись целиком.')

    def test_user_html_and_icon_tokens_cannot_inject_ui_markup(self):
        self.app.ui.load_pack(self.bot.raw)
        self.insert(status='accepted', note='<b>bad</b> [[icon:admin]] & text')
        self.app.show_list_page(101, user_id=101)
        text = self.sent_text()
        self.assertIn('&lt;b&gt;bad&lt;/b&gt;', text)
        self.assertIn('&#91;&#91;icon:admin]]', text)
        self.assertNotIn('<b>bad</b>', text)

    def test_public_text_is_within_telegram_limit_with_astral_notes_and_icons(self):
        self.app.ui.load_pack(self.bot.raw)
        for n in range(5):
            self.insert('@listing'+str(n), status='accepted', note='🚀 <&> '*400)
        self.app.show_list_page(101, user_id=101)
        text = html.unescape(re.sub('<[^>]*>', '', self.sent_text()))
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 4096)

    def test_detail_buttons_are_blue_and_page_routes_survive(self):
        rid = self.insert(status='accepted')
        self.app.show_list_page(101, user_id=101)
        button = next(b for b in self.bot.calls[-1][3]['reply_markup'].buttons if b.callback_data.startswith('list:detail:'))
        self.assertEqual(button.style, 'primary')
        self.app.handle_callbacks(self.callback(data=button.callback_data))
        self.assertIn(f'Жалоба #{rid}', self.sent_text())

    def test_scan_prioritizes_new_then_newest_published_records(self):
        self.insert('@pendingolder', status='review')
        self.insert('@publishedold', status='accepted')
        self.insert('@publishednew', status='accepted')
        urgent = self.insert('@urgentnew', status='review')
        self.app.db_execute("UPDATE complaints SET identity_origin='new' WHERE id=?", (urgent,), commit=True)
        names = [r['target'] for r in self.app.identity_store.scan_targets(4)]
        self.assertEqual(names, ['@urgentnew','@publishednew','@publishedold','@pendingolder'])

    def test_legacy_names_without_at_and_spaces_are_grouped_without_rewriting(self):
        first = self.insert(' LegacyName ', status='accepted')
        second = self.insert('@legacyname', status='accepted')
        self.fake_resolver()
        result = self.app.run_identity_scan_batch()
        self.assertEqual(self.lookups, ['@legacyname'])
        self.assertEqual(result['candidates'], 2)
        self.assertEqual(self.app.get_complaint_by_id(first)['target'], ' LegacyName ')
        self.assertIsNone(self.app.get_complaint_by_id(second)['target_user_id'])
        self.assertEqual(self.app.identity_store.public_matches('@legacyname')['total'], 2)

    def test_invalid_legacy_target_advances_queue_instead_of_looping(self):
        self.insert(' bad name ', status='accepted')
        self.fake_resolver()
        result = self.app.run_identity_scan_batch()
        self.assertEqual(result['targets'], 1)
        self.assertEqual(self.lookups, [])
        self.assertEqual(self.app.identity_store.summary()['unscanned'], 0)

    def test_full_backlog_drains_with_short_delay_and_candidates_require_approval(self):
        for n in range(11):
            self.insert('@backlog'+str(n), status='accepted')
        self.fake_resolver()
        worker = self.app.identity_worker
        self.assertEqual(worker.run_once(), 10)
        self.assertEqual(len(self.lookups), 4)
        self.assertEqual(worker.run_once(), 10)
        self.assertEqual(worker.run_once(), 1800)
        self.assertEqual(len(self.lookups), 11)
        summary = self.app.identity_store.summary()
        self.assertEqual(summary['unscanned'], 0)
        self.assertEqual(summary['candidates'], 11)
        self.assertEqual(summary['linked'], 0)
        self.assertEqual(worker.status()['state'], 'idle')

    def test_completed_candidates_not_rescanned_on_new_worker(self):
        self.insert('@onelegacy', status='accepted')
        self.fake_resolver()
        self.app.identity_worker.run_once()
        newer = IdentityMaintenance(resolver=lambda:self.app.identity_resolver,
            identities=self.app.identity_store, settings=self.app.community_store,
            scan=self.app.run_identity_scan_batch, refresh=self.app.run_identity_refresh_batch,
            lock=self.app.IDENTITY_JOB_LOCK, wake=threading.Event(), stop_event=threading.Event())
        newer.run_once()
        self.assertEqual(self.lookups, ['@onelegacy'])

    def test_transient_error_keeps_record_pending_and_backoff_prevents_hammering(self):
        self.insert(status='accepted')
        self.fake_resolver(ConnectionError('sensitive details should never enter the status'))
        worker = self.app.identity_worker
        clock = [100.0]
        worker.clock = lambda: clock[0]
        self.assertEqual(worker.run_once(), 60)
        self.assertEqual(worker.run_once(), 60)
        self.assertEqual(len(self.lookups), 1)
        self.assertNotIn('sensitive', json.dumps(worker.status()))
        self.assertEqual(self.app.identity_store.summary()['unscanned'], 1)
        clock[0] += 60
        self.assertEqual(worker.run_once(), 120)

    def test_flood_wait_has_exact_retry_and_does_not_consume_record(self):
        self.insert(status='accepted')
        self.fake_resolver(IdentityRateLimited(retry_after=720))
        worker = self.app.identity_worker
        worker.clock = lambda:100.0
        self.assertEqual(worker.run_once(), 720)
        self.app.IDENTITY_SCAN_WAKE.set()
        self.assertEqual(worker.run_once(), 720)
        self.assertEqual(len(self.lookups), 1)
        self.assertEqual(worker.status()['state'], 'rate_limited')
        self.assertEqual(self.app.identity_store.summary()['unscanned'], 1)

    def test_pause_is_persistent_and_nonadmin_cannot_resume_or_pause(self):
        self.insert(status='accepted')
        self.fake_resolver()
        self.app.handle_callbacks(self.callback(uid=101,data='admin:identity:pause'))
        self.assertFalse(self.app.identity_worker.paused)
        self.app.handle_callbacks(self.callback(uid=9999,data='admin:identity:pause'))
        self.assertTrue(self.app.identity_worker.paused)
        self.app.identity_worker.run_once()
        self.assertEqual(self.lookups, [])
        self.app.handle_callbacks(self.callback(uid=101,data='admin:identity:scan'))
        self.assertTrue(self.app.identity_worker.paused)
        self.app.identity_worker.resume()
        self.app.identity_worker.run_once()
        self.assertEqual(len(self.lookups), 1)

    def test_cooldown_survives_worker_recreation(self):
        self.insert(status='accepted')
        self.fake_resolver(IdentityRateLimited(retry_after=720))
        self.app.identity_worker.run_once()
        newer = IdentityMaintenance(resolver=lambda:self.app.identity_resolver,
            identities=self.app.identity_store, settings=self.app.community_store,
            scan=self.app.run_identity_scan_batch, refresh=self.app.run_identity_refresh_batch,
            lock=self.app.IDENTITY_JOB_LOCK, wake=threading.Event(), stop_event=threading.Event())
        self.assertGreater(newer.run_once(), 700)
        self.assertEqual(len(self.lookups), 1)

    def test_pause_does_not_discard_persisted_flood_wait(self):
        self.insert(status='accepted')
        self.fake_resolver(IdentityRateLimited(retry_after=720))
        worker = self.app.identity_worker
        worker.run_once()
        value = self.app.community_store.setting('identity_retry_at')
        worker.pause()
        worker.resume()
        self.assertEqual(self.app.community_store.setting('identity_retry_at'), value)
        self.assertGreater(worker.run_once(), 700)
        self.assertEqual(len(self.lookups), 1)
        self.assertEqual(worker.status()['state'], 'rate_limited')

    def test_no_credentials_means_explicit_disabled_state_not_success(self):
        self.app.identity_worker.run_once()
        self.assertEqual(self.app.identity_worker.status()['state'], 'disabled')
        self.assertIn('Накопление через Telegram включено', self.app.identity_status_text())

    def test_worker_start_is_idempotent_and_shutdown_joins(self):
        self.fake_resolver()
        worker = self.app.identity_worker
        try:
            worker.start()
            thread = worker.thread
            worker.start()
            self.assertIs(thread, worker.thread)
        finally:
            worker.stop()
        self.assertFalse(worker.thread.is_alive())

    def test_emojis_retry_after_failure_without_losing_existing_cache(self):
        self.app.ui.load_pack(self.bot.raw)
        old = self.app.ui.mapping.copy()
        with patch.object(self.bot.raw, 'get_sticker_set', side_effect=ConnectionError('private detail')):
            self.assertEqual(self.app.community.refresh_emoji_pack(), 60)
        self.assertEqual(self.app.ui.mapping, old)
        self.assertNotIn('private detail', self.app.ui.last_error)
        self.assertEqual(self.app.community.refresh_emoji_pack(), 3600)
        self.assertEqual(self.app.ui.last_error, '')

    def test_emoji_pack_retry_respects_telegram_retry_after(self):
        error = RuntimeError('synthetic')
        error.result_json = {'error_code':429, 'parameters':{'retry_after':800}}
        with patch.object(self.bot.raw, 'get_sticker_set', side_effect=error):
            self.assertEqual(self.app.community.refresh_emoji_pack(), 800)

    def test_ui_acknowledges_custom_entity_not_just_plain_glyph(self):
        self.app.ui.load_pack(self.bot.raw)
        ui = self.app.ui
        item = ui.mapping['search']
        text = ui.render('[[icon:search]] Проверка')
        message = types.SimpleNamespace(message_id=1, entities=[], reply_markup=None)
        ui.observe_result(message,text)
        self.assertEqual(ui.delivery_status['emoji'], 'missing')
        message.entities = [base.FakeEntity(type='custom_emoji', offset=0, length=2, custom_emoji_id=item['id'])]
        ui.observe_result(message,text)
        self.assertEqual(ui.delivery_status['emoji'], 'confirmed')

    def test_duplicate_emoji_occurrences_are_all_checked(self):
        self.app.ui.load_pack(self.bot.raw)
        ui = self.app.ui
        item = ui.mapping['search']
        message = types.SimpleNamespace(message_id=1, entities=[{'type':'custom_emoji','custom_emoji_id':item['id']}])
        ui.observe_result(message,ui.render('[[icon:search]] [[icon:search]]'))
        self.assertEqual(ui.delivery_status['emoji'], 'missing')

    def test_ui_missing_icons_are_reported_without_duplicate_send(self):
        self.app.ui.load_pack(self.bot.raw)
        self.bot.send_message(101,'[[icon:search]] Проверка')
        self.assertEqual(len([c for c in self.bot.calls if c[0]=='send_message']), 1)
        self.assertEqual(self.app.ui.delivery_status['emoji'], 'missing')

    def test_ui_button_style_loss_is_reported(self):
        ui = self.app.ui
        markup = ui.keyboard([[ui.button('Проверить',callback_data='menu:check',style='primary')]])
        old = ui.keyboard([[ui.button('Проверить',callback_data='menu:check',style=None)]])
        msg = types.SimpleNamespace(message_id=1,entities=[],reply_markup=old)
        ui.observe_result(msg,'Текст',markup)
        self.assertEqual(ui.delivery_status['buttons'], 'missing')
        msg.reply_markup=markup
        ui.observe_result(msg,'Текст',markup)
        self.assertEqual(ui.delivery_status['buttons'], 'confirmed')

    def test_diagnostic_failure_never_retries_acknowledged_message(self):
        with patch.object(self.app.ui, 'observe_result', side_effect=RuntimeError('synthetic')):
            result = self.bot.send_message(101, 'Проверка')
        self.assertTrue(result.message_id)
        self.assertEqual(len([c for c in self.bot.calls if c[0]=='send_message']), 1)
        self.assertEqual(self.app.ui.delivery_status['emoji'], 'unreadable')

    def test_admin_ui_probe_goes_only_to_administrator(self):
        self.app.handle_callbacks(self.callback(uid=101,data='admin:ui:probe'))
        self.assertEqual([c for c in self.bot.calls if c[0]=='send_message'], [])
        self.app.handle_callbacks(self.callback(uid=9999,data='admin:ui:probe'))
        sends = [c for c in self.bot.calls if c[0]=='send_message']
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][1], 9999)
        buttons = sends[0][3]['reply_markup'].buttons
        self.assertEqual([b.style for b in buttons],['primary','success','danger'])

    def test_appearance_distinguishes_enabled_from_verified(self):
        self.app.community.ui_screen(9999)
        text = self.sent_text()
        self.assertIn('ещё не проверено',text)
        self.assertIn('включено настройкой',text)
        self.assertIn('Фактическая отправка',text)

    def test_foreign_pack_is_rejected_and_cached_mapping_preserved(self):
        self.app.ui.load_pack(self.bot.raw)
        mapping = self.app.ui.mapping.copy()
        foreign = self.bot.raw.get_sticker_set('ignored')
        foreign.name = 'OtherPack'
        with patch.object(self.bot.raw,'get_sticker_set',return_value=foreign):
            with self.assertRaises(ValueError):
                self.app.ui.load_pack(self.bot.raw)
        self.assertEqual(self.app.ui.mapping,mapping)


class ResolverContractSynthetic(unittest.TestCase):
    def setUp(self):
        self.calls = []
        class User:
            def __init__(self, uid, username, bot=False, usernames=()):
                self.id,self.username,self.bot,self.usernames=uid,username,bot,usernames
        self.User = User
        class FloodWaitError(Exception):
            def __init__(self,seconds): self.seconds=seconds
        self.FloodWait = FloodWaitError
        class UsernameInvalidError(Exception): pass
        class UsernameNotOccupiedError(Exception): pass
        self.NotOccupied=UsernameNotOccupiedError
        class ResolveUsernameRequest:
            def __init__(self,username): self.username=username
        self.Request=ResolveUsernameRequest
        owner=self
        class Client:
            def __init__(self,*args,**kw): pass
            async def connect(self): owner.calls.append('connect')
            async def is_user_authorized(self): return True
            async def get_me(self):
                if getattr(owner,'auth_error',None): raise owner.auth_error
                return User(1234567,'syntheticbot',bot=True)
            async def __call__(self,request):
                owner.calls.append(request)
                if getattr(owner,'lookup_error',None): raise owner.lookup_error
                return owner.response
            async def get_entity(self,reference):
                raise AssertionError('username path used entity cache')
            async def disconnect(self): owner.calls.append('disconnect')
        module=types.ModuleType('telethon'); module.TelegramClient=Client
        err=types.ModuleType('telethon.errors')
        err.FloodWaitError,err.UsernameInvalidError,err.UsernameNotOccupiedError=FloodWaitError,UsernameInvalidError,UsernameNotOccupiedError
        tl=types.ModuleType('telethon.tl.types'); tl.User=User; tl.PeerUser=lambda uid:uid
        contacts=types.ModuleType('telethon.tl.functions.contacts'); contacts.ResolveUsernameRequest=ResolveUsernameRequest
        self.patch=patch.dict(sys.modules,{'telethon':module,'telethon.errors':err,'telethon.tl.types':tl,'telethon.tl.functions.contacts':contacts})
        self.patch.start()
        self.resolver=TelegramResolver('1','synthetic_hash',str(1234567)+':'+'X'*35,session_path='/tmp/lamascambase-synthetic-r2',min_interval=0)
        self.response=types.SimpleNamespace(peer=types.SimpleNamespace(user_id=10101),users=[User(10101,'sampleuser')])

    def tearDown(self): self.patch.stop()

    def test_username_uses_resolve_rpc_and_exact_returned_peer(self):
        self.response.users.insert(0,self.User(20202,'notthesame'))
        identity=asyncio.run(self.resolver._fetch('@sampleuser'))
        self.assertEqual(identity.user_id,10101)
        request=next(x for x in self.calls if isinstance(x,self.Request))
        self.assertEqual(request.username,'sampleuser')
        self.assertEqual(self.calls[-1],'disconnect')

    def test_bot_accounts_are_valid_telegram_users(self):
        self.response.users[0].bot=True
        identity=asyncio.run(self.resolver._fetch('@sampleuser'))
        self.assertEqual(identity.user_id,10101)

    def test_changed_username_is_not_accepted_as_previous_owner(self):
        self.response.users[0].username='renamed'
        self.assertIsNone(asyncio.run(self.resolver._fetch('@sampleuser')))

    def test_channel_peer_does_not_become_user_identity(self):
        self.response.peer=types.SimpleNamespace(channel_id=10101)
        self.assertIsNone(asyncio.run(self.resolver._fetch('@sampleuser')))

    def test_not_found_is_different_from_transport_or_value_error(self):
        self.lookup_error=self.NotOccupied()
        self.assertIsNone(asyncio.run(self.resolver._fetch('@sampleuser')))
        self.lookup_error=ValueError('synthetic transport decode issue')
        with self.assertRaises(ValueError):
            asyncio.run(self.resolver._fetch('@sampleuser'))

    def test_flood_wait_during_authorization_is_also_honored(self):
        self.auth_error=self.FloodWait(93)
        with self.assertRaises(IdentityRateLimited) as result:
            asyncio.run(self.resolver._fetch('@sampleuser'))
        self.assertEqual(result.exception.retry_after,93)
        self.assertGreater(self.resolver._cooldown_until,time.monotonic())
        self.assertEqual(self.calls[-1],'disconnect')

    def test_active_aliases_are_normalized_and_deduplicated(self):
        entity=self.User(10101,'SampleUser',usernames=[types.SimpleNamespace(username='sampleuser',active=True),
                       types.SimpleNamespace(username='OldName',active=False),types.SimpleNamespace(username='OtherName',active=True)])
        self.assertEqual(self.resolver._extract_user(entity).usernames,('@sampleuser','@othername'))

    def test_missing_or_zero_api_id_does_not_claim_enabled(self):
        self.assertFalse(TelegramResolver('0','hash','synthetic').enabled)
        self.assertFalse(TelegramResolver('  ','hash','synthetic').enabled)
        self.assertTrue(TelegramResolver(' 12 ',' hash ','synthetic').enabled)


if __name__=='__main__':
    unittest.main()
