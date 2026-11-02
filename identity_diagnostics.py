"""Privacy-preserving audit of every complaint; no remote account resolution."""
import json
from collections import Counter, defaultdict
from identity_db import normalized_username, MAX_USER_ID

CATEGORIES = {
    'confirmed':'ID подтверждён',
    'legacy':'Числовые данные без подтверждения происхождения',
    'telegram':'Telegram передал аккаунт; связь требует проверки',
    'ambiguous':'Ник наблюдался у разных аккаунтов',
    'changed':'Прежний ник; текущее имя отличается',
    'candidate':'Есть кандидат; нужны доказательства связи',
    'unknown':'Нет достаточных сведений об аккаунте',
}


def audit_database(conn, complaint_id=None):
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rows=conn.execute('SELECT * FROM complaints'+(' WHERE id=?' if complaint_id is not None else ''),
                      (complaint_id,) if complaint_id is not None else ()).fetchall()
    aliases=defaultdict(set);direct=defaultdict(set);history=defaultdict(set)
    current={r['user_id']:r['current_username'] for r in conn.execute('SELECT * FROM telegram_identities')}
    observations=conn.execute('SELECT user_id,username,source FROM identity_observations').fetchall()
    for o in observations:
        if o['username']:
            aliases[o['username']].add(o['user_id'])
            if o['source'] in {'bot_users_shared','bot_forward'}:direct[o['username']].add(o['user_id'])
    for h in conn.execute("SELECT user_id,username FROM telegram_username_history WHERE source!='admin_revoked'"):
        history[h['username']].add(h['user_id'])
    choices=defaultdict(set)
    for r in conn.execute("SELECT * FROM identity_candidates WHERE status='candidate'"):
        choices[r['complaint_id']].add(r['user_id'])
    findings=[]
    for c in rows:
        alias=normalized_username(c['target']) if not str(c['target']).strip().isdecimal() else None
        ids=aliases.get(alias,set())|history.get(alias,set())
        numeric=str(c['target']).strip().isascii() and str(c['target']).strip().isdigit()
        legacy_fields=[]
        try: evidence=json.loads(c['evidence'] or '[]')
        except (ValueError,TypeError):evidence=[]
        if isinstance(evidence,list):
            for e in evidence:
                if isinstance(e,dict):
                    for key in ('target_user_id','reported_id','subject_user_id'):
                        value=e.get(key)
                        if str(value).isascii() and str(value).isdigit() and 0<int(value)<=MAX_USER_ID:legacy_fields.append(key)
        pending=bool(choices[c['id']]) or c['candidate_status']=='candidate'
        conflict=len(ids)>1
        linked=isinstance(c['target_user_id'],int) and 0<c['target_user_id']<=MAX_USER_ID
        invalid_link=c['target_user_id'] is not None and not linked
        if invalid_link:category='ambiguous'
        elif linked:category='confirmed'
        elif numeric or legacy_fields:category='legacy'
        elif conflict:category='ambiguous'
        elif direct.get(alias):category='telegram'
        elif pending:category='candidate'
        elif ids and all(current.get(uid)!=alias for uid in ids):category='changed'
        else:category='unknown'
        findings.append({'complaint_id':c['id'],'category':category,
            'reason':'Неверный формат сохранённого ID; нужна проверка' if invalid_link else CATEGORIES[category],
            'linked':linked,'invalid_link':invalid_link,
            'candidate':pending,'observed_accounts':len(aliases.get(alias,set())),
            'history_accounts':len(history.get(alias,set())), 'conflict':conflict,
            'telegram_subject_observed':bool(direct.get(alias)),
            'numeric_claim':numeric,'legacy_structured_claims':len(legacy_fields),
            'reporter_id_is_subject_evidence':False})
    categories=dict(Counter(f['category'] for f in findings))
    legacy={};legacy_findings=[]
    if 'reports' in tables:
        old=conn.execute('SELECT id,reported_id FROM reports').fetchall()
        legacy={'rows':len(old),'numeric_claims':sum(str(r[1] or '').strip().isascii() and str(r[1] or '').strip().isdigit() for r in old),
                'username_claims':sum(bool(normalized_username(r[1])) and not str(r[1] or '').strip().isdigit() for r in old)}
        legacy_findings=[{'legacy_report_id':r[0],'category':'legacy' if str(r[1] or '').strip().isdigit() else 'unknown',
                          'verified_provenance':False,'mapped_to_complaint':False} for r in old]
    return {'total':len(findings),'categories':categories,'conflicts':sum(f['conflict'] for f in findings),
            'legacy_reports':legacy,'legacy_findings':legacy_findings,'findings':findings}
