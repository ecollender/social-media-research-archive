#!/usr/bin/env python3
"""Read-only downloader for completed Clockworks TikTok Scraper runs. Python 3.9+."""
import getpass, hashlib, json, mimetypes, re, shutil, sys, time
from pathlib import Path
from urllib.parse import urlsplit, unquote, quote, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

BASE = 'https://api.apify.com/v2/'

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the API credential to another host.

class Client:
    def __init__(self, token):
        self.token = token
        self.opener = build_opener(NoRedirect)
    def open(self, path):
        url = path if path.startswith('https://') else BASE + path
        p = urlsplit(url)
        if p.scheme != 'https' or p.hostname != 'api.apify.com' or p.port not in (None, 443) or p.username:
            raise ValueError('Only Apify HTTPS storage links are downloaded.')
        for attempt in range(3):
            try:
                return self.opener.open(Request(url, headers={'Authorization': 'Bearer ' + self.token}), timeout=90)
            except HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError('Apify HTTP %s. Check token permissions and that the resource still exists.' % e.code) from None
    def json(self, path):
        with self.open(path) as r:
            return json.load(r)
    def items(self, ds):
        offset = 0
        while True:
            batch = self.json('datasets/%s/items?%s' % (quote(ds, safe=''), urlencode({'format':'json','offset':offset,'limit':1000})))
            if not batch: break
            yield from batch
            offset += len(batch)
    def keys(self, store):
        cursor = ''
        while True:
            d = self.json('key-value-stores/%s/keys?%s' % (quote(store, safe=''), urlencode({'limit':1000,'exclusiveStartKey':cursor})))['data']
            yield from d['items']
            if not d.get('isTruncated'): break
            cursor = d['nextExclusiveStartKey']

def strings(value):
    if isinstance(value, str): yield value
    elif isinstance(value, dict):
        for v in value.values(): yield from strings(v)
    elif isinstance(value, list):
        for v in value: yield from strings(v)

def record_ref(value):
    p = urlsplit(value)
    if p.scheme != 'https' or p.hostname != 'api.apify.com': return None
    m = re.fullmatch(r'/v2/key-value-stores/([^/]+)/records/(.+)', p.path)
    return (unquote(m[1]), unquote(m[2])) if m else None

def dataset_ref(value):
    p = urlsplit(value)
    if p.hostname != 'api.apify.com': return None
    m = re.match(r'/v2/datasets/([^/]+)(?:/items)?$', p.path)
    return unquote(m[1]) if m else None

def safe(value):
    return re.sub(r'[^a-zA-Z0-9_.-]', '_', str(value)).strip('.')[:110] or 'file'

def write_json(path, data):
    tmp = path.with_suffix(path.suffix + '.part')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024*1024), b''): h.update(b)
    return h.hexdigest()

def download(client, ref, folder, manifest):
    store, key = ref
    identity = store + '/' + key
    previous = manifest.get(identity)
    if previous:
        p = folder / previous['file']
        if p.is_file() and p.stat().st_size == previous['bytes'] and digest(p) == previous['sha256']:
            return previous
    with client.open('key-value-stores/%s/records/%s' % (quote(store, safe=''), quote(key, safe=''))) as response:
        kind = response.headers.get_content_type()
        ext = {'video/mp4':'.mp4','text/vtt':'.vtt','image/jpeg':'.jpg','application/json':'.json'}.get(kind, mimetypes.guess_extension(kind) or '.bin')
        name = safe(key)
        if not Path(name).suffix: name += ext
        name = hashlib.sha256(identity.encode()).hexdigest()[:10] + '-' + name
        target = folder / name
        partial = folder / (name + '.part')
        size = int(response.headers.get('Content-Length', 0))
        if size and shutil.disk_usage(folder).free < size + 2*1024**3:
            raise RuntimeError('Not enough disk space; keeping 2 GB free.')
        try:
            with partial.open('wb') as out:
                while True:
                    block = response.read(1024*1024)
                    if not block: break
                    if shutil.disk_usage(folder).free < len(block) + 2*1024**3:
                        raise RuntimeError('Stopped to keep 2 GB free.')
                    out.write(block)
            if size and partial.stat().st_size != size: raise RuntimeError('Incomplete download; rerun to retry.')
            if not partial.stat().st_size: raise RuntimeError('Empty file returned.')
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
    entry = {'file':name,'bytes':target.stat().st_size,'sha256':digest(target),'type':kind}
    manifest[identity] = entry
    write_json(folder / 'downloads.json', manifest)
    return entry

def main():
    print('Apify → Mac archive (TikTok / Instagram)\nDownloads existing results only. It never starts a scraper or deletes cloud files.\n')
    run_id = input('Completed run ID (copy from Apify run details): ').strip()
    if not re.fullmatch(r'[A-Za-z0-9]+', run_id): raise ValueError('Paste just the run ID, not a URL or the # character.')
    destination = input('Save folder [~/Downloads/Saved-Posts]: ').strip() or '~/Downloads/Saved-Posts'
    root = Path(destination).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    token = getpass.getpass('Apify API token (hidden; kept in memory only): ').strip()
    client = Client(token)
    run = client.json('actor-runs/' + run_id)['data']
    if run['status'] not in ('SUCCEEDED', 'ABORTED', 'FAILED', 'TIMED-OUT'):
        raise ValueError('This run is still active. Wait for it to finish, or stop it in Apify before recovering saved results.')
    partial_run = run['status'] != 'SUCCEEDED'
    if partial_run:
        print('Recovering saved results from a ' + run['status'] + ' run. Some posts or files may be missing.')
    default_store = run['defaultKeyValueStoreId']
    config = client.json('key-value-stores/%s/records/INPUT' % default_store)
    posts = list(client.items(run['defaultDatasetId']))
    instagram_posts = [p for p in posts if p.get('shortCode') or 'instagram.com' in str(p.get('inputUrl','')) or 'instagram.com' in str(p.get('url',''))]
    if instagram_posts:
        if len(instagram_posts) != len(posts):
            raise ValueError('Mixed or unrecognized output. Use a run containing Instagram post results only.')
        from instagram import download_posts
        download_posts(posts, root, run_id, run['status'], client=client)
        return
    stores = {default_store}
    named = config.get('videoKvStoreIdOrName')
    if named:
        # Resolve bare store names against the authenticated account.
        user = client.json('users/me')['data']['username']
        try:
            store = client.json('key-value-stores/' + quote(user + '~' + named, safe=''))['data']['id']
        except RuntimeError:
            store = client.json('key-value-stores/' + quote(named, safe=''))['data']['id']
        stores.add(store)
    print('Reading file inventory…')
    inventory = [(s, x['key']) for s in stores for x in client.keys(s)]
    reports = []
    for i, post in enumerate(posts, 1):
        post_id = str(post.get('id') or '')
        if not post_id:
            match = re.search(r'/video/(\d+)', post.get('webVideoUrl',''))
            post_id = match[1] if match else hashlib.sha256(json.dumps(post, sort_keys=True).encode()).hexdigest()[:16]
        folder = root / ('tiktok_' + safe(post_id))
        folder.mkdir(exist_ok=True)
        print('Post %d/%d: %s' % (i, len(posts), post_id))
        write_json(folder / 'post.json', post)
        meta = post.get('videoMeta') or {}
        description = {'caption':post.get('text'), 'source':post.get('webVideoUrl'), 'AI_description':meta.get('aiVideoDescription'), 'AI_summary':meta.get('aiVideoSummary')}
        (folder / 'description.txt').write_text('\n\n'.join(k + ':\n' + (v if isinstance(v,str) else json.dumps(v, ensure_ascii=False)) for k,v in description.items()), encoding='utf-8')
        manifest_path = folder / 'downloads.json'
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        refs = {r for v in strings(post) if v.startswith('https://') for r in [record_ref(v)] if r}
        refs.update((s,k) for s,k in inventory if re.search(r'(?<!\d)' + re.escape(post_id) + r'(?!\d)', k))
        errors, types = [], []
        for ref in sorted(refs):
            try: types.append(download(client, ref, folder, manifest)['type'])
            except Exception as e: errors.append(safe(ref[1]) + ': ' + type(e).__name__ + (' - ' + str(e) if isinstance(e,RuntimeError) else ''))
        comments_url = post.get('commentsDatasetURL') or post.get('commentsDatasetUrl')
        ds = dataset_ref(comments_url) if isinstance(comments_url,str) else None
        comments_status = 'No comments dataset link in output; inspect COMMENTS_DATASET_INFO in Apify.'
        if ds:
            try:
                comments = list(client.items(ds))
                write_json(folder / 'comments.json', comments)
                comments_status = str(len(comments)) + ' comments saved (dataset as supplied by scraper).'
            except Exception as e:
                errors.append('Comments: ' + type(e).__name__)
                comments_status = 'Failed; rerun to retry.'
        elif isinstance(post.get('comments'), list):
            write_json(folder / 'comments.json', post['comments'])
            comments_status = str(len(post['comments'])) + ' inline comments saved.'
        if not refs: errors.append('No stored media matched. Files may use an unfamiliar schema; inspect the store before deleting anything.')
        if config.get('shouldDownloadVideos') and not post.get('isSlideshow') and not any(t.startswith('video/') for t in types):
            errors.append('No video file verified for this post.')
        report = {'post':post_id, 'files_verified':len(types), 'comments':comments_status, 'issues':errors, 'run_status':run['status'], 'partial_run':partial_run}
        write_json(folder / 'status.json', report)
        reports.append(report)
        print('  %s files verified. %s Issues: %s' % (len(types), comments_status, len(errors)))
    write_json(root / ('run-' + run_id + '-report.json'), reports)
    print('\nSaved to: ' + str(root))
    print('Review status.json in each folder. Keep Apify originals until you have checked the files.')
    if partial_run:
        print('Partial recovery only: saved dataset entries may not include every input URL. Compare against the original input list.')

if __name__ == '__main__':
    try: main()
    except KeyboardInterrupt: print('\nStopped. Rerun to retry unfinished files.'); sys.exit(1)
    except Exception as e:
        print('\nStopped: ' + (str(e) if isinstance(e,(ValueError,RuntimeError)) else type(e).__name__ + '. Check connection, disk access, and token permissions.'))
        sys.exit(1)
