"""Explicit live smoke test: send ONE message to ADMIN_ID only; never start polling."""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--send-to-admin', action='store_true',
                        help='Explicitly authorize one Telegram message to ADMIN_ID from private .env')
    args = parser.parse_args()
    if not args.send_to_admin:
        parser.error('Use --send-to-admin to send one test message. There is no bulk-send mode.')
    try:
        import telebot
        from telebot import types
        from dotenv import load_dotenv
    except ImportError:
        print('FAIL: install requirements.txt first.')
        return 2
    from community_store import CommunityStore
    from delivery import classify_error
    from message_content import send_content, utf16_length
    from ui import UI, ICON_CANDIDATES
    load_dotenv(ROOT / '.env', override=False)
    token = os.getenv('API_TOKEN','')
    admin = os.getenv('ADMIN_ID','')
    if not token or token == 'CHANGE_ME' or not admin.isdecimal() or int(admin) <= 0:
        print('FAIL: set API_TOKEN and ADMIN_ID in private .env. Never send them in chat or GitHub.')
        return 2
    bot = telebot.TeleBot(token, threaded=False)
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    try:
        store = CommunityStore(conn, threading.RLock(), int(admin))
        store.initialize()
        ui = UI(types, store)
        bot.get_me()
        chat = bot.get_chat(int(admin))
        if chat.type != 'private':
            print('FAIL: ADMIN_ID must resolve to a private chat; no message was sent.')
            return 2
        count = ui.load_pack(bot)
        icon = ui.mapping.get('search') or ui.catalog[0]
        prefix = icon['emoji'] + ' '
        title = 'Проверка оформления LamaScambaseBot'
        text = prefix + title + '\n\nПроверь анимацию иконки и три цвета кнопок. Это не рассылка.'
        payload = {'kind':'text','text':text,'entities':[
            {'type':'custom_emoji','offset':0,'length':utf16_length(icon['emoji']),
             'custom_emoji_id':icon['id']},
            {'type':'bold','offset':utf16_length(prefix),'length':utf16_length(title)},
        ]}
        rows = []
        for label, style, route in (
            ('Проверить аккаунт','primary','menu:check'),
            ('Поддержка','success','menu:support'),
            ('Настройки уведомлений','danger','menu:notifications'),
        ):
            rows.append([types.InlineKeyboardButton(label, callback_data=route, style=style,
                                                     icon_custom_emoji_id=icon['id'])])
        message = send_content(bot, types, int(admin), payload, reply_markup=ui.keyboard(rows))
        print('PASS: Telegram acknowledged the test message and preserved entities, icons and styles.')
        print('Message ID:', message.message_id)
        print('Pack items:', count, '| Automatically mapped roles:', len(ui.mapping), '/', len(ICON_CANDIDATES))
        missing = sorted(set(ICON_CANDIDATES)-set(ui.mapping))
        if missing:
            print('Needs selection in Admin / Appearance:', ', '.join(missing))
        print('Check the received message visually on your Telegram client. No production database was opened.')
        return 0
    except Exception as exc:
        code, retry = classify_error(exc)
        print('FAIL:', code, '(' + type(exc).__name__ + ')')
        if retry:
            print('retry_after:', retry)
        print('No automatic retry. Check the admin chat before running the test again.')
        return 1
    finally:
        conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
