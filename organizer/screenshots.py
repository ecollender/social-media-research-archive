"""Root-level JPEG/PNG import and reversible reference links. Files are never moved."""
import database
import hashlib,json,sqlite3,time
from pathlib import Path


def init(db):
    with database.connection(db) as c:
        c.execute('CREATE TABLE IF NOT EXISTS screenshot_links (screenshot_id TEXT PRIMARY KEY, post_id TEXT NOT NULL)')


def scan(root,db):
    count=0;warnings=[]
    with database.connection(db,timeout=30) as c:
        for p in sorted(root.iterdir()):
            if p.suffix.lower() not in ('.jpg','.jpeg','.png') or not p.is_file():continue
            try:
                p.resolve().relative_to(root.resolve())
                # Content identity means rescans, renames and duplicate copies do not add duplicate cards.
                digest=hashlib.sha256()
                with p.open('rb') as f:
                    for chunk in iter(lambda:f.read(1024*1024),b''):digest.update(chunk)
                pid='screenshot_'+digest.hexdigest()
                old=c.execute('SELECT thumbnail FROM posts WHERE id=?',(pid,)).fetchone()
                if old and (root/old[0]).is_file():continue
                stamp=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(p.stat().st_mtime))
                c.execute('INSERT INTO posts(id,folder,title,creator,caption,summary,transcript,comments,source,posted,thumbnail,video,tags,notes,platform,assets,custom_title) VALUES (?,?,?,"","","","","[]","",?,?,"","","","screenshot","[]",0) ON CONFLICT(id) DO UPDATE SET thumbnail=excluded.thumbnail', (pid,'.',p.stem,stamp,p.name))
                count+=1
            except (OSError,ValueError) as e:warnings.append(p.name+': '+str(e))
    return count,warnings


def link(db,sid,pid):
    with database.connection(db,timeout=30) as c:
        source=c.execute('SELECT platform FROM posts WHERE id=?',(sid,)).fetchone()
        if not source or source[0]!='screenshot':raise ValueError('Choose a standalone screenshot.')
        if pid:
            dest=c.execute('SELECT platform FROM posts WHERE id=?',(pid,)).fetchone()
            if not dest or dest[0] not in ('tiktok','instagram'):raise ValueError('Choose a TikTok or Instagram post.')
            c.execute('INSERT INTO screenshot_links(screenshot_id,post_id) VALUES (?,?) ON CONFLICT(screenshot_id) DO UPDATE SET post_id=excluded.post_id',(sid,pid))
        else:c.execute('DELETE FROM screenshot_links WHERE screenshot_id=?',(sid,))


def references(db,pid):
    with database.connection(db) as c:
        c.row_factory=sqlite3.Row
        rows=c.execute('SELECT p.id,p.title,p.thumbnail FROM screenshot_links l JOIN posts p ON p.id=l.screenshot_id WHERE l.post_id=? ORDER BY p.title',(pid,)).fetchall()
        parent=c.execute('SELECT p.id,p.title FROM screenshot_links l JOIN posts p ON p.id=l.post_id WHERE l.screenshot_id=?',(pid,)).fetchone()
    return {'references':[dict(r) for r in rows], 'attached_to':dict(parent) if parent else None}
