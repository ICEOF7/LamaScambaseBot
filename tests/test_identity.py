"""ID/username history regression tests. Uses only synthetic users and local SQLite."""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

import test_bot


class TelegramIdentityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_bot.AppTest(methodName='test_target_is_strictly_validated')
        self.fixture.setUp()
        self.app = self.fixture.app
        self.bot = self.fixture.bot

    def tearDown(self):
        self.fixture.tearDown()

    def tg_identity(self, user_id, *names):
        from telegram_identity import TelegramIdentity
        return TelegramIdentity(user_id, names[0] if names else None,
                                tuple(names), datetime.now(timezone.utc).isoformat())

    def test_stable_id_persists_across_username_change(self):
        rid = self.fixture.insert(target='@oldname', status='accepted')
        self.app.identity_store.manual_link(rid, 661234567, actor_id=9999, evidence='Проверяемое доказательство #100')
        self.app.identity_store.observe(661234567, '@oldname', source='mtproto')
        self.app.identity_store.observe(661234567, '@newname', source='mtproto')
        old_result = self.app.format_check_text('@oldname')
        new_result = self.app.format_check_text('@newname')
        by_id = self.app.format_check_text('661234567')
        for result in (old_result, new_result, by_id):
            self.assertIn(f'#{rid}', result)
            self.assertIn('661234567', result)
        self.assertIn('Исторический ник', old_result)
        self.assertIn('другому человеку', old_result)
        self.assertIn('Найдено по истории ников', new_result)

    def test_username_reuse_does_not_transfer_reports_to_new_owner(self):
        rid = self.fixture.insert('@previous', status='accepted')
        self.app.identity_store.manual_link(rid, 44556677, actor_id=9999, evidence='Проверяемое доказательство #100')
        self.app.identity_store.observe(44556677, '@previous', source='mtproto')
        self.app.identity_store.observe(44556677, '@changed', source='mtproto')
        self.app.identity_store.observe(99887766, '@previous', source='mtproto')
        self.assertEqual(self.app.identity_store.public_matches('99887766')['total'], 0)
        self.assertEqual(self.app.identity_store.public_matches('44556677')['total'], 1)
        text = self.app.format_check_text('@previous')
        self.assertIn('Исторический ник', text)
        self.assertIn(f'#{rid}', text)
        self.assertNotIn('99887766', text)

    def test_legacy_scan_creates_candidate_not_public_id(self):
        rid = self.fixture.insert('@legacyabc', status='accepted')
        result = self.app.identity_store.apply_scan('@legacyabc',
                                                    self.tg_identity(777888999, '@legacyabc'))
        self.assertEqual(result['candidates'], 1)
        row = self.app.get_complaint_by_id(rid)
        self.assertIsNone(row['target_user_id'])
        self.assertEqual(row['candidate_user_id'], 777888999)
        self.assertEqual(self.app.identity_store.public_matches('777888999')['total'], 0)
        self.assertTrue(self.app.identity_store.approve_candidate(rid, actor_id=9999, evidence='Проверяемое доказательство #100'))
        self.assertEqual(self.app.identity_store.public_matches('777888999')['total'], 1)
        self.assertEqual(self.app.get_complaint_by_id(rid)['identity_source'], 'admin_evidence')

    def test_username_resolve_requires_moderator_evidence_even_for_fresh_report(self):
        rid = self.fixture.insert('@freshuser', status='review')
        self.app.db_execute('UPDATE complaints SET identity_origin=? WHERE id=?',
                            ('new', rid), commit=True)
        entity = self.tg_identity(123987456, '@freshuser')
        self.app.identity_resolver = types.SimpleNamespace(
            enabled=True, resolve_username=lambda u: entity)
        result = self.app.run_identity_scan_batch()
        self.assertEqual(result['linked'], 0)
        self.assertEqual(result['candidates'], 1)
        row = self.app.get_complaint_by_id(rid)
        self.assertIsNone(row['target_user_id'])
        self.assertTrue(self.app.identity_store.reviewed_link(rid,123987456,9999,'Подтверждение по переписке #100'))
        self.assertEqual(row['status'], 'review')
        self.assertEqual(self.app.identity_store.public_matches('123987456')['total'], 0)
        self.app.change_complaint_status(rid, 'accepted')
        self.assertEqual(self.app.identity_store.public_matches('123987456')['total'], 1)

    def test_scan_rejects_mismatching_current_owner(self):
        rid = self.fixture.insert('@nameone', status='accepted')
        incorrect = self.tg_identity(123987456, '@nametwo')
        result = self.app.identity_store.apply_scan('@nameone', incorrect)
        self.assertEqual(result['unavailable'], 1)
        self.assertIsNone(self.app.get_complaint_by_id(rid)['candidate_user_id'])
        self.assertEqual(self.app.identity_store.public_matches('123987456')['total'], 0)

    def test_rejected_candidate_is_not_rescanned_until_reset(self):
        rid = self.fixture.insert('@oldusername', status='accepted')
        self.app.identity_store.apply_scan('@oldusername', self.tg_identity(123987456, '@oldusername'))
        self.assertTrue(self.app.identity_store.reject_candidate(rid, actor_id=9999, evidence='Проверяемое основание #100'))
        self.assertFalse(self.app.identity_store.approve_candidate(rid, actor_id=9999, evidence='Проверяемое доказательство #100'))
        self.assertEqual(self.app.identity_store.scan_targets(), [])
        self.assertEqual(self.app.identity_store.summary()['unresolved'], 1)
        self.assertTrue(self.app.identity_store.reset_scan(rid, actor_id=9999, evidence='Проверяемое основание #100'))
        self.assertEqual(len(self.app.identity_store.scan_targets()), 1)

    def test_admin_identity_queue_and_access_control(self):
        rid = self.fixture.insert('@historical', status='accepted')
        self.app.identity_store.apply_scan('@historical', self.tg_identity(99881122, '@historical'))
        self.app.handle_callbacks(self.fixture.callback(uid=101, data=f'admin:identity:approve:{rid}:0'))
        self.assertIsNone(self.app.get_complaint_by_id(rid)['target_user_id'])
        self.app.handle_callbacks(self.fixture.callback(uid=9999, data='admin:identity:list:0'))
        messages = [c[2]
                    for c in self.bot.calls if c[0] in ('edit_message_text','send_message')]
        self.assertTrue(any('Telegram ID и привязки' in str(text) for text in messages))
        self.app.handle_callbacks(self.fixture.callback(uid=9999, data=f'admin:identity:approve:{rid}:0'))
        self.assertIsNone(self.app.get_complaint_by_id(rid)['target_user_id'])
        self.app.handle_identity_evidence(self.fixture.msg(uid=9999, text='Доказательство: сообщение из переписки #123'))
        self.assertEqual(self.app.get_complaint_by_id(rid)['target_user_id'], 99881122)

    def test_numeric_search_and_review_records_remain_private(self):
        rid = self.fixture.insert('@helloabc', status='review')
        self.app.identity_store.manual_link(rid, 123654987, actor_id=9999, evidence='Проверяемое доказательство #100')
        self.assertEqual(self.app.identity_store.public_matches('123654987')['total'], 0)
        self.assertIn('не найдено', self.app.format_check_text('123654987'))
        self.assertIn('не найдено', self.app.format_check_text('@helloabc'))
        self.assertTrue(self.app.valid_search_reference('123654987'))
        self.assertFalse(self.app.valid_search_reference('-123654987'))
        self.assertFalse(self.app.valid_search_reference('999999999999999999999999'))

    def test_manual_linking_is_admin_only_and_reversible(self):
        rid = self.fixture.insert('@manualname', status='accepted')
        self.app.admin_bindid_command(self.fixture.msg(uid=101, text=f'/bindid #{rid} 445566778'))
        self.assertIsNone(self.app.get_complaint_by_id(rid)['target_user_id'])
        self.app.admin_bindid_command(self.fixture.msg(uid=9999, text=f'/bindid #{rid} 445566778 переписка с аккаунтом: #123'))
        self.assertEqual(self.app.get_complaint_by_id(rid)['target_user_id'], 445566778)
        self.app.admin_unlinkid_command(self.fixture.msg(uid=9999, text=f'/unlinkid #{rid} Ошибочная связь: переписка #100'))
        self.assertIsNone(self.app.get_complaint_by_id(rid)['target_user_id'])

    def test_bot_observes_authenticated_user_without_public_accusations(self):
        self.app.remember_user_seen(self.fixture.user(123123123, username='ordinary'))
        self.assertEqual(self.app.identity_store.account(123123123)['current_username'], '@ordinary')
        self.assertEqual(self.app.identity_store.public_matches('@ordinary')['total'],0)
        self.app.remember_user_seen(self.fixture.user(123123123, username='othername'))
        self.assertEqual(self.app.identity_store.account(123123123)['current_username'], '@othername')
        old_alias = self.app.db_execute('''SELECT is_current FROM telegram_username_history
            WHERE user_id=? AND username=?''',(123123123,'@ordinary'),fetchone=True)[0]
        self.assertFalse(old_alias['is_current'])

    def test_candidate_id_not_disclosed_in_public_list(self):
        self.fixture.insert('@legacyuser', status='accepted')
        self.app.identity_store.apply_scan('@legacyuser', self.tg_identity(312312312, '@legacyuser'))
        self.app.show_list_page(101, user_id=101)
        public_message = [c[2] for c in self.bot.calls if c[0]=='send_message'][-1]
        self.assertNotIn('ID не установлен', public_message)
        self.assertIn('Записи без ID', public_message)
        self.assertNotIn('312312312', public_message)

    def test_refresh_by_id_exposes_new_alias_with_old_id(self):
        rid = self.fixture.insert('@firstnick', status='accepted')
        self.app.identity_store.manual_link(rid, 44889900, actor_id=9999, evidence='Проверяемое доказательство #100')
        self.app.identity_store.observe(44889900, '@firstnick')
        self.app.identity_resolver = types.SimpleNamespace(
            enabled=True, resolve_id=lambda uid: self.tg_identity(uid, '@lastnick'))
        self.app.db_execute('UPDATE telegram_identities SET last_checked=?',
                            ('2020-01-01T00:00:00+00:00',),commit=True)
        self.assertEqual(self.app.run_identity_refresh_batch(),1)
        self.assertIn(f'#{rid}',self.app.format_check_text('@lastnick'))
        self.assertIn('Исторический ник',self.app.format_check_text('@firstnick'))

    def test_no_network_configuration_means_no_claim_to_resolve_id(self):
        self.assertFalse(self.app.identity_resolver.enabled)
        rid = self.fixture.insert('@noresolver', status='accepted')
        self.assertEqual(self.app.run_identity_scan_batch()['targets'],0)
        self.assertIsNone(self.app.get_complaint_by_id(rid)['target_user_id'])

    def test_migration_of_old_sqlite_preserves_status_and_text(self):
        from identity_db import IdentityStore
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / 'old.sqlite3'
            c = sqlite3.connect(file_path)
            c.row_factory = sqlite3.Row
            c.execute('''CREATE TABLE complaints (id INTEGER PRIMARY KEY, target TEXT NOT NULL,
                note TEXT NOT NULL, status TEXT NOT NULL)''')
            c.execute("INSERT INTO complaints VALUES (1,'@oldname','historical evidence','accepted')")
            c.commit()
            store = IdentityStore(c, threading.RLock())
            store.initialize()
            store.initialize()
            row = c.execute('SELECT * FROM complaints').fetchone()
            self.assertEqual(row['target'], '@oldname')
            self.assertEqual(row['note'], 'historical evidence')
            self.assertEqual(row['status'], 'accepted')
            self.assertIsNone(row['target_user_id'])
            self.assertEqual(row['candidate_status'], 'unscanned')
            c.close()


    def test_delayed_new_case_never_auto_links_recycled_name(self):
        rid = self.fixture.insert('@slowcase', status='accepted')
        self.app.db_execute('UPDATE complaints SET identity_origin=?,timestamp=? WHERE id=?',
                            ('new','2020-01-01T00:00:00+00:00',rid),commit=True)
        outcome = self.app.identity_store.apply_scan('@slowcase',self.tg_identity(401401401,'@slowcase'))
        self.assertEqual(outcome['linked'],0)
        self.assertEqual(outcome['candidates'],1)
        self.assertIsNone(self.app.get_complaint_by_id(rid)['target_user_id'])

    def test_multiple_legacy_records_do_not_merge_without_review(self):
        r1 = self.fixture.insert('@samehandle', status='accepted')
        r2 = self.fixture.insert('@samehandle', status='accepted')
        outcome = self.app.identity_store.apply_scan('@samehandle',self.tg_identity(77334422,'@samehandle'))
        self.assertEqual(outcome['candidates'],2)
        self.assertTrue(self.app.identity_store.approve_candidate(r1, actor_id=9999, evidence='Проверяемое доказательство #100'))
        self.assertEqual(self.app.identity_store.public_matches('77334422')['total'],1)
        self.assertIsNone(self.app.get_complaint_by_id(r2)['target_user_id'])

    def test_no_known_id_after_transient_network_error(self):
        rid = self.fixture.insert('@networkissue', status='review')
        def fail(_):
            raise ConnectionError('Synthetic API disconnect')
        self.app.identity_resolver = types.SimpleNamespace(enabled=True,resolve_username=fail)
        with self.assertLogs('lamascambase', level='ERROR'):
            outcome = self.app.run_identity_scan_batch()
        self.assertEqual(outcome['errors'],1)
        self.assertIsNone(self.app.get_complaint_by_id(rid)['identity_checked_at'])
        self.assertEqual(len(self.app.identity_store.scan_targets()),1)

    def test_unlink_retains_revoked_alias_for_audit(self):
        rid = self.fixture.insert('@oldalias',status='accepted')
        self.app.identity_store.manual_link(rid,44668822, actor_id=9999, evidence='Проверяемое доказательство #100')
        self.assertEqual(self.app.identity_store.public_matches('@oldalias')['total'],1)
        self.assertTrue(self.app.identity_store.unlink(rid, actor_id=9999, evidence='Проверяемое основание #100'))
        self.assertIsNone(self.app.get_complaint_by_id(rid)['target_user_id'])
        row = self.app.db_execute('''SELECT * FROM telegram_username_history
            WHERE user_id=? AND username=?''',(44668822,'@oldalias'),fetchone=True)[0]
        self.assertEqual(row['source'],'admin_revoked')

    def test_telegram_resolver_is_optional_when_api_data_missing(self):
        from telegram_identity import TelegramResolver
        r = TelegramResolver('','','9999:MOCKBOTTOKEN')
        self.assertFalse(r.enabled)
        self.assertIsNone(r.resolve_username('@someone'))



if __name__ == '__main__':
    unittest.main()
