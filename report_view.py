"""Compact public complaint cards; unconfirmed candidate IDs are never rendered."""
from __future__ import annotations
import html
from datetime import datetime
from identity_db import MAX_USER_ID


def excerpt(value, limit=120):
    """Collapse whitespace and truncate between words, not in the middle of one."""
    value = ' '.join(str(value or '').split())
    if len(value) <= limit:
        return value
    prefix = value[:limit + 1]
    if ' ' not in prefix:
        return 'Длинное описание — открой запись целиком.'
    return prefix.rsplit(' ', 1)[0].rstrip(' ,;:—-') + '…'


def short_date(value):
    raw = str(value or '')
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        return parsed.strftime('%d.%m.%Y')
    except (ValueError, TypeError):
        return 'Дата не указана'


def complaint_card(row):
    target = html.escape(str(row['target']), quote=False)
    confirmed = row.get('target_user_id')
    if not isinstance(confirmed,int) or isinstance(confirmed,bool) or not 0<confirmed<=MAX_USER_ID:
        confirmed = None
    heading = f'[[icon:document]] <b>#{int(row["id"])} · {target}</b>'
    if confirmed:
        heading += f' · ID <code>{int(confirmed)}</code>'
    lines = [heading]
    if not confirmed and row.get('candidate_status') == 'candidate':
        lines.append('<i>Привязка Telegram ID ожидает проверки модератором</i>')
    current = row.get('current_username')
    if confirmed and current and current.lower() != str(row['target']).lower():
        lines.append(f'Последний известный ник: <b>{html.escape(current, quote=False)}</b>')
    lines.append(html.escape(excerpt(row.get('note')), quote=False).replace('[', '&#91;'))
    lines.append(f'<i>{short_date(row.get("timestamp"))}</i>')
    return '\n'.join(lines)
