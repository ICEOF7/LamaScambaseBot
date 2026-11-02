"""Synthetic runtime, fault-injection, migration and release-boundary checks."""
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from unittest.mock import patch

import test_community as base
from community_store import CommunityStore
from delivery import DeliveryWorker
from identity_db import IdentityStore
from message_content import PUBLIC_ROUTES, capture_message, send_content, verify_content_result, ContentMismatchError
from runtime_lock import InstanceLock
from ui import UI

ROOT = Path(__file__).resolve().parents[1]


class RuntimeAudit(unittest.TestCase):
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

    def test_broadcast_db_commit_failure_retries_storage_not_send(self):
        jid = self.queued((101,102))
        worker = self.worker()
        original = self.store.finish_broadcast
        calls = []
        def fail_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError('synthetic database busy')
            return original(*args, **kwargs)
        with patch.object(self.store,'finish_broadcast',side_effect=fail_once):
            self.assertFalse(worker.run_once())
            self.assertEqual(self.store.job(jid,9999)['counts'],{'inflight':1,'pending':1})
            self.assertTrue(worker.run_once())
        self.assertEqual(len([c for c in self.bot.calls if c[0]=='send_message']),1)
        self.assertEqual(self.store.job(jid,9999)['counts'],{'sent':1,'pending':1})
        worker.run_once()
        self.assertEqual([c[1] for c in self.bot.calls if c[0]=='send_message'],[101,102])

    def test_support_db_commit_failure_never_duplicates_notification(self):
        tid = self.ticket()
        worker = self.worker()
        with patch.object(self.store,'finish_support',side_effect=sqlite3.OperationalError('synthetic busy')):
            self.assertFalse(worker.run_once())
            self.assertFalse(worker.run_once())
        self.assertTrue(worker.run_once())
        self.assertEqual(len([c for c in self.bot.calls if c[0]=='send_message']),1)
        self.assertEqual(self.store.ticket(tid,101)['messages'][0]['delivery_status'],'sent')

    def test_restart_while_result_is_uncommitted_stays_uncertain(self):
        jid = self.queued()
        with patch.object(self.store,'finish_broadcast',side_effect=sqlite3.OperationalError('synthetic busy')):
            self.worker().run_once()
        self.store.recover()
        restarted = self.worker()
        self.assertFalse(restarted.run_once())
        self.assertEqual(self.store.job(jid,9999)['counts'],{'uncertain':1})
        self.assertEqual(len([c for c in self.bot.calls if c[0]=='send_message']),1)

    def test_threaded_worker_start_is_idempotent_and_shutdown_joins(self):
        jid = self.queued((101,102,103))
        worker = self.worker()
        try:
            worker.start()
            first_thread = worker.thread
            worker.start()
            self.assertIs(worker.thread,first_thread)
            until = time.monotonic()+5
            while self.store.job(jid,9999)['counts'].get('sent',0)<3 and time.monotonic()<until:
                time.sleep(.01)
            self.assertEqual(self.store.job(jid,9999)['counts'],{'sent':3})
        finally:
            worker.stop()
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual(len([c for c in self.bot.calls if c[0]=='send_message']),3)

    def test_second_bot_main_is_blocked_before_runtime_starts(self):
        with InstanceLock(self.app.DB_PATH), patch.object(self.app,'run_bot') as runtime:
            with self.assertRaises(RuntimeError):
                self.app.main()
            runtime.assert_not_called()

    def test_identity_sync_uses_same_database_guard(self):
        spec=importlib.util.spec_from_file_location('audit_sync',ROOT/'scripts/identity_sync.py')
        module=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'app':self.app}):
            spec.loader.exec_module(module)
        with InstanceLock(self.app.DB_PATH), patch.object(module,'run_sync') as action:
            with self.assertRaises(RuntimeError):
                module.main()
            action.assert_not_called()

    def test_all_public_broadcast_routes_open_user_screens(self):
        for route in sorted(PUBLIC_ROUTES):
            with self.subTest(route=route):
                self.bot.calls.clear()
                self.app.handle_callbacks(self.callback(data=route))
                output=[c[2] for c in self.bot.calls if c[0] in {'send_message','edit_message_text'}]
                self.assertTrue(output)
                self.assertFalse(any('Не удалось выполнить действие' in s for s in output))

    def test_support_pages_pagination_and_long_escaped_text(self):
        tid=self.ticket(subject='<subject> & "quotes"')
        for i in range(2,9):
            m=self.message(text='🚀<&> '*100,mid=i)
            self.store.add_reply(tid,101,capture_message(m),101,i)
        self.app.ui.load_pack(self.bot.raw)
        for route in (f'sup:ticket:{tid}:0',f'sup:ticket:{tid}:1',f'sup:ticket:{tid}:999',
                      'sup:list:own:open:0','sup:list:own:closed:0','sup:list:own:open:1',
                      f'sup:message:{self.store.ticket(tid,101)["messages"][0]["id"]}'):
            with self.subTest(route=route):
                self.app.handle_callbacks(self.callback(data=route))
        rendered=[c[2] for c in self.bot.calls if c[0] in {'send_message','edit_message_text'}]
        for text in rendered:
            plain=unescape(re.sub('<[^>]+>','',text))
            self.assertLessEqual(len(plain.encode('utf-16-le'))//2,4096)
        self.assertFalse(any('Не удалось выполнить действие' in s for s in rendered))

    def test_admin_catalog_and_all_dashboard_screens(self):
        self.ticket()
        jid=self.draft()
        for route in ('admin:support','sup:list:adm:open:0','sup:list:adm:closed:0',
                      'admin:broadcast','bc:list:1',f'bc:view:{jid}', 'admin:ui',
                      'admin:ui:refresh','admin:ui:gallery:0','admin:ui:gallery:1'):
            with self.subTest(route=route):
                self.app.handle_callbacks(self.callback(uid=9999,data=route))
        output=[c[2] for c in self.bot.calls if c[0] in {'send_message','edit_message_text'}]
        self.assertFalse(any('Не удалось выполнить действие' in s for s in output))

    def test_broadcast_controls_cancel_confirm_and_paused_screen(self):
        jid=self.queued((101,102))
        for route in (f'bc:pause:{jid}',f'bc:view:{jid}',f'bc:resume:{jid}',f'bc:cancel:{jid}',f'bc:cancel_yes:{jid}'):
            self.app.handle_callbacks(self.callback(uid=9999,data=route))
        self.assertEqual(self.store.job(jid,9999)['status'],'cancelled')
        self.assertEqual(self.store.job(jid,9999)['counts'],{'skipped':2})

    def test_malformed_routes_never_expose_private_ticket_or_draft(self):
        tid=self.ticket()
        for route in ('sup:list:adm:open:0','sup:list:own:invalid:0','sup:ticket:',
                      'sup:ticket:99999999999999999999:0','sup:message:-1',
                      'bc:send:1:1','admin:ui:gallery:0'):
            with self.subTest(route=route):
                self.app.handle_callbacks(self.callback(uid=102,data=route))
        output=[c[2] for c in self.bot.calls if c[0] in {'send_message','edit_message_text'}]
        self.assertFalse(any('Вопрос о проверке' in s for s in output))
        self.assertEqual(self.store.ticket(tid,101)['total'],1)

    def test_legacy_menu_does_not_duplicate_after_ambiguous_edit(self):
        with patch.object(self.bot.raw,'edit_message_text',side_effect=TimeoutError('synthetic uncertain edit')):
            with self.assertRaises(TimeoutError):
                self.app.show_main_menu(101,101,message_id=44)
        self.assertFalse(any(c[0]=='send_message' for c in self.bot.calls))

    def test_unchanged_menu_is_a_noop_without_new_message(self):
        with patch.object(self.bot.raw,'edit_message_text',side_effect=base.ApiError(400,'Bad Request: message is not modified')):
            self.app.show_main_menu(101,101,message_id=44)
        self.assertFalse(any(c[0]=='send_message' for c in self.bot.calls))

    def test_definitive_media_edit_error_creates_one_new_menu(self):
        with patch.object(self.bot.raw,'edit_message_text',side_effect=base.ApiError(400,'Bad Request: there is no text in the message to edit')):
            self.app.show_main_menu(101,101,message_id=44)
        self.assertEqual(len([c for c in self.bot.calls if c[0]=='send_message']),1)

    def test_callback_error_logging_does_not_include_transport_token(self):
        token=str(123456789)+':'+'X'*35
        error=TimeoutError('Synthetic request https://api.telegram.org/bot'+token+'/editMessageText')
        with patch.object(self.bot.raw,'edit_message_text',side_effect=error), self.assertLogs('lamascambase',level='ERROR') as logs:
            self.app.handle_callbacks(self.callback(data='menu:main'))
        self.assertNotIn(token,'\n'.join(logs.output))
        self.assertIn('TimeoutError','\n'.join(logs.output))

    def test_callback_chat_mismatch_has_no_database_or_state_side_effect(self):
        self.app.set_state(101,'community_support_subject')
        call=self.callback(data='menu:main')
        call.message.chat.id=102
        self.app.handle_callbacks(call)
        self.assertEqual(self.app.get_state(101)['state'],'community_support_subject')
        self.assertEqual(self.app.conn.execute('SELECT COUNT(*) FROM users_seen').fetchone()[0],0)

    def test_native_reply_closes_legacy_relay_without_copying_to_partner(self):
        tid=self.ticket()
        self.worker().run_once()
        notification=self.app.conn.execute('SELECT telegram_message_id FROM support_outbox').fetchone()[0]
        self.app.admin_chat_sessions.update({9999:102,102:9999})
        m=self.message(9999,'Reply belongs to ticket 101',mid=25)
        m.reply_to_message=types.SimpleNamespace(message_id=notification)
        self.app.community_message(m)
        self.assertFalse(self.app.admin_chat_sessions)
        self.assertFalse(any(c[0]=='copy_message' for c in self.bot.calls))
        self.assertEqual(self.store.ticket(tid,9999)['total'],2)

    def test_stale_relay_handler_cannot_send_form_content(self):
        self.app.admin_chat_sessions.update({101:9999,9999:101})
        self.app.set_state(101,'community_support_body',subject='Private form')
        self.app.relay_chat_message(self.message(text='Do not relay'))
        self.assertFalse(any(c[0]=='copy_message' for c in self.bot.calls))

    def test_stale_fallback_cannot_replace_active_form(self):
        self.app.set_state(101,'community_support_subject')
        self.app.fallback_text_handler(self.message(text='A subject'))
        self.assertEqual(self.bot.calls,[])
        self.assertEqual(self.app.get_state(101)['state'],'community_support_subject')

    def test_ui_error_with_unusual_body_keeps_original_transport_error(self):
        exc=RuntimeError('Synthetic transport failure')
        exc.result_json=['not','a','dict']
        self.bot.fail_next=exc
        with self.assertRaises(RuntimeError) as captured:
            self.bot.send_message(101,'[[icon:home]] Home')
        self.assertIs(captured.exception,exc)

    def test_unicode_entity_valid_boundaries_still_roundtrip(self):
        m=self.message(text='🚀 Привет 🌍')
        m.entities=[{'type':'bold','offset':3,'length':6}, {'type':'custom_emoji','offset':10,'length':2,'custom_emoji_id':'7000001'}]
        payload=capture_message(m)
        result=send_content(self.bot.raw,self.app.types,101,payload)
        self.assertEqual(result.text,m.text)

    def test_boolean_entity_offset_is_not_a_valid_integer(self):
        m=self.message(text='abc')
        m.entities=[{'type':'bold','offset':True,'length':1}]
        with self.assertRaises(ValueError):capture_message(m)

    def test_caption_position_loss_is_detected(self):
        payload={'kind':'photo','file_id':'SYNTHETIC','caption':'test','caption_entities':[], 'show_caption_above_media':True}
        result=types.SimpleNamespace(message_id=500,caption='test',caption_entities=[],show_caption_above_media=False)
        with self.assertRaises(ContentMismatchError):verify_content_result(result,payload)

    def test_support_only_owner_or_admin_can_reopen_or_view_message(self):
        tid=self.ticket()
        self.store.close_ticket(tid,101)
        mid=self.store.ticket(tid,101)['messages'][0]['id']
        for route in (f'sup:reopen:{tid}',f'sup:close_yes:{tid}',f'sup:reply:{tid}',f'sup:message:{mid}'):
            self.app.handle_callbacks(self.callback(uid=102,data=route))
        self.assertEqual(self.store.ticket(tid,101)['status'],'closed')
        self.assertEqual(self.store.ticket(tid,101)['total'],1)

    def test_synthetic_500_recipients_reopen_sqlite_mid_delivery(self):
        rows=[(uid,None,'2026-01-01','2026-01-01',uid) for uid in range(1000,1500)]
        with self.store.transaction() as conn:
            conn.executemany('INSERT INTO users_seen(user_id,username,first_seen,last_seen,last_chat_id) VALUES (?,?,?,?,?)',rows)
        jid=self.store.create_broadcast(9999)
        self.store.save_draft(jid,9999,payload=capture_message(self.message()))
        revision=self.store.job(jid,9999)['revision']
        self.store.mark_preview(jid,9999,revision)
        self.assertEqual(self.store.enqueue(jid,9999,revision),500)
        for uid in range(1000,1010):self.store.set_notifications(uid,False)
        for uid in range(1010,1015):self.store.mark_blocked(uid)
        worker=self.worker()
        for _ in range(200):self.assertTrue(worker.run_once())
        restored_conn=sqlite3.connect(self.app.DB_PATH)
        restored_conn.row_factory=sqlite3.Row
        restored=CommunityStore(restored_conn,threading.RLock(),9999)
        try:
            restored.initialize();restored.recover()
            next_worker=DeliveryWorker(restored,self.bot.raw,self.app.types,self.app.ui,throttle=False)
            for _ in range(501):
                if not next_worker.run_once():break
            job=restored.job(jid,9999)
            self.assertEqual(job['status'],'completed')
            self.assertEqual(job['counts'],{'sent':485,'skipped':15})
            sent=[c[1] for c in self.bot.calls if c[0]=='send_message']
            self.assertEqual(len(sent),len(set(sent)))
            self.assertEqual(set(sent),set(range(1015,1500)))
            self.assertEqual(restored_conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(restored_conn.execute('PRAGMA foreign_key_check').fetchall(),[])
        finally:restored_conn.close()

    def test_concurrent_support_writes_and_duplicate_updates(self):
        tickets={owner:self.ticket(owner) for owner in range(201,207)}
        def append(owner,mid):
            return self.store.add_reply(tickets[owner],owner,capture_message(self.message(owner,mid=mid)),owner,mid)
        tasks=[(owner,mid) for owner in tickets for mid in range(2,42)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda p:append(*p),tasks+tasks))
        self.assertEqual(sum(created for _,created in results),240)
        self.assertEqual(self.app.conn.execute('SELECT COUNT(*) FROM support_messages').fetchone()[0],246)
        self.assertEqual(self.app.conn.execute('SELECT COUNT(*) FROM support_outbox').fetchone()[0],246)
        for owner,tid in tickets.items():self.assertEqual(self.store.ticket(tid,owner)['total'],41)
        self.assertEqual(self.app.conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_additive_migration_preserves_100_legacy_records(self):
        with tempfile.TemporaryDirectory() as temp:
            conn=sqlite3.connect(str(Path(temp)/'synthetic.sqlite3'))
            conn.row_factory=sqlite3.Row
            conn.execute('CREATE TABLE complaints(id INTEGER PRIMARY KEY, target TEXT, from_user TEXT, from_id INTEGER, note TEXT, evidence TEXT, timestamp TEXT, status TEXT)')
            records=[(n,'@synthetic_'+str(n),None,1000+n,'Legacy note '+str(n),'[]','2026-01-01','accepted' if n%2 else 'review') for n in range(1,101)]
            conn.executemany('INSERT INTO complaints VALUES (?,?,?,?,?,?,?,?)',records);conn.commit()
            lock=threading.RLock()
            try:
                for _ in range(3):
                    IdentityStore(conn,lock).initialize()
                    CommunityStore(conn,lock,9999).initialize()
                actual=[tuple(r) for r in conn.execute('SELECT id,target,from_user,from_id,note,evidence,timestamp,status FROM complaints ORDER BY id')]
                self.assertEqual(actual,records)
                self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            finally:conn.close()


class InstanceLockTests(unittest.TestCase):
    def test_second_process_is_rejected_and_restart_after_release_works(self):
        code='''import sys
from runtime_lock import InstanceLock
try:
    with InstanceLock(sys.argv[1]): pass
except RuntimeError:
    sys.exit(3)
'''
        with tempfile.TemporaryDirectory() as root:
            db=str(Path(root)/'synthetic.sqlite3')
            env=dict(os.environ,PYTHONPATH=str(ROOT))
            with InstanceLock(db):
                result=subprocess.run([sys.executable,'-c',code,db],env=env,capture_output=True,timeout=5)
                self.assertEqual(result.returncode,3,result.stderr.decode())
            result=subprocess.run([sys.executable,'-c',code,db],env=env,capture_output=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr.decode())

    def test_lock_releases_on_exception(self):
        with tempfile.TemporaryDirectory() as root:
            db=str(Path(root)/'synthetic.sqlite3')
            with self.assertRaises(ValueError):
                with InstanceLock(db):raise ValueError('synthetic')
            with InstanceLock(db):pass

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink alias regression')
    def test_symlink_alias_of_database_uses_same_instance_lock(self):
        with tempfile.TemporaryDirectory() as root:
            database=Path(root)/'original.sqlite3'
            database.touch()
            alias=Path(root)/'alias.sqlite3'
            alias.symlink_to(database)
            with InstanceLock(str(database)) as first:
                second=InstanceLock(str(alias))
                self.assertEqual(first.path,second.path)
                with self.assertRaises(RuntimeError):
                    with second: pass

    def test_memory_database_needs_no_file(self):
        with InstanceLock(':memory:') as lock:self.assertIsNone(lock.path)


class ArchiveSafetyTests(unittest.TestCase):
    def scanner(self):
        spec=importlib.util.spec_from_file_location('archive_audit_scanner',ROOT/'scripts/check_release.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def check_zip(self,members):
        with tempfile.TemporaryDirectory() as root:
            archive=Path(root)/'source.zip'
            with zipfile.ZipFile(archive,'w') as z:
                for name,value in members:z.writestr(name,value)
            return self.scanner().scan_archive(archive)

    def test_clean_archive_passes_strict_scan(self):
        self.assertEqual(self.check_zip([('Bot/app.py','print("synthetic")'),('Bot/.env.example','API_TOKEN=CHANGE_ME\nADMIN_ID=0\nTELEGRAM_API_HASH=\n')]),[])

    def test_git_history_is_forbidden_in_archive(self):
        self.assertTrue(self.check_zip([('Bot/.git/config','synthetic')]))

    def test_runtime_cache_is_forbidden_in_archive(self):
        self.assertTrue(self.check_zip([('Bot/__pycache__/cached.py','synthetic')]))

    def test_traversal_archive_is_rejected_without_extraction(self):
        self.assertEqual(self.check_zip([('../escape.py','synthetic')]),['unsafe archive member'])

    def test_absolute_and_windows_paths_are_rejected(self):
        for path in ('/tmp/escape.py','C:/escape.py','Bot\\escape.py'):
            with self.subTest(path=path):self.assertEqual(self.check_zip([(path,'synthetic')]),['unsafe archive member'])

    def test_renamed_binary_database_is_rejected(self):
        self.assertTrue(self.check_zip([('Bot/not_database.py',b'SQLite format 3\x00synthetic')]))

    def test_runtime_secret_symlink_cannot_hide_in_ignored_directory_name(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)
            (path/'source.py').write_text('synthetic')
            (path/'.venv').symlink_to(path/'source.py')
            self.assertTrue(self.scanner().scan(path))

    def test_archive_size_limit(self):
        self.assertEqual(self.check_zip([('Bot/large.txt','x'*500001)]),['oversized archive member'])


if __name__=='__main__':unittest.main()
