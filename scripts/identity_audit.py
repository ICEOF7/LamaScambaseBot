"""Read-only per-complaint identity reasons, without account IDs or usernames."""
import argparse,json,sqlite3,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from identity_diagnostics import audit_database

def main():
    p=argparse.ArgumentParser();p.add_argument('--db',type=Path,required=True)
    p.add_argument('--case',type=int);p.add_argument('--all-reasons',action='store_true');a=p.parse_args()
    path=a.db.resolve(strict=True)
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON')
        result=audit_database(c,a.case)
    if not a.all_reasons:
        result.pop('legacy_findings')
        if a.case is None:result.pop('findings')
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
