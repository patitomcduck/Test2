import os
import sqlite3
import time
import zipfile
import json
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DATA_DIR=Path(os.environ.get('DATA_DIR','/app/data'))
DB_PATH=DATA_DIR/'pos.db'
BACKUP_DIR=DATA_DIR/'backups'
KEEP=14

def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat()+'Z'

def get_timezone():
    try:
        conn=sqlite3.connect(DB_PATH); row=conn.execute("SELECT store_timezone FROM store_settings WHERE id=1").fetchone(); conn.close()
        return ZoneInfo((row[0] if row and row[0] else 'UTC'))
    except Exception:
        return ZoneInfo('UTC')

def make_backup():
    BACKUP_DIR.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
    out=BACKUP_DIR/f'collector-pos-backup-{stamp}.zip'
    tmp=DATA_DIR/f'.auto-backup-{stamp}.db'
    src=sqlite3.connect(DB_PATH); dst=sqlite3.connect(tmp); src.backup(dst); dst.close(); src.close()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as zf:
        zf.write(tmp,'pos.db')
        brand=DATA_DIR/'brand'
        if brand.exists():
            for f in brand.rglob('*'):
                if f.is_file(): zf.write(f,f'brand/{f.relative_to(brand)}')
        zf.writestr('backup-info.json',json.dumps({'created_at':now_iso(),'version':'3.0.0-desktop-preview','trigger':'automatic'},indent=2))
    tmp.unlink(missing_ok=True)
    try:
        conn=sqlite3.connect(DB_PATH); conn.execute("INSERT INTO backup_runs(filename,trigger,status,size_bytes,created_at,details) VALUES (?,?,?,?,?,?)",(out.name,'automatic','ok',out.stat().st_size,now_iso(),None)); conn.commit(); conn.close()
    except Exception: pass
    files=sorted(BACKUP_DIR.glob('collector-pos-backup-*.zip'),key=lambda p:p.stat().st_mtime,reverse=True)
    for old in files[KEEP:]: old.unlink(missing_ok=True)
    print(f'[backup] {out.name}',flush=True)

def seconds_to_next():
    tz=get_timezone(); now=datetime.now(tz)
    target=now.replace(hour=2,minute=30,second=0,microsecond=0)
    if target<=now: target+=timedelta(days=1)
    return max(60,(target-now).total_seconds())

if __name__=='__main__':
    while True:
        try:
            wait=seconds_to_next(); print(f'[backup] next run in {int(wait)}s',flush=True); time.sleep(wait); make_backup()
        except Exception as exc:
            print(f'[backup] error: {exc}',flush=True); time.sleep(3600)
