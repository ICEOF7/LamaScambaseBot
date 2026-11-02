import json
import sqlite3
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
import test_bot
from identity_diagnostics import audit_database


class IdentityReliability(unittest.TestCase):
    setUp=test_bot.AppTest.setUp
    tearDown=test_bot.AppTest.tearDown
    msg=test_bot.AppTest.msg
    user=test_bot.AppTest.user
    insert=test_bot.AppTest.insert
    callback=test_bot.AppTest.callback

    def selected_form(self, uid=101, subject=919191, username='subject'):
        self.app.report_command(self.msg(uid=uid,text='/report'))
        request=self.app.get_state(uid)['request_id']
        m=self.msg(uid=uid,text=None,kind='users_shared')
        m.users_shared=types.SimpleNamespace(request_id=request,users=[types.SimpleNamespace(user_id=subject,username=username)])
        self.app.handle_report_target(m)
        self.app.handle_report_note(self.msg(uid=uid,text='Описание события для проверки'))
        self.app.get_state(uid)['evidence']=[{'type':'text','data':'Доказательство #100'}]
        self.app.identity_store.save_form(uid,self.app.get_state(uid))
        return dict(self.app.get_state(uid))

    def submit(self, data):
        return self.app.identity_store.create_report(data,101,'@reporter',data['note'],data['evidence'],'review')

    def test_report_and_subject_are_atomic_when_sqlite_update_fails(self):
        data=self.selected_form()
        self.app.conn.execute("""CREATE TRIGGER reject_subject BEFORE UPDATE OF target_user_id ON complaints
            BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):self.submit(data)
        self.assertEqual(self.app.conn.execute('SELECT COUNT(*) FROM complaints').fetchone()[0],0)
        self.assertIsNone(self.app.conn.execute('SELECT complaint_id FROM identity_event_claims').fetchone()[0])
        self.app.conn.execute('DROP TRIGGER reject_subject')
        cid,created=self.submit(data)
        self.assertTrue(created)
        row=self.app.get_complaint_by_id(cid)
        self.assertEqual(row['target_user_id'],919191)
        self.assertEqual(row['identity_source'],'bot_users_shared')
        self.assertEqual(row['status'],'review')

    def test_failed_observation_rolls_back_dedup_key_then_retry_succeeds(self):
        cid=self.insert('@retryuser')
        self.app.conn.execute("""CREATE TRIGGER reject_candidate BEFORE INSERT ON identity_candidates
            BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.app.identity_store.observe(515151,'retryuser',event_key='retry')
        self.assertIsNone(self.app.identity_store.account(515151))
        self.assertIsNone(self.app.conn.execute("SELECT * FROM identity_observations WHERE event_key='retry'").fetchone())
        self.app.conn.execute('DROP TRIGGER reject_candidate')
        self.assertTrue(self.app.identity_store.observe(515151,'retryuser',event_key='retry'))
        self.assertEqual(len(self.app.identity_store.candidates(cid)),1)

    def test_concurrent_submit_consumes_form_once(self):
        data=self.selected_form()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:self.submit(data),range(16)))
        self.assertEqual(sum(created for _,created in results),1)
        self.assertEqual(len({cid for cid,_ in results}),1)
        self.assertEqual(self.app.conn.execute('SELECT COUNT(*) FROM complaints').fetchone()[0],1)

    def test_restart_restores_form_and_selected_identity(self):
        self.selected_form()
        self.app.user_states.clear()
        data=self.app.get_state(101)
        self.assertEqual(data['selected_id'],919191)
        self.assertEqual(len(data['evidence']),1)
        self.app.finalize_complaint(101,101)
        row=self.app.conn.execute('SELECT * FROM complaints').fetchone()
        self.assertEqual(row['target_user_id'],919191)
        self.app.user_states.clear()
        self.assertEqual(self.app.get_state(101),{})

    def test_event_cannot_be_reused_by_new_form(self):
        first=self.selected_form()
        old=first['selection_event_key']
        m=self.msg(text='/report');m.message_id=999
        self.app.report_command(m)
        second=self.app.get_state(101)
        self.assertFalse(self.app.identity_store.claim_selection(101,second,old,919191,'bot_users_shared',second['request_id']))
        self.assertNotEqual(first['_form_token'],second['_form_token'])

    def test_expired_and_corrupt_forms_are_not_restored(self):
        data=self.selected_form()
        self.app.conn.execute('UPDATE identity_forms SET expires_at=?',(time.time()-1,));self.app.conn.commit()
        self.app.user_states.clear()
        self.assertEqual(self.app.get_state(101),{})
        with self.assertRaises(ValueError):self.submit(data)
        self.app.report_command(self.msg())
        self.app.conn.execute("UPDATE identity_forms SET state_json='invalid' WHERE status='active'");self.app.conn.commit()
        self.app.user_states.clear()
        self.assertEqual(self.app.get_state(101),{})

    def test_form_lifetime_is_not_shorter_than_identity_binding_window(self):
        data=self.selected_form()
        then=datetime.now(timezone.utc)-timedelta(minutes=35)
        self.app.conn.execute('UPDATE identity_forms SET opened_at=?,expires_at=?',(then.timestamp(),time.time()+1400))
        self.app.conn.execute('UPDATE identity_observations SET received_at=?,observed_at=? WHERE event_key=?',
            (then.isoformat(),then.isoformat(),data['selection_event_key']));self.app.conn.commit()
        cid,_=self.submit(data)
        self.assertEqual(self.app.get_complaint_by_id(cid)['target_user_id'],919191)

    def test_ledger_recovers_exact_lost_link_but_not_revoked_one(self):
        data=self.selected_form();cid,_=self.submit(data)
        self.app.conn.execute('UPDATE complaints SET target_user_id=NULL WHERE id=?',(cid,));self.app.conn.commit()
        self.assertEqual(self.app.identity_store.recover_known_links(),1)
        self.assertEqual(self.app.identity_store.recover_known_links(),0)
        self.assertTrue(self.app.identity_store.unlink(cid,9999,'Ошибка в доказательстве #100',919191))
        self.assertEqual(self.app.identity_store.recover_known_links(),0)
        self.assertIsNone(self.app.get_complaint_by_id(cid)['target_user_id'])
        audit=self.app.conn.execute("SELECT * FROM identity_review_audit WHERE action='unlink'").fetchone()
        self.assertEqual(audit['actor_id'],9999)
        self.assertEqual(audit['previous_user_id'],919191)

    def test_changed_subject_metadata_prevents_ledger_recovery(self):
        cid,_=self.submit(self.selected_form())
        self.app.conn.execute("UPDATE complaints SET target_user_id=NULL,target='@different' WHERE id=?",(cid,));self.app.conn.commit()
        self.assertEqual(self.app.identity_store.recover_known_links(),0)

    def test_author_clarification_is_private_candidate_and_never_automatic_link(self):
        cid=self.insert('@oldowner',status='accepted',uid=101)
        self.app.clarify_command(self.msg(uid=202,text=f'/clarify #{cid}'))
        self.assertNotIn('clarify_id',self.app.get_state(202))
        self.app.clarify_command(self.msg(text=f'/clarify #{cid}'))
        m=self.msg(text=None,kind='users_shared')
        m.users_shared=types.SimpleNamespace(request_id=self.app.get_state(101)['request_id'],users=[types.SimpleNamespace(user_id=808080,username='newowner')])
        self.app.handle_report_target(m)
        row=self.app.get_complaint_by_id(cid)
        self.assertIsNone(row['target_user_id'])
        self.assertEqual(row['candidate_user_id'],808080)
        self.assertEqual(self.app.identity_store.public_matches('808080')['total'],0)

    def test_rejected_candidate_is_audited_and_stale_unlink_does_not_remove_new_link(self):
        cid=self.insert('@candidate');self.app.identity_store.observe(112233,'candidate')
        self.assertTrue(self.app.identity_store.reject_candidate(cid,9999,'Нет связи с перепиской #100'))
        self.assertEqual(self.app.conn.execute("SELECT actor_id FROM identity_review_audit WHERE action='reject'").fetchone()[0],9999)
        self.assertTrue(self.app.identity_store.reviewed_link(cid,112233,9999,'Доказательство: переписка #100'))
        self.assertFalse(self.app.identity_store.unlink(cid,9999,'Устаревшая кнопка #100',445566))
        self.assertEqual(self.app.get_complaint_by_id(cid)['target_user_id'],112233)
        with self.assertRaises(PermissionError):self.app.identity_store.unlink(cid,101,'Неверные полномочия #100')

    def test_confirmed_numeric_subject_does_not_match_old_incorrect_numeric_claim(self):
        cid=self.insert('112233',status='accepted')
        self.app.identity_store.reviewed_link(cid,445566,9999,'Проверяемое доказательство #100')
        self.assertEqual(self.app.identity_store.public_matches('112233')['total'],0)
        self.assertEqual(self.app.identity_store.public_matches('445566')['total'],1)

    def test_history_earliest_observation_and_no_username_are_retained(self):
        store=self.app.identity_store
        store.observe(112233,'history',checked_at='2026-10-20T00:00:00+00:00',event_key='new')
        store.observe(112233,'history',checked_at='2026-10-01T00:00:00+00:00',event_key='old')
        row=self.app.conn.execute('SELECT first_seen FROM telegram_username_history').fetchone()
        self.assertEqual(row['first_seen'],'2026-10-01T00:00:00+00:00')
        store.observe(112233,None,event_key='no-name')
        self.assertEqual(self.app.conn.execute('SELECT COUNT(*) FROM telegram_username_history').fetchone()[0],1)

    def test_diagnostics_never_infer_subject_from_reporter_and_do_not_expose_identity(self):
        cid=self.insert('@testtarget',uid=515151)
        self.app.identity_store.observe(515151,'reporter')
        result=audit_database(self.app.conn)
        self.assertEqual(result['findings'][0]['category'],'unknown')
        self.assertFalse(result['findings'][0]['reporter_id_is_subject_evidence'])
        self.assertNotIn('515151',json.dumps(result));self.assertNotIn('@testtarget',json.dumps(result))

    def test_filters_and_mutations_require_private_administrator(self):
        cid=self.insert('@unknown')
        for route in ('admin:identity:filter:unknown:0','admin:identity:audit:0',f'admin:identity:clarify:{cid}'):
            self.bot.calls.clear();self.app.handle_callbacks(self.callback(uid=101,data=route))
            self.assertFalse(any(c[0]=='send_message' for c in self.bot.calls))
        self.app.handle_callbacks(self.callback(uid=9999,data='admin:identity:filter:unknown:0'))
        self.assertTrue(any(c[0]=='send_message' for c in self.bot.calls))

    def test_reconnecting_does_not_install_duplicate_ingress(self):
        self.app.install_identity_observer()
        wrapper=self.app.bot.raw.process_new_messages
        self.app.install_identity_observer()
        self.assertIs(self.app.bot.raw.process_new_messages,wrapper)

    def test_repeated_evidence_event_is_not_appended_twice_after_restart(self):
        self.selected_form()
        self.app.get_state(101)['evidence']=[]
        m=self.msg(text='Подтверждение: переписка #101');m.message_id=1001
        self.app.handle_report_evidence(m)
        self.app.user_states.clear()
        self.app.handle_report_evidence(m)
        self.assertEqual(len(self.app.get_state(101)['evidence']),1)

    def test_replayed_report_command_does_not_replace_active_form(self):
        data=self.selected_form()
        self.app.user_states.clear()
        self.app.report_command(self.msg(text='/report'))
        self.assertEqual(self.app.get_state(101)['_form_token'],data['_form_token'])
        self.assertEqual(self.app.get_state(101)['selected_id'],919191)

    def test_handler_fallback_does_not_rewind_username_of_replayed_message(self):
        old='2026-10-01T00:00:00+00:00';new='2026-10-20T00:00:00+00:00'
        self.app.remember_user_seen(self.user(515151,'before'),515151,observed_at=old,event_key='old-message')
        self.app.remember_user_seen(self.user(515151,'after'),515151,observed_at=new,event_key='new-message')
        self.app.remember_user_seen(self.user(515151,'before'),515151,observed_at=old,event_key='old-message')
        self.assertEqual(self.app.identity_store.account(515151)['current_username'],'@after')
        self.assertEqual(self.app.conn.execute('SELECT username FROM users_seen WHERE user_id=515151').fetchone()[0],'@after')

    def test_obsolete_event_key_can_repair_old_partial_reconciliation(self):
        cid=self.insert('@repair')
        self.app.identity_store.observe(111222,'repair',event_key='partial')
        self.app.conn.execute('DELETE FROM identity_candidates WHERE complaint_id=?',(cid,))
        self.app.conn.execute("UPDATE complaints SET candidate_status='unscanned',candidate_user_id=NULL WHERE id=?",(cid,))
        self.app.conn.commit()
        self.assertFalse(self.app.identity_store.observe(111222,'repair',event_key='partial'))
        self.assertEqual(len(self.app.identity_store.candidates(cid)),1)

    def test_manual_helpers_cannot_bypass_evidence_or_administrator(self):
        cid=self.insert('@protected')
        with self.assertRaises(PermissionError):self.app.identity_store.manual_link(cid,111222)
        with self.assertRaises(PermissionError):self.app.identity_store.approve_candidate(cid)
        with self.assertRaises(PermissionError):self.app.identity_store.manual_link(cid,111222,actor_id=101,evidence='Основание для проверки #100')
        with self.assertRaises(PermissionError):self.app.identity_store.unlink(cid)
        self.assertIsNone(self.app.get_complaint_by_id(cid)['target_user_id'])

    def test_corrupted_old_id_does_not_crash_public_card(self):
        from report_view import complaint_card
        row={'id':1,'target':'@example','target_user_id':'damaged','note':'Пример','timestamp':'bad-date'}
        self.assertNotIn('damaged',complaint_card(row))
        self.assertIn('нет подтверждённой привязки',self.app.public_identity_line(row))

    def test_numeric_subject_is_not_fabricated_username_history(self):
        cid=self.insert('123123')
        self.app.identity_store.reviewed_link(cid,123123,9999,'Основание по переписке #100')
        self.assertEqual(self.app.conn.execute('SELECT COUNT(*) FROM telegram_username_history').fetchone()[0],0)

    def test_large_history_is_not_scanned_for_each_message(self):
        store=self.app.identity_store
        now=datetime.now(timezone.utc).isoformat()
        self.app.conn.executemany('INSERT INTO identity_observations VALUES (?,?,?,?,?,?,?)',
            [(f'bulk:{n}',200000+n,f'@bulk{n}',now,'bot_message',200000+n,now) for n in range(50000)])
        self.app.conn.executemany('''INSERT INTO complaints (target,note,timestamp,status)
            VALUES (?,'Изолированная нагрузка',?,'accepted')''',[(f'@complaint{n}',now) for n in range(20000)])
        self.app.conn.commit()
        started=time.monotonic()
        for n in range(25):store.observe(300000+n,f'new{n}',event_key=f'new:{n}')
        elapsed=time.monotonic()-started
        print(f'IDENTITY_BENCHMARK observations=50000 complaints=20000 events=25 elapsed={elapsed:.3f}s')
        self.assertLess(elapsed,5,'Incoming event reconciliation must be indexed')
        plan=' '.join(str(tuple(r)) for r in self.app.conn.execute('EXPLAIN QUERY PLAN SELECT MAX(observed_at) FROM identity_observations WHERE username=?',('@bulk9',)))
        self.assertIn('INDEX',plan)
