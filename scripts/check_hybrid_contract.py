"""Offline R3 contract check against the actual installed Telegram library."""
import os,sys,tempfile,json,time
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
with tempfile.TemporaryDirectory() as tmp:
    os.environ.update(API_TOKEN='123456789:'+('X'*35), ADMIN_ID='9999',
                      DB_PATH=str(Path(tmp)/'contract.db'),CHANNEL_USERNAMES='',
                      TELEGRAM_API_ID='',TELEGRAM_API_HASH='')
    import app
    from telebot import types
    from hybrid_identity import observe_message
    sent=[]
    def send(chat_id,text,**kwargs):
        sent.append((chat_id,text,kwargs))
        return SimpleNamespace(message_id=1)
    def message(mid,**extra):
        return types.Message.de_json({'message_id':mid,'date':int(time.time()),
            'chat':{'id':101,'type':'private'},
            'from':{'id':101,'is_bot':False,'first_name':'Reporter','username':'reporter'},**extra})
    with patch.object(app.bot.raw,'send_message',side_effect=send):
        app.report_command(message(1,text='/report'))
        request=app.get_state(101)['request_id']
        keyboard=json.loads(sent[-1][2]['reply_markup'].to_json())
        button=keyboard['keyboard'][0][0]['request_users']
        assert button['request_id']==request and button['request_username'] is True
        shared=message(2,users_shared={'request_id':request,'users':[{'user_id':919191,'username':'sharedname'}]})
        assert shared.content_type=='users_shared'
        app.handle_report_target(shared)
        assert app.ui.delivery_status['buttons'] != 'unreadable'
        assert app.get_state(101)['selected_id']==919191
        assert app.identity_store.account(919191)['current_username']=='@sharedname'
        app.handle_report_note(message(6,text='Описание для изолированной проверки'))
        app.handle_report_evidence(message(7,text='Доказательство для изолированной проверки'))
        app.finalize_complaint(101,101)
        report=app.conn.execute('SELECT * FROM complaints').fetchone()
        assert report['target_user_id']==919191 and report['identity_source']=='bot_users_shared'
        assert report['status']=='review'
        assert app.identity_store.public_matches('919191')['total']==0
        app.change_complaint_status(report['id'],'accepted')
        assert app.identity_store.public_matches('919191')['total']==1
        assert '919191' in app.public_identity_line(dict(report))
        forwarded=message(3,text='forward',forward_origin={'type':'user','date':1793300000,
            'sender_user':{'id':818181,'is_bot':False,'first_name':'Sender','username':'sendername'}})
        observe_message(app.identity_store,forwarded)
        observe_message(app.identity_store,forwarded)
        assert app.identity_store.account(818181)['current_username']=='@sendername'
        hidden=message(4,text='forward',forward_origin={'type':'hidden_user','date':1793300000,'sender_user_name':'Hidden'})
        observe_message(app.identity_store,hidden)
        raw=app.bot.raw
        with patch.object(raw,'process_new_messages') as messages, patch.object(raw,'process_new_callback_query') as callbacks:
            app.install_identity_observer()
            raw.process_new_messages([message(5,text='hello')])
            call=SimpleNamespace(id='cb-test',from_user=SimpleNamespace(id=101,username='reporter'))
            raw.process_new_callback_query([call]);raw.process_new_callback_query([call])
            assert app.conn.execute("SELECT COUNT(*) FROM identity_observations WHERE event_key='callback:cb-test'").fetchone()[0]==1
            assert messages.call_count==1 and callbacks.call_count==2
    app.conn.close()
print('PASS: R3 real Telegram types, request_users keyboard, users_shared, forward_origin, hidden sender, ingestion hooks and replay safety; no network.')
