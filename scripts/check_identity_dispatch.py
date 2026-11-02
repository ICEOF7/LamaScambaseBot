"""Run real TeleBot dispatch on isolated synthetic updates; all HTTP is mocked."""
import os,sys,tempfile,time,json,socket
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ.update(API_TOKEN='123456789:'+('X'*35),ADMIN_ID='9999',DB_PATH=str(Path(tmp)/'dispatch.db'),
            CHANNEL_USERNAMES='',TELEGRAM_API_ID='',TELEGRAM_API_HASH='',USERNAME_LOOKUP_ENABLED='false')
        import app
        from telebot import types,apihelper
        app.bot.raw.threaded=False
        app.install_identity_observer()
        calls=[]
        def transport(_token,api_method,params=None,**kw):
            calls.append(api_method)
            if api_method=='answerCallbackQuery':return True
            assert api_method in {'sendMessage','editMessageText'},api_method
            result={'message_id':500+len(calls),'date':int(time.time()),
                'chat':{'id':int(params['chat_id']),'type':'private'},'text':params['text'],
                'from':{'id':123456789,'is_bot':True,'first_name':'Test bot'}}
            if params.get('reply_markup'):
                markup=params['reply_markup']
                markup=json.loads(markup) if isinstance(markup,str) else markup
                if 'inline_keyboard' in markup:result['reply_markup']=markup
            return result
        sequence=0
        def user(uid=101):return {'id':uid,'is_bot':False,'first_name':'Isolated tester','username':f'fixture{uid}'}
        def message(mid,uid=101,**extra):
            nonlocal sequence
            sequence+=1
            raw={'message_id':mid,'date':int(time.time()),'chat':{'id':uid,'type':'private'},'from':user(uid),**extra}
            app.bot.raw.process_new_updates([types.Update.de_json({'update_id':sequence,'message':raw})])
        def callback(data,uid=101,key=None):
            nonlocal sequence
            sequence+=1
            raw={'id':key or 'callback-'+str(sequence),'from':user(uid),'chat_instance':'isolated',
                'data':data,'message':{'message_id':500,'date':int(time.time()),'chat':{'id':uid,'type':'private'},'text':'Fixture'}}
            app.bot.raw.process_new_updates([types.Update.de_json({'update_id':sequence,'callback_query':raw})])
        with patch.object(apihelper,'_make_request',side_effect=transport),patch.object(socket.socket,'connect',side_effect=AssertionError('Network forbidden')):
            message(1,text='/start')
            message(2,text='/report')
            request=app.get_state(101)['request_id']
            message(3,users_shared={'request_id':request,'users':[{'user_id':919191,'username':'chosen_fixture'}]})
            message(4,text='Описание для изолированного сценария')
            message(5,text='Доказательство для изолированного сценария')
            callback('report:submit',key='submit-first')
            callback('report:submit',key='submit-first')
            rows=app.conn.execute('SELECT * FROM complaints').fetchall();assert len(rows)==1
            cid=rows[0]['id']
            assert rows[0]['target_user_id']==919191 and rows[0]['from_id']==101
            assert rows[0]['status']=='review' and rows[0]['identity_source']=='bot_users_shared'
            assert app.identity_store.public_matches('919191')['total']==0
            callback(f'admin:approve:{cid}:0',uid=9999)
            assert app.identity_store.public_matches('919191')['total']==1
            assert '919191' in app.format_check_text('919191')
            message(11,text='/report')
            message(12,text='Пересылка',forward_origin={'type':'user','date':1,
                'sender_user':{'id':818181,'is_bot':True,'first_name':'Isolated account'}})
            message(13,text='Описание другого изолированного сценария')
            message(14,text='Доказательство другого изолированного сценария')
            callback('report:submit')
            row=app.conn.execute('SELECT * FROM complaints ORDER BY id DESC LIMIT 1').fetchone()
            assert row['target_user_id']==818181 and row['identity_source']=='bot_forward' and row['status']=='review'
            message(21,text='/report')
            message(22,text='Скрытая пересылка',forward_origin={'type':'hidden_user','date':1,'sender_user_name':'Hidden fixture'})
            assert app.get_state(101)['state']==app.STATE_WAITING_FOR_REPORT_TARGET
            assert not app.get_state(101).get('selected_id')
            message(23,text='/cancel')
            cur=app.conn.execute('''INSERT INTO complaints
                (target,from_id,note,evidence,timestamp,status) VALUES ('@legacy_fixture',101,'Legacy fixture','[]',?,'accepted')''',
                ('2000-01-01T00:00:00+00:00',));old=cur.lastrowid;app.conn.commit()
            message(31,uid=9999,text=f'/clarify #{old}')
            request=app.get_state(9999)['request_id']
            message(32,uid=9999,users_shared={'request_id':request,'users':[{'user_id':717171,'username':'new_fixture'}]})
            assert app.get_complaint_by_id(old)['target_user_id'] is None
            assert app.get_complaint_by_id(old)['candidate_user_id']==717171
            callback(f'admin:identity:verify:{old}:717171',uid=101)
            assert app.get_state(101).get('state')!=app.STATE_IDENTITY_EVIDENCE
            callback(f'admin:identity:verify:{old}:717171',uid=9999)
            message(33,uid=9999,text='Изолированное доказательство: переписка #100')
            assert app.get_complaint_by_id(old)['target_user_id']==717171
            callback(f'admin:identity:unlink:{old}:0',uid=9999)
            message(34,uid=9999,text='Изолированная отмена: переписка #101')
            assert app.get_complaint_by_id(old)['target_user_id'] is None
            assert app.identity_store.recover_known_links()==0
            assert app.conn.execute("SELECT COUNT(*) FROM identity_review_audit WHERE actor_id=9999 AND action='unlink'").fetchone()[0]==1
            callback('admin:identity:filter:unknown:0',uid=9999)
            callback('admin:identity:audit:0',uid=9999)
        app.conn.close()
        print('PASS: real TeleBot.process_new_updates dispatch; synthetic isolated picker, forward, hidden origin, report persistence, moderation, numeric search, clarification, admin review/revocation and replay. HTTP mocked; no production data or accounts used.')

if __name__=='__main__':main()
