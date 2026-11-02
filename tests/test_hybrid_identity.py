"""R3 security and identity attribution regressions (no Telegram network)."""
import types
import unittest
from unittest.mock import patch
import test_bot
from hybrid_identity import observe_message


class HybridTests(unittest.TestCase):
    setUp=test_bot.AppTest.setUp
    tearDown=test_bot.AppTest.tearDown
    msg=test_bot.AppTest.msg
    user=test_bot.AppTest.user
    insert=test_bot.AppTest.insert
    callback=test_bot.AppTest.callback
    def test_forward_replay_and_hidden_sender(self):
        m=self.msg();m.date=1793310000
        m.forward_origin=types.SimpleNamespace(type='user',sender_user=self.user(5151,'forwarded'))
        observe_message(self.app.identity_store,m)
        observe_message(self.app.identity_store,m)
        count=self.app.conn.execute('SELECT COUNT(*) FROM identity_observations').fetchone()[0]
        self.assertEqual(count,2)
        self.assertEqual(self.app.identity_store.account(5151)['current_username'],'@forwarded')
        m.message_id+=1
        m.forward_origin=types.SimpleNamespace(type='hidden_user',sender_user_name='forwarded')
        observe_message(self.app.identity_store,m)
        self.assertEqual(self.app.conn.execute('SELECT COUNT(*) FROM identity_observations WHERE user_id=5151').fetchone()[0],1)

    def test_recycled_username_never_moves_complaints(self):
        rid=self.insert('@sharedname',status='accepted')
        store=self.app.identity_store
        store.observe(1111,'sharedname',checked_at='2026-10-01T00:00:00+00:00',event_key='old')
        self.assertIsNone(self.app.get_complaint_by_id(rid)['target_user_id'])
        self.assertEqual(store.public_matches('1111')['total'],0)
        self.assertTrue(store.reviewed_link(rid,1111,9999,'Переписка с аккаунтом #456'))
        store.observe(2222,'sharedname',checked_at='2026-10-29T00:00:00+00:00',event_key='new')
        self.assertEqual(store.public_matches('2222')['total'],0)
        self.assertEqual(store.public_matches('1111')['total'],1)
        self.assertIsNone(store.account(1111)['current_username'])
        self.assertFalse(store.reviewed_link(rid,2222,9999,'Другое основание #456'))
        self.assertFalse(store.manual_link(rid,2222, actor_id=9999, evidence='Проверяемое доказательство #100'))

    def test_stale_event_does_not_restore_previous_owner(self):
        store=self.app.identity_store
        store.observe(2222,'sharedname',checked_at='2026-10-29T00:00:00+00:00',event_key='new')
        store.observe(1111,'sharedname',checked_at='2026-10-01T00:00:00+00:00',event_key='old')
        self.assertEqual(store.account(2222)['current_username'],'@sharedname')
        self.assertIsNone(store.account(1111)['current_username'])
        self.assertEqual(len(store.history(1111)),1)

    def test_multiple_owners_are_separate_candidates(self):
        rid=self.insert('@ambiguous',status='accepted');store=self.app.identity_store
        for uid in (1111,2222):store.observe(uid,'ambiguous')
        self.assertEqual({r['user_id'] for r in store.candidates(rid)},{1111,2222})
        self.assertIsNone(self.app.get_complaint_by_id(rid)['target_user_id'])
        self.assertTrue(store.reject_candidate(rid, actor_id=9999, evidence='Проверяемое основание #100'))
        store.observe(1111,'ambiguous')
        self.assertEqual(store.candidates(rid),[])

    def test_request_users_selection_links_subject_not_allegation(self):
        self.app.report_command(self.msg())
        request=self.app.get_state(101)['request_id']
        m=self.msg(text=None,kind='users_shared')
        m.users_shared=types.SimpleNamespace(request_id=request,users=[types.SimpleNamespace(user_id=9191,username=None)])
        self.app.handle_report_target(m)
        self.assertEqual(self.app.get_state(101)['target'],'9191')
        self.assertEqual(self.app.identity_store.account(9191)['user_id'],9191)
        self.app.handle_report_note(self.msg(text='Описание ситуации'))
        self.app.get_state(101)['evidence']=[{'type':'text','data':'Скрин переписки #456'}]
        self.app.finalize_complaint(101,101)
        self.app.finalize_complaint(101,101)
        row=self.app.conn.execute('SELECT * FROM complaints').fetchall()
        self.assertEqual(len(row),1)
        self.assertEqual(row[0]['target_user_id'],9191)
        self.assertEqual(row[0]['identity_source'],'bot_users_shared')
        self.assertIsNone(row[0]['candidate_user_id'])
        self.assertEqual(self.app.identity_store.public_matches('9191')['total'],0)
        self.assertTrue(self.app.change_complaint_status(row[0]['id'],'accepted'))
        self.assertEqual(self.app.identity_store.public_matches('9191')['total'],1)
        self.assertIn('указан заявителем', self.app.public_identity_line(
            self.app.get_complaint_by_id(row[0]['id'])))

    def test_expired_request_and_typed_id_are_not_verified(self):
        self.app.report_command(self.msg())
        m=self.msg(text=None,kind='users_shared')
        m.users_shared=types.SimpleNamespace(request_id=-1,users=[types.SimpleNamespace(user_id=7171,username='victim')])
        self.app.handle_report_target(m)
        self.assertIsNone(self.app.identity_store.account(7171))
        self.assertEqual(self.app.get_state(101)['state'],self.app.STATE_WAITING_FOR_REPORT_TARGET)
        self.app.handle_report_target(self.msg(text='7171'))
        self.assertNotIn('selected_id',self.app.get_state(101))
        self.assertIsNone(self.app.identity_store.account(7171))

    def test_admin_routes_and_evidence_are_private(self):
        rid=self.insert('@owner',status='accepted')
        self.app.identity_store.observe(7171,'owner')
        for uid in (101,):
            for route in ('admin:identity:history:7171',f'admin:identity:verify:{rid}:7171'):
                self.app.handle_callbacks(self.callback(uid=uid,data=route))
                self.assertNotIn('candidate_id',self.app.get_state(uid))
        self.app.handle_callbacks(self.callback(uid=9999,data=f'admin:identity:verify:{rid}:7171'))
        self.assertIsNone(self.app.get_complaint_by_id(rid)['target_user_id'])
        self.app.handle_identity_evidence(self.msg(uid=9999,text='Доказательство: переписка #88'))
        self.app.handle_identity_evidence(self.msg(uid=9999,text='Доказательство: переписка #88'))
        self.assertEqual(self.app.conn.execute('SELECT COUNT(*) FROM identity_review_audit').fetchone()[0],1)
        self.assertEqual(self.app.get_complaint_by_id(rid)['target_user_id'],7171)

    def test_local_reconcile_never_resolves_network(self):
        with patch.object(self.app.identity_resolver,'resolve_username',side_effect=AssertionError('network')):
            self.insert('@unknown',status='accepted')
            self.app.start_manual_identity_scan(9999)
            self.assertEqual(self.app.identity_store.summary()['unknown'],1)

    def test_large_bot_api_id_and_migration_repeat(self):
        uid=2**48+1
        self.app.identity_store.observe(uid,'largeid')
        before=self.app.conn.execute('SELECT COUNT(*) FROM identity_observations').fetchone()[0]
        self.app.identity_store.initialize();self.app.identity_store.initialize()
        self.assertEqual(self.app.conn.execute('SELECT COUNT(*) FROM identity_observations').fetchone()[0],before)
        self.assertEqual(self.app.identity_store.account(uid)['user_id'],uid)
