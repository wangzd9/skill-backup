#!/usr/bin/env python3
"""Lossless legacy-input compaction. Only an explicitly supplied offline copy."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from workflow_input_pages import compress_legacy_result,expand_result


def rowid_identity(db):
    identities={}
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        quoted='"'+table.replace('"','""')+'"'
        info=list(db.execute('PRAGMA table_info('+quoted+')'))
        keys=[c[1] for c in sorted(info,key=lambda c:c[5]) if c[5]]
        if not keys:raise ValueError('TABLE_WITHOUT_STABLE_KEY:'+table)
        columns=','.join('"'+key.replace('"','""')+'"' for key in keys)
        try:rows=list(db.execute('SELECT rowid,'+columns+' FROM '+quoted+' ORDER BY rowid'))
        except sqlite3.OperationalError:continue  # WITHOUT ROWID has no hidden identity.
        identities[table]=(keys,rows)
    return identities


def restore_rowids(db, identities):
    repaired=0
    for table,(keys,rows) in identities.items():
        quoted='"'+table.replace('"','""')+'"'
        columns=','.join('"'+key.replace('"','""')+'"' for key in keys)
        current=list(db.execute('SELECT rowid,'+columns+' FROM '+quoted+' ORDER BY rowid'))
        if current==rows:continue
        if {tuple(r[1:]) for r in current}!={tuple(r[1:]) for r in rows}:
            raise ValueError('TABLE_IDENTITIES_CHANGED:'+table)
        maximum=max([abs(r[0]) for r in [*rows,*current]],default=0)
        db.execute('UPDATE '+quoted+' SET rowid=rowid-?',(2*maximum+1,))
        where=' AND '.join('"'+key.replace('"','""')+'" IS ?' for key in keys)
        for row in rows:db.execute('UPDATE '+quoted+' SET rowid=? WHERE '+where,row)
        repaired+=1
    db.commit()
    if rowid_identity(db)!=identities:raise ValueError('ROWID_RESTORE_FAILED')
    return repaired


def compact(path, *, threshold=1048576):
    path=Path(path).resolve()
    live=Path.home()/'.hermes/role-pool.runtime/business.sqlite3'
    if path==live.resolve():raise ValueError('OFFLINE_COPY_REQUIRED')
    if not path.is_file():raise FileNotFoundError(path)
    before=path.stat().st_size;changed=0;raw_bytes=0;stored_bytes=0;proof=hashlib.sha256()
    with sqlite3.connect(path) as db:
        if db.execute('PRAGMA quick_check(1)').fetchone()[0]!='ok':raise ValueError('SOURCE_DATABASE_CHECK_FAILED')
        identities=rowid_identity(db)
        db.execute('BEGIN IMMEDIATE')
        rows=db.execute("SELECT rowid,result FROM commands WHERE verb='work_inputs' AND LENGTH(CAST(result AS BLOB))>=? ORDER BY rowid",(threshold,))
        # Updates use a separate cursor and preserve every rowid, command ID,
        # fingerprint, creation time, decision and non-input business field.
        for rowid,raw in rows:
            original=json.loads(raw);compressed=compress_legacy_result(original,threshold=threshold)
            if compressed==original:continue
            expanded=expand_result(None,compressed)
            restored=json.dumps(expanded,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            canonical=json.dumps(original,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            if restored!=canonical:raise ValueError('LEGACY_INPUT_ROUNDTRIP_MISMATCH:'+str(rowid))
            encoded=json.dumps(compressed,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            db.execute('UPDATE commands SET result=? WHERE rowid=?',(encoded,rowid))
            proof.update(str(rowid).encode()+b':'+hashlib.sha256(canonical.encode()).digest())
            changed+=1;raw_bytes+=len(raw.encode());stored_bytes+=len(encoded.encode())
        db.commit()
        db.execute('VACUUM')
        repaired=restore_rowids(db,identities)
        check=db.execute('PRAGMA quick_check(1)').fetchone()[0]
        if check!='ok':raise ValueError('COMPACTED_DATABASE_CHECK_FAILED')
        counts={table:db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                for table in ('commands','events','works','candidates','verifications')}
    return {'database':str(path),'before_bytes':before,'after_bytes':path.stat().st_size,
        'changed_commands':changed,'old_result_bytes':raw_bytes,'new_result_bytes':stored_bytes,
        'roundtrip_verified':changed,'roundtrip_digest':proof.hexdigest(),'counts':counts,
        'quick_check':check,'rowid_tables_verified':len(identities),'rowid_tables_repaired':repaired,
        'attachments_added':0,'online_cutover_performed':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline-copy',required=True,type=Path)
    parser.add_argument('--report',required=True,type=Path)
    args=parser.parse_args();result=compact(args.offline_copy)
    args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))
