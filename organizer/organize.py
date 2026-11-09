#!/usr/bin/env python3
"""Local archive browser with optional, explicitly selected Gemini classification."""
import database
import categorize, screenshots, research, restricted_imports
import csv, io, json, mimetypes, re, secrets, sqlite3, sys, threading, time, webbrowser
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit, parse_qs, quote

METRICS = {'likes':'diggCount', 'views':'playCount', 'total_comments':'commentCount', 'shares':'shareCount', 'saves':'collectCount'}

def metric_value(value):
    if isinstance(value, bool): return None
    if isinstance(value, int) and 0 <= value <= 9223372036854775807: return value
    if isinstance(value, str) and value.strip().isdigit():
        n = int(value.strip())
        return n if n <= 9223372036854775807 else None
    return None

COLUMNS = ['id','folder','title','creator','caption','summary','transcript','comments','source','posted','thumbnail','video','tags','notes']

def read_json(path, default):
    try: return json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError): return default

def text(v):
    if v is None: return ''
    return v if isinstance(v,str) else json.dumps(v,ensure_ascii=False,indent=2)

def inside(root, path):
    p = path.resolve()
    try: p.relative_to(root.resolve())
    except ValueError: raise ValueError('File is outside the archive.')
    return p


def video_id(value):
    match = re.search(r'/video/(\d+)', text(value))
    return match[1] if match else None

def comments_for_post(raw, post):
    """Shared Apify datasets contain multiple videos. Keep unknown-source rows."""
    if not isinstance(raw, list): return []
    target = video_id(post.get('webVideoUrl')) or text(post.get('id'))
    if not target: return raw
    selected = []
    for comment in raw:
        if not isinstance(comment, dict): continue
        source = next((video_id(comment.get(k)) for k in ('videoWebUrl', 'submittedVideoUrl', 'input') if video_id(comment.get(k))), None)
        if source is None or source == target: selected.append(comment)
    return selected

def comment_threads(raw):
    """Preserve captured ordering, nest replies, and keep missing-parent replies."""
    nodes = {}; order = []
    def collect(items, inferred_parent=None):
        for comment in items:
            if not isinstance(comment, dict): continue
            key = text(comment.get('cid') or comment.get('id')) or 'anonymous-' + str(len(order))
            parent = comment.get('repliesToId', comment.get('parentCommentId', inferred_parent))
            parent = text(parent) if parent not in (None, '', 0, '0') else (text(inferred_parent) if inferred_parent else None)
            if key not in nodes:
                nodes[key] = {'comment': {k:v for k,v in comment.items() if k != 'replies'}, 'id': key,
                              'parent': parent, 'children': [], 'reply': bool(parent), 'notice': ''}
                order.append(key)
            elif inferred_parent and not nodes[key]['parent']:
                nodes[key]['parent'] = text(inferred_parent); nodes[key]['reply'] = True
            nested = comment.get('replies')
            if isinstance(nested, list): collect(nested, key)
    collect(raw if isinstance(raw, list) else [])
    roots = []; orphans = []
    for key in order:
        node = nodes[key]; parent = node['parent']
        if not parent: roots.append(node); continue
        if parent not in nodes:
            node['notice'] = 'Parent comment not captured'
            orphans.append(node); continue
        seen = {key}; cursor = parent; cycle = False
        while cursor in nodes:
            if cursor in seen: cycle = True; break
            seen.add(cursor); cursor = nodes[cursor]['parent']
        if cycle:
            node['notice'] = 'Reply relationship could not be resolved'
            orphans.append(node)
        else: nodes[parent]['children'].append(node)
    return {'main': roots, 'unmatched': orphans, 'total': len(nodes),
            'main_count': sum(not nodes[k]['reply'] for k in order),
            'reply_count': sum(nodes[k]['reply'] for k in order)}

def title_for(post):
    caption = re.sub(r'https?://\S+|(?<!\w)#[\w]+', '', text(post.get('text')))
    caption = re.sub(r'\s+', ' ', caption).strip()
    if caption: return caption[:120].rsplit(' ',1)[0] + '…' if len(caption)>120 else caption
    return 'Untitled post — ' + text((post.get('authorMeta') or {}).get('name') or post.get('id'))

def init_archive(root):
    db = root/'archive.sqlite'
    existed = db.exists()
    with database.connection(db) as c:
        c.execute('CREATE TABLE IF NOT EXISTS posts (' + ','.join(x + ' TEXT' for x in COLUMNS) + ', custom_title INTEGER DEFAULT 0, PRIMARY KEY(id))')
        existing = {row[1] for row in c.execute('PRAGMA table_info(posts)')}
        additions = {**{name:'INTEGER' for name in METRICS}, 'platform':"TEXT DEFAULT 'tiktok'", 'assets':"TEXT DEFAULT '[]'"}
        missing = [name for name in additions if name not in existing]
        if missing and existed:
            c.commit()
            backup = root / ('archive-before-metrics-' + str(time.time_ns()) + '.sqlite')
            with database.connection(backup) as dest: c.backup(dest)
            print('Database backup saved:', backup.name)
        for name in missing:
            c.execute('ALTER TABLE posts ADD COLUMN ' + name + ' ' + additions[name])
    with database.connection(db) as c:
        if existed and not c.execute("SELECT 1 FROM sqlite_master WHERE name='classification'").fetchone():
            with database.connection(root/("archive-before-categories-"+str(time.time_ns())+".sqlite")) as dest: c.backup(dest)
    categorize.init(db)
    screenshots.init(db)
    research.init(db)
    return db

def scan(root, db):
    removed,warnings=restricted_imports.cleanup(root,db)
    if removed: print("Removed",len(removed),"restricted-only Instagram imports. Recovery copies: Removed-restricted-imports")
    count=0
    with database.connection(db) as c:
        known_issues=dict(c.execute('SELECT post_id,reason FROM import_issues').fetchall())
        for folder in sorted(list(root.glob('tiktok_*')) + list(root.glob('instagram_*'))):
            if not folder.is_dir(): continue
            try:
                inside(root,folder)
                post=read_json(folder/'post.json',None)
                if not isinstance(post,dict) or not post:
                    if folder.name.startswith('instagram_'):
                        issue='Instagram post.json missing, empty or invalid'
                        c.execute('INSERT OR REPLACE INTO import_issues VALUES (?,?)',(folder.name,issue))
                        if known_issues.get(folder.name)==issue:continue
                    warnings.append(folder.name+': post.json missing or invalid; existing saved edits kept'); continue
                pid=folder.name
                manifest=read_json(folder/'downloads.json',{})
                files=[p for p in folder.iterdir() if p.is_file() and p.suffix.lower() not in ('.part',)]
                video=''; thumbnail=''; transcripts=[]
                for f in files:
                    inside(root,f)
                    rel=f.relative_to(root).as_posix()
                    if f.suffix.lower() in ('.mp4','.mov','.webm') and not video: video=rel
                    if f.suffix.lower() in ('.jpg','.jpeg','.png','.webp'):
                        if not thumbnail or 'cover' in f.name.lower(): thumbnail=rel
                    if f.suffix.lower() in ('.vtt','.srt') or 'subtitle' in f.name.lower() or 'transcript' in f.name.lower():
                        transcripts.append(f.read_text(encoding='utf-8', errors='replace'))
                meta=post.get('videoMeta') or {}
                author=post.get('authorMeta') or {}
                comment_data=comments_for_post(read_json(folder/'comments.json',[]), post)
                vals=[pid,folder.name,title_for(post),text(author.get('name')),text(post.get('text')),
                      text(meta.get('aiVideoSummary') or meta.get('aiVideoDescription')),
                      '\n\n'.join(transcripts),text(comment_data),text(post.get('webVideoUrl')),
                      text(post.get('createTimeISO')),thumbnail,video,'','']
                old=c.execute('SELECT title,custom_title,tags,notes FROM posts WHERE id=?',(pid,)).fetchone()
                custom=0
                if old:
                    if old[1]: vals[2]=old[0]; custom=1
                    vals[-2:]=[old[2],old[3]]
                assets = []
                for entry in read_json(folder/'assets.json', []):
                    if not isinstance(entry,dict) or entry.get('kind') not in ('image','video'): continue
                    f = inside(root, folder / entry.get('file',''))
                    if f.is_file(): assets.append({'file':f.relative_to(root).as_posix(), 'kind':entry['kind'], 'position':entry.get('position',0)})
                assets.sort(key=lambda a:a['position'])
                platform = 'instagram' if folder.name.startswith('instagram_') else 'tiktok'
                issue=''
                if post.get('error') or post.get('errorCode'):
                    issue=('Instagram' if platform=='instagram' else 'TikTok')+' scraper returned an error'
                elif platform=='instagram' and not video and not thumbnail and not text(post.get('text')).strip() and not any(t.strip() for t in transcripts):
                    issue='Instagram import has no saved media, caption or transcript'
                if issue:
                    c.execute('INSERT OR REPLACE INTO import_issues VALUES (?,?)',(pid,issue))
                    if known_issues.get(pid)!=issue:
                        warnings.append(pid+': '+issue+'; hidden from gallery')
                    continue
                c.execute('DELETE FROM import_issues WHERE post_id=?',(pid,))
                columns = COLUMNS + ['custom_title'] + list(METRICS) + ['platform','assets']
                values = vals + [custom] + [metric_value(post.get(source)) for source in METRICS.values()] + [platform,json.dumps(assets)]
                c.execute('INSERT OR REPLACE INTO posts (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in values) + ')', values)
                count+=1
            except (OSError, ValueError, TypeError) as e:
                warnings.append(folder.name+': '+str(e))
        for row in c.execute('SELECT id,folder FROM posts WHERE platform="instagram"').fetchall():
            if not (root/row[1]).is_dir():c.execute('INSERT OR REPLACE INTO import_issues VALUES (?,?)',(row[0],'Instagram post folder is missing'))
    added,extra=screenshots.scan(root,db)
    return count+added,warnings+extra

def csv_cell(v):
    v=text(v)
    return "'"+v if v.lstrip().startswith(('=','+','-','@')) or v.startswith(('\t','\r','\n')) else v

class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,root,db):
        categorize.init(db)
        screenshots.init(db)
        research.init(db)
        self.scan_lock=threading.Lock()
        self.batch=categorize.Batch(db,root)
        self.root=root; self.db=db; self.token=secrets.token_urlsafe(32)
        self.page=(Path(__file__).parent/'page.html').read_text(encoding='utf-8').replace('__TOKEN__',self.token)
        super().__init__(('127.0.0.1',0),Handler)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send(self, body, kind='application/json', code=200, extra=None):
        if not isinstance(body,bytes): body=body.encode('utf-8')
        self.send_response(code); self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(body))); self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        if extra:
            for k,v in extra.items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(body)
    def valid_host(self):
        return self.headers.get('Host') == '127.0.0.1:'+str(self.server.server_port)
    def conn(self):
        return database.connection(self.server.db)
    def do_GET(self):
        try: self.get_request()
        except (BrokenPipeError,ConnectionResetError): pass
        except (sqlite3.Error,OSError):
            self.send(json.dumps({'error':'The organizer could not read the archive. Check its folder is available and restart using the latest organizer.'}),code=503)
    def get_request(self):
        if not self.valid_host(): return self.send('{}',code=403)
        u=urlsplit(self.path); q=parse_qs(u.query)
        if u.path=='/': return self.send(self.server.page,'text/html; charset=utf-8')
        if u.path=='/comments.js': return self.send((Path(__file__).parent/'comments.js').read_text(),'text/javascript; charset=utf-8')
        if u.path=='/ai.js': return self.send((Path(__file__).parent/'ai.js').read_text(),'text/javascript; charset=utf-8')
        if u.path=='/api/ai/config': return self.send(json.dumps({'categories':categorize.CATEGORIES,'model':categorize.DEFAULT_MODEL}))
        if u.path=='/api/ai/status': return self.send(json.dumps(self.server.batch.status()))
        if u.path=='/api/file-check':
            with self.conn() as c:
                issues=[dict(r) for r in c.execute('SELECT * FROM import_issues ORDER BY post_id')]
                rows=c.execute('SELECT id,thumbnail,video FROM posts').fetchall()
            missing=[]
            for row in rows:
                for kind in ('thumbnail','video'):
                    if not row[kind]:continue
                    try: exists=inside(self.server.root,self.server.root/row[kind]).is_file()
                    except (OSError,ValueError):exists=False
                    if not exists:missing.append({'post_id':row['id'],'kind':kind,'relative_path':row[kind]})
            return self.send(json.dumps({'excluded_imports':issues,'missing_media':missing,'note':'Read-only file check. No files or database records were removed.'},indent=2),'application/json',extra={'Content-Disposition':'attachment; filename="Archive-file-check.json"'})
        if u.path=='/api/category-state':
            with self.conn() as c: rows=c.execute('SELECT id,categories FROM classification').fetchall()
            return self.send(json.dumps({r['id']:(json.loads(r['categories']) or [''])[0] for r in rows}))
        if u.path=='/api/link-options':
            search='%'+q.get('q',[''])[0][:500]+'%'
            mode=q.get('mode',['posts'])[0]
            try: offset=max(0,int(q.get('offset',['0'])[0]))
            except ValueError: offset=0
            with self.conn() as c:
                clause='platform IN ("tiktok","instagram")' if mode=='posts' else 'platform="screenshot" AND NOT EXISTS (SELECT 1 FROM screenshot_links l WHERE l.screenshot_id=posts.id)'
                rows=c.execute('SELECT id,title,creator,thumbnail,platform FROM posts WHERE ('+clause+') AND NOT EXISTS (SELECT 1 FROM import_issues i WHERE i.post_id=posts.id) AND (title LIKE ? OR creator LIKE ? OR source LIKE ?) ORDER BY title LIMIT 40 OFFSET ?',(search,search,search,offset)).fetchall()
            return self.send(json.dumps({'items':[dict(r) for r in rows]}))
        if u.path=='/api/posts':
            term=q.get('q',[''])[0][:500]; search='%'+term+'%'
            try: offset=max(0,int(q.get('offset',['0'])[0]))
            except ValueError: offset=0
            with self.conn() as c:
                where=' WHERE title LIKE ? OR creator LIKE ? OR caption LIKE ? OR summary LIKE ? OR transcript LIKE ? OR comments LIKE ? OR tags LIKE ? OR notes LIKE ? OR EXISTS (SELECT 1 FROM manual_comments m WHERE m.post_id=posts.id AND (m.text LIKE ? OR m.username LIKE ?))'
                args=[search]*10
                platform=q.get('platform',['all'])[0]
                if platform in ('tiktok','instagram','screenshot'):
                    where = ' WHERE (' + where[len(' WHERE '):] + ') AND platform=?'
                    args.append(platform)
                where=' WHERE ('+where[len(' WHERE '):]+')'
                where+=' AND NOT EXISTS (SELECT 1 FROM import_issues i WHERE i.post_id=posts.id)'
                category=q.get('category',[''])[0]
                status=q.get('status',[''])[0]
                if q.get('highlight',[''])[0]=='1': where+=' AND EXISTS (SELECT 1 FROM research_flags f WHERE f.post_id=posts.id AND f.highlighted=1)'
                if category in categorize.CATEGORIES:
                    where+=' AND EXISTS (SELECT 1 FROM classification a WHERE a.id=posts.id AND a.categories LIKE ?)'
                    args.append('%'+json.dumps(category)[1:-1]+'%')
                if category=='uncategorized': where+=' AND NOT EXISTS (SELECT 1 FROM classification a WHERE a.id=posts.id AND a.categories!="[]")'
                if status=='failed': where+=' AND EXISTS (SELECT 1 FROM classification a WHERE a.id=posts.id AND a.outcome="failed")'
                direction='ASC' if q.get('sort',['newest'])[0]=='oldest' else 'DESC'
                rows=c.execute('SELECT id,title,creator,posted,thumbnail,video,tags,platform,likes,views,total_comments,shares,saves,substr(caption,1,180) AS caption FROM posts'+where+' ORDER BY CASE WHEN posted IS NULL OR posted="" THEN 1 ELSE 0 END, posted '+direction+',id LIMIT 60 OFFSET ?',args+[offset]).fetchall()
                total=c.execute('SELECT COUNT(*) FROM posts'+where,args).fetchone()[0]
                highlighted={r[0] for r in c.execute('SELECT post_id FROM research_flags WHERE highlighted=1')}
            states=categorize.records(self.server.db,[r['id'] for r in rows])
            return self.send(json.dumps({'items':[{**dict(r),'classification':states[r['id']],'highlighted':r['id'] in highlighted} for r in rows],'total':total},ensure_ascii=False))
        if u.path=='/api/post':
            with self.conn() as c: row=c.execute('SELECT * FROM posts WHERE id=?',(q.get('id',[''])[0],)).fetchone()
            payload = dict(row) if row else {}
            if row:
                payload.update(research.details(self.server.db,row['id']))
                payload.update(screenshots.references(self.server.db,row['id']))
                payload['classification']=categorize.record(self.server.db,row['id'])
                payload['media_assets'] = json.loads(row['assets'] or '[]')
                payload['comment_threads'] = comment_threads(json.loads(row['comments'] or '[]')+research.comment_rows(self.server.db,row['id']))
            return self.send(json.dumps(payload,ensure_ascii=False),code=200 if row else 404)
        if u.path=='/export.csv':
            headers=['Post ID','Title','Creator','Tags','Notes','Posted','Caption','AI description','Source URL','Folder','Video file','Thumbnail file','Transcript','Comments JSON']
            fields=['id','title','creator','tags','notes','posted','caption','summary','source','folder','video','thumbnail','transcript','comments']
            headers += ['Likes (at scrape)', 'Views (at scrape)', 'Total comments (at scrape)', 'Shares (at scrape)', 'Saves (at scrape)']
            headers += ['Platform']
            fields += list(METRICS) + ['platform']
            stream=io.StringIO(newline=''); w=csv.writer(stream); w.writerow(headers+['Primary category','Secondary categories','Category source','Attached to post ID','Reference screenshot IDs','Research highlight','Manually added comments JSON'])
            with self.conn() as c:
                for row in c.execute('SELECT * FROM posts ORDER BY posted DESC,id'):
                    cr=categorize.record(self.server.db,row['id']); refs=screenshots.references(self.server.db,row['id'])
                    w.writerow([csv_cell(row[f]) for f in fields]+[csv_cell(cr['primary']),csv_cell(', '.join(cr['secondary'])),'Manual' if cr['manual'] else ('AI' if cr['categories'] else 'Uncategorized'),(refs['attached_to'] or {}).get('id',''),csv_cell(', '.join(r['id'] for r in refs['references'])),str(research.details(self.server.db,row['id'])['highlighted']),csv_cell(json.dumps(research.details(self.server.db,row['id'])['manual_comments'],ensure_ascii=False))])
            return self.send(('\ufeff'+stream.getvalue()).encode('utf-8'),'text/csv; charset=utf-8',extra={'Content-Disposition':'attachment; filename="Post-Archive.csv"'})
        if u.path=='/file':
            pid=q.get('id',[''])[0]; kind=q.get('kind',[''])[0]
            if kind not in ('video','thumbnail','asset'): return self.send('{}',code=400)
            field = 'assets' if kind=='asset' else kind
            with self.conn() as c: row=c.execute('SELECT '+field+' FROM posts WHERE id=?',(pid,)).fetchone()
            if not row or not row[0]: return self.send('{}',code=404)
            path = row[0]
            if kind=='asset':
                try:
                    assets = json.loads(path); index=int(q.get('index',['0'])[0])
                    if index<0: raise ValueError()
                    path=assets[index]['file']
                except (ValueError,IndexError,KeyError): return self.send('{}',code=404)
            try:
                p=inside(self.server.root,self.server.root/path)
                size=p.stat().st_size; start=0; end=size-1; status=200
                rng=self.headers.get('Range')
                if rng:
                    m=re.fullmatch(r'bytes=(\d*)-(\d*)',rng)
                    if not m or not any(m.groups()): return self.send('',code=416)
                    if m[1]: start=int(m[1]); end=min(int(m[2]),size-1) if m[2] else size-1
                    else: start=max(0,size-int(m[2]))
                    if start>end or start>=size: return self.send('',code=416,extra={'Content-Range':'bytes */'+str(size)})
                    status=206
                self.send_response(status)
                self.send_header('Content-Type',mimetypes.guess_type(p.name)[0] or 'application/octet-stream')
                self.send_header('Accept-Ranges','bytes'); self.send_header('Content-Length',str(end-start+1))
                self.send_header('X-Content-Type-Options','nosniff')
                if status==206: self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                self.end_headers()
                with p.open('rb') as f:
                    f.seek(start); remaining=end-start+1
                    while remaining:
                        b=f.read(min(1024*1024,remaining))
                        if not b: break
                        self.wfile.write(b); remaining-=len(b)
            except (FileNotFoundError,ValueError): return self.send('{}',code=404)
            except (BrokenPipeError,ConnectionResetError): pass
            return
        self.send('{}',code=404)
    def do_POST(self):
        try: self.post_request()
        except (BrokenPipeError,ConnectionResetError): pass
        except (sqlite3.Error,OSError):
            self.send(json.dumps({'error':'The organizer could not complete the save. Check the archive folder and try again; do not delete archive.sqlite.'}),code=503)
    def post_request(self):
        if not self.valid_host() or self.headers.get('X-Archive-Token')!=self.server.token: return self.send('{}',code=403)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=100000: return self.send('{}',code=400)
            body=json.loads(self.rfile.read(length))
            if not isinstance(body,dict): raise ValueError('Invalid request.')
            if self.path=='/api/highlight':
                research.highlight(self.server.db,str(body.get('id','')),body.get('highlighted'))
                return self.send(json.dumps({'saved':True}))
            if self.path=='/api/comment':
                cid=research.save_comment(self.server.db,body)
                return self.send(json.dumps({'saved':True,'id':cid}))
            if self.path=='/api/comment/delete':
                research.delete_comment(self.server.db,str(body.get('post_id','')),str(body.get('id','')))
                return self.send(json.dumps({'saved':True}))
            if self.path=='/api/scan':
                if not self.server.scan_lock.acquire(blocking=False): raise ValueError('An import is already running.')
                try: count,warnings=scan(self.server.root,self.server.db)
                finally: self.server.scan_lock.release()
                return self.send(json.dumps({'count':count,'warnings':warnings}))
            if self.path=='/api/link':
                screenshots.link(self.server.db,str(body.get('screenshot_id','')),str(body.get('post_id','')))
                return self.send(json.dumps({'saved':True}))
            if self.path=='/api/ai/models':
                return self.send(json.dumps({'models':categorize.available_models(body)}))
            if self.path=='/api/ai/start':
                self.server.batch.start(body); return self.send(json.dumps({'started':True}))
            if self.path=='/api/ai/stop':
                self.server.batch.stop.set(); return self.send(json.dumps({'stopping':True}))
            if self.path=='/api/categories':
                categorize.review(self.server.db,str(body.get('id','')),body.get('categories')); return self.send(json.dumps({'saved':True}))
            if self.path!='/api/edit': return self.send('{}',code=404)
            title=str(body.get('title','')).strip()[:300]
            if not title: return self.send('{"error":"A title is required."}',code=400)
            with self.conn() as c:
                pid=str(body.get('id',''))
                if 'tags' in body:
                    updated=c.execute('UPDATE posts SET title=?,custom_title=1,tags=?,notes=? WHERE id=?',(title,str(body['tags'])[:2000],str(body.get('notes',''))[:20000],pid)).rowcount
                else:
                    updated=c.execute('UPDATE posts SET title=?,custom_title=1,notes=? WHERE id=?',(title,str(body.get('notes',''))[:20000],pid)).rowcount
            return self.send(json.dumps({'saved':bool(updated)}),code=200 if updated else 404)
        except (ValueError,TypeError) as e: return self.send(json.dumps({'error':str(e)}),code=400)

def main():
    print('Post Archive Organizer\nBrowse your existing downloads. No Apify token needed.\n')
    location=input('Archive folder [~/Downloads/Saved-Posts]: ').strip() or '~/Downloads/Saved-Posts'
    # Finder-dragged paths may be quoted; prefer typing or pasting the plain path.
    root=Path(location.strip("\"'")).expanduser().resolve()
    if not root.is_dir(): raise ValueError('That folder does not exist.')
    import fcntl
    archive_lock=(root/'.organizer.lock').open('a')
    try: fcntl.flock(archive_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: raise ValueError('This archive is already open in another organizer. Close its Terminal process first.')
    db=init_archive(root); count,warnings=scan(root,db)
    print('Indexed',count,'post folders.')
    for warning in warnings: print('Check:',warning)
    if not count: print('Choose the parent Saved-Posts folder, not an individual tiktok folder.')
    server=Server(root,db); url='http://127.0.0.1:'+str(server.server_port)
    print('\nOpen '+url+'\nKeep this Terminal open. Press Control-C to stop.')
    webbrowser.open(url)
    try: server.serve_forever()
    except KeyboardInterrupt: print('\nClosed. Titles and notes are saved in archive.sqlite.')
    finally:
        server.batch.stop.set()
        if server.batch.thread:
            print('Finishing the current AI request before closing…')
            server.batch.thread.join(timeout=100)
        server.server_close()
        archive_lock.close()

if __name__=='__main__':
    try: main()
    except (ValueError,OSError,sqlite3.Error) as e: print('Could not open archive:',e); sys.exit(1)
