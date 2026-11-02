"""R3.2: honest identity presentation, privacy, and local-only diagnostics."""
import unittest
from unittest.mock import patch
import test_bot


class IdLookupR32(unittest.TestCase):
    setUp = test_bot.AppTest.setUp
    tearDown = test_bot.AppTest.tearDown
    msg = test_bot.AppTest.msg
    user = test_bot.AppTest.user
    insert = test_bot.AppTest.insert

    def test_myid_uses_authenticated_sender_not_username_lookup(self):
        self.app.myid_command(self.msg(uid=101, text='/myid'))
        text = [c[2] for c in self.bot.calls if c[0]=='send_message'][-1]
        self.assertIn('<code>101</code>', text)
        self.assertEqual(self.app.identity_store.account(101)['current_username'], '@tester')

    def test_myid_does_not_leak_to_group(self):
        self.app.myid_command(self.msg(uid=101,text='/myid',chat_type='group'))
        self.assertIsNone(self.app.identity_store.account(101))

    def test_private_admin_lookup_reports_seen_account(self):
        self.app.identity_store.observe(456789,'Old_Name',event_key='a',source='bot_forward')
        self.app.admin_id_lookup_command(self.msg(uid=9999,text='/id @old_name'))
        text=[c[2] for c in self.bot.calls if c[0]=='send_message'][-1]
        self.assertIn('<code>456789</code>',text)
        self.assertIn('не подтверждённые связи с жалобами',text)

    def test_admin_lookup_does_not_leak_to_regular_user(self):
        self.app.identity_store.observe(456789,'Old_Name',event_key='b',source='bot_forward')
        self.app.admin_id_lookup_command(self.msg(uid=101,text='/id @old_name'))
        self.assertEqual([c for c in self.bot.calls if c[0]=='send_message'], [])

    def test_lookup_unknown_never_calls_mtproto_or_makes_claim(self):
        with patch.object(self.app.identity_resolver,'resolve_username',side_effect=AssertionError('network called')):
            self.app.admin_id_lookup_command(self.msg(uid=9999,text='/id @unknown_human'))
        text = [c[2] for c in self.bot.calls if c[0]=='send_message'][-1]
        self.assertIn('не найден',text)
        self.assertIn('Это не доказательство, что аккаунта нет',text)
        self.assertEqual(self.app.identity_store.lookup_observed('@unknown_human'),[])

    def test_recycled_name_preserves_both_historic_ids(self):
        store=self.app.identity_store
        store.observe(11111,'rec_name',event_key='early',checked_at='2026-10-01T00:00:00+00:00')
        store.observe(22222,'rec_name',event_key='later',checked_at='2026-10-28T00:00:00+00:00')
        result=store.lookup_observed('@rec_name')
        self.assertEqual([r['user_id'] for r in result],[22222,11111])
        self.assertIsNone(store.account(11111)['current_username'])

    def test_candidate_is_visibly_pending_not_exposed_publicly(self):
        rid=self.insert('@candidate',status='accepted')
        self.app.identity_store.observe(777777,'candidate',event_key='c')
        complaint=self.app.get_complaint_by_id(rid)
        self.assertEqual(complaint['candidate_status'],'candidate')
        public=self.app.public_identity_line(complaint)
        self.assertIn('неподтверждённые данные', public)
        self.assertNotIn('777777',public)
        self.assertIn('неподтверждённые данные',self.app.format_check_text('@candidate'))
        self.assertEqual(self.app.identity_store.public_matches('777777')['total'],0)

    def test_unknown_case_has_explicit_unconfirmed_status(self):
        complaint=self.app.get_complaint_by_id(self.insert('@not_observed',status='accepted'))
        self.assertIn('нет подтверждённой привязки', self.app.public_identity_line(complaint))

    def test_id_lookup_never_binds_complaint(self):
        rid=self.insert('@candidid',status='accepted')
        self.app.identity_store.observe(456789,'candidid',event_key='d')
        self.app.admin_id_lookup_command(self.msg(uid=9999,text='/id @candidid'))
        self.assertIsNone(self.app.get_complaint_by_id(rid)['target_user_id'])


if __name__=='__main__':
    unittest.main()
