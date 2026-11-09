"""Research annotations stored separately from imported post data."""
import database
import json, sqlite3, time, uuid
from pathlib import Path

RENAMES={'Indigeneity & colonialism':'Colonialism & indigeneity','Knowledge & belief':'Knowledge, belief & religion','Jewish identity, Israel & Palestine':'Jewish identity, antisemitism, Israel & Palestine'}
PASS_ID='expanded-categories-v1'


def connect(db):
    return database.connection(db)


def init(db):
    with connect(db) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='research_migrations'").fetchone():
            with database.connection(Path(db).with_name('archive-before-research-update-'+str(time.time_ns())+'.sqlite')) as dest:c.backup(dest)
        c.execute('CREATE TABLE IF NOT EXISTS research_migrations(name TEXT PRIMARY KEY)')
        c.execute('CREATE TABLE IF NOT EXISTS import_issues(post_id TEXT PRIMARY KEY, reason TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS research_flags(post_id TEXT PRIMARY KEY, highlighted INTEGER NOT NULL DEFAULT 0)')
        c.execute('CREATE TABLE IF NOT EXISTS manual_comments(id TEXT PRIMARY KEY, post_id TEXT NOT NULL, text TEXT NOT NULL, username TEXT NOT NULL, url TEXT NOT NULL, reply_to TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS category_pass(post_id TEXT NOT NULL, pass_id TEXT NOT NULL, outcome TEXT NOT NULL, additions TEXT NOT NULL DEFAULT "[]", PRIMARY KEY(post_id,pass_id))')
        if not c.execute('SELECT 1 FROM research_migrations WHERE name="expanded-taxonomy"').fetchone():
            for row in c.execute('SELECT id,categories FROM classification').fetchall():
                cats=list(dict.fromkeys(RENAMES.get(x,x) for x in json.loads(row['categories'])))
                c.execute('UPDATE classification SET categories=? WHERE id=?',(json.dumps(cats),row['id']))
            c.execute('INSERT INTO research_migrations VALUES ("expanded-taxonomy")')


def details(db,pid):
    with connect(db) as c:
        flag=c.execute('SELECT highlighted FROM research_flags WHERE post_id=?',(pid,)).fetchone()
        comments=c.execute('SELECT * FROM manual_comments WHERE post_id=? ORDER BY created,id',(pid,)).fetchall()
    return {'highlighted':bool(flag and flag[0]),'manual_comments':[dict(r) for r in comments]}


def highlight(db,pid,value):
    if not isinstance(value,bool):raise ValueError('Invalid highlight value.')
    with connect(db) as c:
        if not c.execute('SELECT 1 FROM posts WHERE id=?',(pid,)).fetchone():raise ValueError('Post not found.')
        c.execute('INSERT INTO research_flags VALUES (?,?) ON CONFLICT(post_id) DO UPDATE SET highlighted=excluded.highlighted',(pid,int(value)))


def save_comment(db,body):
    pid=body.get('post_id','');cid=body.get('id')
    values={}
    for name,limit in [('text',30000),('username',300),('url',3000),('reply_to',300)]:
        v=body.get(name,'')
        if not isinstance(v,str) or len(v)>limit:raise ValueError('Invalid or oversized '+name+'.')
        values[name]=v.strip()
    if not values['text']:raise ValueError('Enter the comment text.')
    if values['url'] and not values['url'].lower().startswith(('https://','http://')):raise ValueError('Comment links must begin with https:// or http://.')
    with connect(db) as c:
        row=c.execute('SELECT platform FROM posts WHERE id=?',(pid,)).fetchone()
        if not row or row['platform'] not in ('tiktok','instagram'):raise ValueError('Add comments to a TikTok or Instagram post.')
        stamp=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        if cid:
            if not isinstance(cid,str) or values['reply_to']==cid:raise ValueError('A comment cannot reply to itself.')
            old=c.execute('SELECT post_id FROM manual_comments WHERE id=?',(cid,)).fetchone()
            if not old or old['post_id']!=pid:raise ValueError('Comment not found in this post.')
            c.execute('UPDATE manual_comments SET text=?,username=?,url=?,reply_to=?,updated=? WHERE id=?',(values['text'],values['username'],values['url'],values['reply_to'],stamp,cid))
        else:
            cid='manual_'+uuid.uuid4().hex
            c.execute('INSERT INTO manual_comments VALUES (?,?,?,?,?,?,?,?)',(cid,pid,values['text'],values['username'],values['url'],values['reply_to'],stamp,stamp))
    return cid


def delete_comment(db,pid,cid):
    with connect(db) as c:c.execute('DELETE FROM manual_comments WHERE id=? AND post_id=?',(cid,pid))


def comment_rows(db,pid):
    # Compatible with the existing threaded-comment renderer; source remains explicit.
    return [{'id':r['id'],'text':r['text'],'ownerUsername':r['username'],'repliesToId':r['reply_to'],'url':r['url'],'manual':True,'likesCount':None} for r in details(db,pid)['manual_comments']]


def pass_pending(db):
    with connect(db) as c:
        rows=c.execute('SELECT p.id,a.categories FROM posts p JOIN classification a ON a.id=p.id WHERE p.platform IN ("tiktok","instagram") AND a.reviewed=0 AND a.outcome="complete" AND NOT EXISTS (SELECT 1 FROM import_issues i WHERE i.post_id=p.id) AND NOT EXISTS (SELECT 1 FROM category_pass s WHERE s.post_id=p.id AND s.pass_id=?) ORDER BY p.posted DESC,p.id',(PASS_ID,)).fetchall()
    # Keep primary and existing secondary categories. AI never grows past two secondaries.
    return [r['id'] for r in rows if 0<len(json.loads(r['categories']))<3]


def mark_pass(db,pid,additions,outcome):
    with connect(db) as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT categories,reviewed FROM classification WHERE id=?',(pid,)).fetchone()
        applied=[]
        if row and not row['reviewed'] and outcome=='complete':
            cats=json.loads(row['categories'])
            if cats:
                applied=[x for x in additions if x not in cats][:max(0,3-len(cats))]
                c.execute('UPDATE classification SET categories=? WHERE id=? AND reviewed=0',(json.dumps(cats+applied),pid))
        c.execute('INSERT INTO category_pass(post_id,pass_id,outcome,additions) VALUES (?,?,?,?) ON CONFLICT(post_id,pass_id) DO UPDATE SET outcome=excluded.outcome,additions=excluded.additions',(pid,PASS_ID,outcome,json.dumps(applied)))
    return applied
