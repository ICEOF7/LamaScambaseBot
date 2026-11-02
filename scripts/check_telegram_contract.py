"""Offline check against the installed REAL pyTelegramBotAPI, without Telegram calls."""
from __future__ import annotations
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    try:
        import telebot
        from telebot import apihelper, types
    except ImportError:
        print('FAIL: install requirements.txt first; pyTelegramBotAPI is missing.')
        return 2
    from message_content import send_content
    token = str(1234567) + ':' + 'X' * 35
    bot = telebot.TeleBot(token, threaded=False)
    entity = {'type':'custom_emoji', 'offset':0, 'length':2, 'custom_emoji_id':'7000001'}
    payload = {'kind':'text', 'text':'🚀 Проверка', 'entities':[entity]}
    markup = types.InlineKeyboardMarkup()
    for style in ('primary', 'success', 'danger'):
        markup.add(types.InlineKeyboardButton(style, callback_data='menu:check',
                                              style=style, icon_custom_emoji_id='7000001'))
    calls = []
    def transport(_token, method_name, params=None, **kwargs):
        if method_name != 'sendMessage':
            raise AssertionError('Unexpected Telegram method: ' + method_name)
        calls.append((method_name, params))
        def decoded(value):
            return json.loads(value) if isinstance(value, str) else value
        return {'message_id':501, 'date':1, 'chat':{'id':101,'type':'private'},
                'from':{'id':1234567,'is_bot':True,'first_name':'Synthetic'},
                'text':params['text'], 'entities':decoded(params['entities']),
                'reply_markup':decoded(params['reply_markup'])}
    try:
        with patch.object(apihelper, '_make_request', side_effect=transport):
            result = send_content(bot, types, 101, payload, reply_markup=markup)
        assert len(calls) == 1 and result.message_id == 501
        assert result.entities[0].custom_emoji_id == '7000001'
        assert 'parse_mode' not in calls[0][1]
        wire = calls[0][1]['reply_markup']
        wire = json.loads(wire) if isinstance(wire, str) else wire
        assert [r[0]['style'] for r in wire['inline_keyboard']] == ['primary','success','danger']
    except Exception as exc:
        print('FAIL: pyTelegramBotAPI contract mismatch (' + type(exc).__name__ + ').')
        return 1
    print('PASS: real library serializes native entities, custom emoji IDs and all three button colors.')
    print('No network calls, polling, files or real messages were used.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
