"""Independent regressions from the second audit; synthetic accounts/data only."""
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch

import test_community as base
from message_content import capture_message, verify_content_result, ContentMismatchError
from ui import UI


class AuditRegressions(unittest.TestCase):
    setUp = base.CommunityTests.setUp
    tearDown = base.CommunityTests.tearDown
    user = base.CommunityTests.user
    msg = base.CommunityTests.msg
    callback = base.CommunityTests.callback
    insert = base.CommunityTests.insert
    store = base.CommunityTests.store
    seen = base.CommunityTests.seen
    message = base.CommunityTests.message
    draft = base.CommunityTests.draft
    queued = base.CommunityTests.queued
    worker = base.CommunityTests.worker
    ticket = base.CommunityTests.ticket

    def notified_ticket(self, owner=101):
        tid = self.ticket(owner)
        self.worker().run_once()
        row = self.app.conn.execute('SELECT telegram_message_id FROM support_outbox ORDER BY id DESC LIMIT 1').fetchone()
        return tid, row[0]

    def test_native_reply_overrides_other_ticket_composer(self):
        first, notification = self.notified_ticket()
        second = self.ticket(102)
        self.app.set_state(9999, 'community_support_reply', ticket_id=second)
        msg = self.message(9999, 'Private answer for the FIRST ticket', mid=20)
        msg.reply_to_message = types.SimpleNamespace(message_id=notification)
        self.app.community_message(msg)
        self.assertEqual(self.store.ticket(first, 9999)['total'], 2)
        self.assertEqual(self.store.ticket(second, 9999)['total'], 1)
        row = self.app.conn.execute('SELECT recipient_id FROM support_outbox ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(row[0], 101, 'A reply to user 101 must never go to user 102')

    def test_native_reply_cannot_become_broadcast_content(self):
        tid, notification = self.notified_ticket()
        jid = self.store.create_broadcast(9999)
        self.app.set_state(9999, 'community_bc_content', job_id=jid)
        msg = self.message(9999, 'Private support answer, NOT a newsletter', mid=20)
        msg.reply_to_message = types.SimpleNamespace(message_id=notification)
        self.app.community_message(msg)
        self.assertIsNone(self.store.job(jid, 9999)['payload'])
        self.assertEqual(self.store.ticket(tid, 9999)['total'], 2)

    def test_native_reply_is_recognized_during_old_report_form(self):
        tid, notification = self.notified_ticket()
        self.app.set_state(9999, self.app.STATE_WAITING_FOR_REPORT_NOTE, target='@synthetic')
        msg = self.message(9999, 'Answer to the support notification', mid=21)
        msg.reply_to_message = types.SimpleNamespace(message_id=notification)
        self.assertTrue(self.app.community.wants_message(msg))
        self.app.community_message(msg)
        self.assertEqual(self.store.ticket(tid, 9999)['total'], 2)

    def test_reply_to_closed_ticket_does_not_fall_through_to_other_ticket(self):
        first, notification = self.notified_ticket()
        second = self.ticket(102)
        self.store.close_ticket(first, 101)
        self.app.set_state(9999, 'community_support_reply', ticket_id=second)
        msg = self.message(9999, 'Must not leak to ticket two', mid=22)
        msg.reply_to_message = types.SimpleNamespace(message_id=notification)
        self.app.community_message(msg)
        self.assertEqual(self.store.ticket(first, 9999)['total'], 1)
        self.assertEqual(self.store.ticket(second, 9999)['total'], 1)

    def test_stale_report_note_handler_cannot_overwrite_support_form(self):
        self.app.set_state(101, 'community_support_body', subject='Support topic')
        self.app.handle_report_note(self.message(text='This is private support text'))
        self.assertEqual(self.app.get_state(101)['state'], 'community_support_body')
        self.assertNotIn('note', self.app.get_state(101))

    def test_stale_report_target_handler_cannot_overwrite_support_form(self):
        self.app.set_state(101, 'community_support_body', subject='Support topic')
        self.app.handle_report_target(self.message(text='@synthetic'))
        self.assertEqual(self.app.get_state(101)['state'], 'community_support_body')

    def test_bad_control_character_in_subject_stays_on_first_step(self):
        self.app.set_state(101, 'community_support_subject')
        self.app.community_message(self.message(text='Bad\tSubject'))
        self.assertEqual(self.app.get_state(101)['state'], 'community_support_subject')

    def test_nested_bad_icon_override_does_not_crash_startup(self):
        self.store.set_setting('emoji_overrides', json.dumps({'search': []}))
        ui = UI(self.app.types, self.store)
        self.assertEqual(ui.overrides, {})

    def test_invalid_cached_icons_are_not_exposed_to_gallery(self):
        self.store.set_setting('emoji_catalog', json.dumps([{}, {'id':'bad','emoji':'x'}, {'id':'7000001','emoji':'🔍'}]))
        ui = UI(self.app.types, self.store)
        self.assertEqual(ui.catalog, [{'id':'7000001','emoji':'🔍'}])

    def test_split_utf16_surrogate_entity_is_rejected(self):
        msg = self.message(text='🚀 test')
        msg.entities = [{'type':'bold', 'offset':1, 'length':1}]
        with self.assertRaises(ValueError):
            capture_message(msg)

    def test_datetime_entity_change_is_detected(self):
        entity = {'type':'date_time','offset':0,'length':4,'unix_time':1000,'date_time_format':'d'}
        payload = {'kind':'text','text':'date','entities':[entity]}
        result = types.SimpleNamespace(message_id=510,text='date',entities=[dict(entity,unix_time=2000)])
        with self.assertRaises(ContentMismatchError):
            verify_content_result(result,payload)

    def test_lost_media_spoiler_is_detected(self):
        payload = {'kind':'photo','file_id':'SYNTHETIC', 'caption':'test', 'caption_entities':[], 'has_spoiler':True}
        result = types.SimpleNamespace(message_id=510,caption='test',caption_entities=[],has_media_spoiler=False)
        with self.assertRaises(ContentMismatchError):
            verify_content_result(result,payload)

    def test_corrupt_retry_setting_does_not_stop_delivery(self):
        jid = self.queued()
        self.store.set_setting('delivery_retry_after','broken-value')
        self.worker().run_once()
        self.assertEqual(self.store.job(jid,9999)['counts'], {'sent':1})

    def test_last_429_attempt_still_respects_global_retry_after(self):
        self.queued((101,102))
        self.app.conn.execute('UPDATE broadcast_deliveries SET attempts=7 WHERE user_id=101')
        self.app.conn.commit()
        self.bot.fail_next=base.ApiError(429,'Too Many Requests',retry_after=10)
        self.worker().run_once()
        self.assertGreater(float(self.store.setting('delivery_retry_after','0')), time.time()+9)

    def test_denied_admin_callback_does_not_discard_current_form(self):
        self.app.set_state(101,'community_support_body',subject='Keep this draft')
        self.app.handle_callbacks(self.callback(data='admin:broadcast'))
        self.assertEqual(self.app.get_state(101).get('state'),'community_support_body')

    def test_direct_relay_cannot_start_over_private_support_form(self):
        rid = self.insert(uid=101)
        self.app.set_state(101,'community_support_body',subject='In progress')
        self.app.start_direct_chat(9999,rid)
        self.assertNotIn(101,self.app.admin_chat_sessions)


class ScannerAudit(unittest.TestCase):
    @staticmethod
    def scanner():
        spec=importlib.util.spec_from_file_location('audit_scanner',Path(__file__).resolve().parents[1]/'scripts/check_release.py')
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_example_cannot_contain_telegram_api_hash(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root,'.env.example').write_text('TELEGRAM_API_HASH='+'ab'*16+'\n',encoding='utf-8')
            self.assertTrue(self.scanner().scan(Path(root)))

    def test_scanner_detects_fine_grained_github_token(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root,'bad.py').write_text('token="'+'github'+'_pat_'+'a'*82+'"',encoding='utf-8')
            self.assertTrue(self.scanner().scan(Path(root)))


if __name__ == '__main__':
    unittest.main()
