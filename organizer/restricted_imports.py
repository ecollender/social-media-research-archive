"""Remove restricted-only Instagram imports from the active archive, with recovery copies."""
import json,time
from pathlib import Path
import database


def read(path):
    try:
        value=json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value,dict) else {}
    except (FileNotFoundError,ValueError):return {}


def restricted_only(folder):
    if folder.is_symlink() or not folder.is_dir():return False
    post=read(folder/'post.json')
    raw=read(folder/'instagram-original.json')
    if raw.get('error')!='restricted_page' and post.get('error')!='restricted_page':return False
    # A later failed scrape must never discard previously captured useful content.
    if any(isinstance(obj.get(k),str) and obj[k].strip() for obj in (post,raw) for k in ('text','caption','transcript')):return False
    for f in folder.iterdir():
        if f.is_symlink():return False
        if f.is_file() and f.stat().st_size:
            if f.suffix.lower() in ('.jpg','.jpeg','.png','.webp','.mp4','.mov','.webm','.vtt','.srt'):return False
            if f.name.lower()=='transcript.txt' and f.read_text(encoding='utf-8',errors='replace').strip():return False
    return True


def cleanup(root,db):
    candidates=[];warnings=[]
    for folder in sorted(root.glob('instagram_*')):
        try:
            if restricted_only(folder):candidates.append(folder)
        except OSError as e:warnings.append(folder.name+': could not inspect restricted import; kept in place ('+str(e)+')')
    if not candidates:return [],warnings
    recovery=root/'Removed-restricted-imports'
    if recovery.is_symlink():raise ValueError('Recovery folder cannot be a symbolic link.')
    batch=recovery/str(time.time_ns());batch.mkdir(parents=True)
    with database.connection(db) as source:
        with database.connection(batch/'archive-before-removal.sqlite') as dest:source.backup(dest)
    removed=[]
    for folder in candidates:
        destination=batch/folder.name
        folder.rename(destination)
        try:
            with database.connection(db) as c:
                pid=folder.name
                for table,key in [('classification','id'),('research_flags','post_id'),('manual_comments','post_id'),('category_pass','post_id'),('import_issues','post_id'),('posts','id')]:
                    c.execute('DELETE FROM '+table+' WHERE '+key+'=?',(pid,))
                # Screenshots survive independently, with their own categories and annotations.
                c.execute('DELETE FROM screenshot_links WHERE post_id=?',(pid,))
        except Exception:
            destination.rename(folder)
            raise
        removed.append(folder.name)
    (batch/'removed.json').write_text(json.dumps({'removed_from_active_archive':removed,'reason':'restricted_page with no saved media, caption or transcript'},indent=2),encoding='utf-8')
    return removed,warnings
