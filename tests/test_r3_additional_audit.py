"""Independent regression checks for R3 delivery and passive ID resolution."""
from __future__ import annotations
import sqlite3
import unittest
from unittest.mock import patch, Mock
from datetime import datetime, timezone
import test_bot


class R3Audit(unittest.TestCase):
    setUp = test_bot.AppTest.setUp
    tearDown = test_bot.AppTest.tearDown
    msg = test_bot.AppTest.msg
    insert = test_bot.AppTest.insert
    user = test_bot.AppTest.user
    callback = test_bot.AppTest.callback

    def test_report_user_selection_accepts_bot_accounts(self):
        """User selector must not silently exclude scam bots."""
        self.app.report_command(self.msg(text='/report'))
        sent = [x for x in self.bot.calls if x[0]=='send_message'][-1]
        keyboard = sent[3]['reply_markup']
        selector = keyboard.keyboard[0][0].request_users
        self.assertIsNone(getattr(selector,'user_is_bot',None))

    def test_ingress_continues_if_passive_identity_observation_fails(self):
        """ID enrichment is best effort; it must not blackhole user messages."""
        raw = self.app.bot.raw
        incoming = Mock()
        raw.process_new_messages = incoming
        self.app.install_identity_observer()
        with patch.object(self.app.identity_store, 'observe',side_effect=sqlite3.OperationalError('synthetic SQLite unavailable')):
            raw.process_new_messages([self.msg(text='/start')])
        incoming.assert_called_once()

    def test_callback_continues_if_passive_identity_observation_fails(self):
        raw = self.app.bot.raw
        callbacks = Mock()
        raw.process_new_callback_query = callbacks
        self.app.install_identity_observer()
        with patch.object(self.app.identity_store,'observe',side_effect=sqlite3.OperationalError('synthetic SQLite unavailable')):
            raw.process_new_callback_query([self.callback(uid=101)])
        callbacks.assert_called_once()

    def test_new_observation_does_not_rescan_unrelated_legacy_targets(self):
        """A single Bot API event must only reconcile the corresponding alias / ID."""
        rid = self.insert('@oldername',status='accepted')
        self.app.identity_store.observe(5555,'oldername',checked_at='2026-10-01T00:00:00+00:00',event_key='observed-old')
        self.app.conn.execute('DELETE FROM identity_candidates WHERE complaint_id=?',(rid,))
        self.app.conn.execute("UPDATE complaints SET candidate_user_id=NULL,candidate_status='unscanned' WHERE id=?",(rid,))
        self.app.conn.commit()
        self.app.identity_store.observe(6666,'unrelated',checked_at='2026-10-02T00:00:00+00:00',event_key='observed-new')
        self.assertEqual(self.app.identity_store.candidates(rid),[])
        self.app.identity_store.reconcile(alias='oldername')
        self.assertEqual(len(self.app.identity_store.candidates(rid)),1)

    def test_manual_entry_reconciles_known_history_without_network(self):
        self.app.identity_store.observe(91919,'old_manual',event_key='existing-identity')
        self.app.insert_manual_scammers(['@old_manual'], 'Проверить при модерации')
        row = self.app.conn.execute("SELECT * FROM complaints WHERE target='@old_manual'").fetchone()
        self.assertEqual(row['candidate_user_id'],91919)
        self.assertIsNone(row['target_user_id'])

    def test_forwarded_video_note_can_submit_target(self):
        reg = [(args,kwargs,fn) for args,kwargs,fn in self.app.bot.raw.message_handlers if fn.__name__=='handle_report_target']
        self.assertTrue(reg)
        types=reg[0][1]['content_types']
        self.assertIn('video_note',types)
        self.assertIn('animation',types)
