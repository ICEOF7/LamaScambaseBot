"""R3.3: trust Telegram-originated target IDs only for the exact report event."""
import sqlite3
import sys
import types
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import test_bot

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT / 'scripts'))
from identity_coverage import coverage


class R33IdentityTests(unittest.TestCase):
    setUp=test_bot.AppTest.setUp
    tearDown=test_bot.AppTest.tearDown
    msg=test_bot.AppTest.msg
    user=test_bot.AppTest.user
    insert=test_bot.AppTest.insert

    def _insert_new(self,target,reporter=101,created=None):
        _, rid=self.app.db_execute("""INSERT INTO complaints
            (target, from_user,from_id,note,evidence,timestamp,status,identity_origin)
            VALUES (?, '@tester', ?, 'Свидетельство для модератора', '[]', ?,'review','new')""",
            (target,reporter,created or datetime.now(timezone.utc).isoformat()), commit=True)
        return rid

    def _observed(self,uid,name,source='bot_users_shared',actor=101,evt='evt',when=None):
        self.app.identity_store.observe(uid,name,source=source,actor_id=actor,
            event_key=evt,checked_at=when or (datetime.now(timezone.utc)-timedelta(seconds=3)).isoformat())

    def test_user_shared_id_automatically_identifies_new_report_subject(self):
        rid=self._insert_new('123456789')
        self._observed(123456789,None)
        self.assertTrue(self.app.identity_store.link_direct_report_subject(
            rid,123456789,101,event_key='evt',source='bot_users_shared'))
        row=self.app.get_complaint_by_id(rid)
        self.assertEqual(row['target_user_id'],123456789)
        self.assertEqual(row['identity_source'],'bot_users_shared')
        self.assertEqual(row['candidate_status'],'linked')
        self.assertEqual(self.app.identity_store.candidates(rid),[])
        self.assertFalse(self.app.identity_store.link_direct_report_subject(
            rid,123456789,101,event_key='evt',source='bot_users_shared'))
        self.assertEqual(self.app.identity_store.public_matches('123456789')['total'],0)
        self.app.change_complaint_status(rid,'accepted')
        self.assertEqual(self.app.identity_store.public_matches('123456789')['total'],1)

    def test_forwarded_sender_id_can_bind_a_new_report(self):
        rid=self._insert_new('@origin_user')
        self._observed(151515,'origin_user',source='bot_forward',evt='forward-event')
        self.assertTrue(self.app.identity_store.link_direct_report_subject(
            rid,151515,101,event_key='forward-event',source='bot_forward'))
        self.assertEqual(self.app.get_complaint_by_id(rid)['target_user_id'],151515)

    def test_unrelated_reporter_event_and_username_are_rejected(self):
        rid=self._insert_new('@real_target')
        self._observed(4444,'real_target',actor=202,evt='somebody-else')
        self.assertFalse(self.app.identity_store.link_direct_report_subject(
            rid,4444,101,event_key='somebody-else',source='bot_users_shared'))
        self._observed(4444,'other_target',evt='wrong-name')
        self.assertFalse(self.app.identity_store.link_direct_report_subject(
            rid,4444,101,event_key='wrong-name',source='bot_users_shared'))
        self.assertIsNone(self.app.get_complaint_by_id(rid)['target_user_id'])

    def test_stale_and_future_event_cannot_link_and_old_record_stays_unlinked(self):
        now=datetime.now(timezone.utc)
        rid=self._insert_new('33333',created=now.isoformat())
        self._observed(33333,None,evt='old',when=(now-timedelta(hours=2)).isoformat())
        self.app.conn.execute('UPDATE identity_observations SET received_at=? WHERE event_key=?',
                              ((now-timedelta(hours=2)).isoformat(),'old'))
        self.app.conn.commit()
        self.assertFalse(self.app.identity_store.link_direct_report_subject(
            rid,33333,101,event_key='old',source='bot_users_shared'))
        self._observed(33333,None,evt='future',when=(now+timedelta(minutes=3)).isoformat())
        self.app.conn.execute('UPDATE identity_observations SET received_at=? WHERE event_key=?',
                              ((now+timedelta(hours=2)).isoformat(),'future'))
        self.app.conn.commit()
        self.assertFalse(self.app.identity_store.link_direct_report_subject(
            rid,33333,101,event_key='future',source='bot_users_shared'))
        legacy=self.insert('@other',status='accepted')
        self._observed(33333,'other',evt='legacy-match')
        self.assertFalse(self.app.identity_store.link_direct_report_subject(
            legacy,33333,101,event_key='legacy-match',source='bot_users_shared'))
        self.assertIsNone(self.app.get_complaint_by_id(legacy)['target_user_id'])

    def test_delayed_telegram_update_links_on_receive_not_original_date(self):
        rid=self._insert_new('333222111')
        old=(datetime.now(timezone.utc)-timedelta(days=3)).isoformat()
        self._observed(333222111,None,evt='delayed',when=old)
        self.assertTrue(self.app.identity_store.link_direct_report_subject(
            rid,333222111,101,event_key='delayed',source='bot_users_shared'))

    def test_without_username_user_shared_does_not_erase_last_observed_name(self):
        store=self.app.identity_store
        store.observe(98765,'old_real_name',source='bot_message',event_key='seen')
        store.observe(98765,None,source='bot_users_shared',event_key='shared-no-name')
        self.assertEqual(store.account(98765)['current_username'],'@old_real_name')
        store.observe(98765,None,source='bot_message',event_key='real-no-name')
        self.assertIsNone(store.account(98765)['current_username'])

    def test_numeric_claim_in_old_complaint_is_searchable_but_not_verified(self):
        rid=self.insert('222333444',status='accepted')
        self.assertEqual(self.app.identity_store.public_matches('222333444')['total'],1)
        self.assertIsNone(self.app.get_complaint_by_id(rid)['target_user_id'])
        rendered=self.app.format_check_text('222333444')
        self.assertIn('введённому автором жалобы',rendered)
        self.assertNotIn('Telegram ID: <code>222333444</code>',rendered)

    def test_readonly_coverage_does_not_modify_db_and_exposes_no_ids(self):
        rid=self.insert('@oldrecord',status='accepted')
        self.app.identity_store.observe(444444,'oldrecord',source='bot_forward',event_key='abc')
        db=Path(self.tmp.name)/'only_test.sqlite3'
        self.app.conn.commit()
        expected=(self.app.conn.execute('SELECT COUNT(*) FROM complaints').fetchone()[0],
                  self.app.conn.execute('SELECT COUNT(*) FROM identity_candidates').fetchone()[0])
        summary=coverage(db)
        self.assertEqual(summary['total_reports'],1)
        self.assertEqual(summary['reports_with_linked_id'],0)
        self.assertEqual(summary['complaints_with_pending_candidate'],1)
        self.assertEqual(summary['ids_observed_by_bot'],1)
        self.assertNotIn('444444',str(summary))
        self.assertEqual((self.app.conn.execute('SELECT COUNT(*) FROM complaints').fetchone()[0],
                          self.app.conn.execute('SELECT COUNT(*) FROM identity_candidates').fetchone()[0]),expected)

    def test_coverage_rejects_missing_db_without_creating_one(self):
        missing=Path(self.tmp.name)/'never_created.db'
        with self.assertRaises(FileNotFoundError):
            coverage(missing)
        self.assertFalse(missing.exists())

    def test_no_bot_api_username_resolver_called_for_new_report(self):
        from unittest.mock import patch
        rid=self._insert_new('222000')
        self._observed(222000,None)
        with patch.object(self.app.identity_resolver,'resolve_username',side_effect=AssertionError('network')):
            self.assertTrue(self.app.identity_store.link_direct_report_subject(
                rid,222000,101,event_key='evt',source='bot_users_shared'))

if __name__=='__main__':unittest.main()
