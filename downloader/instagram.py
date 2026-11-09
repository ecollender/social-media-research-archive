"""Instagram results adapter and media downloads. Does not send Apify tokens to CDNs."""
import json, hashlib, shutil, time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

def validate_url(url):
    p=urlsplit(url);host=(p.hostname or '').lower()
    if p.scheme!='https' or p.port not in (None,443) or p.username or not any(host==d or host.endswith('.'+d) for d in ('cdninstagram.com','fbcdn.net')):
        raise ValueError('Expected an Instagram/Facebook HTTPS media URL.')
    return url

class MediaRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        validate_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def assets_for(post):
    assets=[]
    children=post.get('childPosts') or [post]
    for i,child in enumerate(children,1):
        kind='video' if child.get('type')=='Video' else 'image'
        url=(child.get('downloadedVideo') or child.get('videoUrl')) if kind=='video' else child.get('displayUrl')
        assets.append({'key':'item-%03d-%s'%(i,kind),'url':url,'kind':kind,'position':i})
    if post.get('type') in ('Video','Sidecar') and post.get('displayUrl'):
        assets.append({'key':'cover','url':post['displayUrl'],'kind':'thumbnail','position':0})
    return assets

def normalize(post):
    return {'platform':'instagram','id':str(post.get('shortCode') or post.get('id') or ''),
            'text':post.get('caption') or post.get('alt') or '',
            'authorMeta':{'name':post.get('ownerUsername') or post.get('ownerFullName') or ''},
            'webVideoUrl':post.get('url') or post.get('inputUrl') or '',
            'createTimeISO':post.get('timestamp'),'videoMeta':{},
            'diggCount':post.get('likesCount'),
            'playCount':post.get('videoPlayCount') if post.get('videoPlayCount') is not None else post.get('videoViewCount'),
            'commentCount':post.get('commentsCount'),'shareCount':None,'collectCount':None,
            'isSlideshow':post.get('type')=='Sidecar','originalType':post.get('type')}

def fetch_asset(asset,folder,manifest,opener=None,client=None):
    from download import digest,write_json
    key=asset['key'];previous=manifest.get(key)
    if previous:
        p=folder/previous['file']
        if p.is_file() and p.stat().st_size==previous['bytes'] and digest(p)==previous['sha256']:return previous
    if not asset.get('url'):raise ValueError('No downloadable URL in scraper output.')
    from download import record_ref
    hosted = record_ref(asset['url'])
    if hosted and client is None: raise ValueError('Apify authentication is needed for this hosted video.')
    if not hosted: validate_url(asset['url'])
    opener=opener or build_opener(MediaRedirect())
    for attempt in range(3):
        try:
            response=client.open(asset['url']) if hosted else opener.open(Request(asset['url'],headers={'User-Agent':'Mozilla/5.0'}),timeout=90)
            break
        except HTTPError as e:
            if e.code in (429,500,502,503,504) and attempt<2:time.sleep(2**attempt);continue
            raise RuntimeError('Media HTTP %s; the link may have expired or access may be blocked.'%e.code) from None
    with response:
        ct=response.headers.get_content_type()
        allowed={'video/mp4':'.mp4','image/jpeg':'.jpg','image/png':'.png','image/webp':'.webp'}
        if ct not in allowed:raise ValueError('Unexpected media format: '+ct)
        if asset['kind']=='video' and not ct.startswith('video/'):raise ValueError('Expected a video file.')
        if asset['kind']!='video' and not ct.startswith('image/'):raise ValueError('Expected an image file.')
        target=folder/(key+allowed[ct]);partial=folder/(target.name+'.part')
        size=int(response.headers.get('Content-Length',0))
        if size and shutil.disk_usage(folder).free<size+2*1024**3:raise RuntimeError('Not enough disk space; keeping 2 GB free.')
        try:
            with partial.open('wb') as out:
                while True:
                    block=response.read(1024*1024)
                    if not block:break
                    if shutil.disk_usage(folder).free<len(block)+2*1024**3:raise RuntimeError('Stopped to keep 2 GB free.')
                    out.write(block)
            if not partial.stat().st_size or (size and partial.stat().st_size!=size):raise RuntimeError('Incomplete media download; rerun to retry.')
            partial.replace(target)
        finally:partial.unlink(missing_ok=True)
    entry={'file':target.name,'kind':asset['kind'],'position':asset['position'],'bytes':target.stat().st_size,'sha256':digest(target),'type':ct}
    manifest[key]=entry;write_json(folder/'instagram-downloads.json',manifest)
    return entry

def read_existing(path,default):
    if not path.exists(): return default
    return json.loads(path.read_text(encoding='utf-8'))

def merge_comments(old,new):
    out=[]; positions={}
    for row in old + new:
        if not isinstance(row,dict): continue
        key=str(row.get('id') or row.get('cid') or json.dumps(row,sort_keys=True))
        if key not in positions:
            positions[key]=len(out);out.append(dict(row))
        else:
            previous=out[positions[key]]
            merged={**previous, **{k:v for k,v in row.items() if v is not None}}
            if isinstance(previous.get('replies'),list) or isinstance(row.get('replies'),list):
                merged['replies']=merge_comments(previous.get('replies') or [],row.get('replies') or [])
            out[positions[key]]=merged
    return out

def download_posts(posts,root,run_id,status,client=None):
    from download import write_json,safe
    reports=[]
    for n,post in enumerate(posts,1):
        pid=str(post.get('shortCode') or post.get('id') or hashlib.sha256(json.dumps(post,sort_keys=True).encode()).hexdigest()[:16])
        folder=root/('instagram_'+safe(pid));folder.mkdir(exist_ok=True)
        errors=[];assets=[]
        print('Instagram post %d/%d: %s'%(n,len(posts),pid))
        write_json(folder/('instagram-source-'+safe(run_id)+'.json'),post)
        old_original=read_existing(folder/'instagram-original.json',{})
        merged_original={**old_original, **{k:v for k,v in post.items() if v not in (None, '', [])}}
        write_json(folder/'instagram-original.json',merged_original)
        if post.get('error') or post.get('errorCode'):
            errors.append('Scraper returned: '+str(post.get('error') or post.get('errorCode')))
        else:
            old_normal=read_existing(folder/'post.json',{})
            new_normal=normalize(merged_original)
            write_json(folder/'post.json',{**old_normal,**{k:v for k,v in new_normal.items() if v not in (None,'',{},[])}})
            transcript=post.get('transcript')
            if isinstance(transcript,str) and transcript.strip():
                temporary=folder/'transcript.txt.part'
                temporary.write_text(transcript,encoding='utf-8')
                temporary.replace(folder/'transcript.txt')
            comments=merge_comments(read_existing(folder/'comments.json',[]),post.get('latestComments') or [])
            write_json(folder/'comments.json',comments)
            (folder/'description.txt').write_text((post.get('caption') or post.get('alt') or '')+'\n\nSource: '+(post.get('url') or post.get('inputUrl') or ''),encoding='utf-8')
            mp=folder/'instagram-downloads.json';manifest=json.loads(mp.read_text()) if mp.exists() else {}
            for asset in assets_for(post):
                try:assets.append(fetch_asset(asset,folder,manifest,client=client))
                except Exception as e:errors.append(asset['key']+': '+(str(e) if isinstance(e,(ValueError,RuntimeError)) else type(e).__name__))
            # Preserve previously downloaded items if this scrape omits one or a transfer fails.
            previous_assets=read_existing(folder/'assets.json',[])
            known={(a['kind'],a['position']):a for a in previous_assets}
            known.update({(a['kind'],a['position']):a for a in assets})
            assets=sorted(known.values(),key=lambda a:(a['position'],a['kind']))
            write_json(folder/'assets.json',assets)
        report={'post':pid,'platform':'instagram','files_verified':len(assets),'issues':errors,'run_status':status,'partial_run':status!='SUCCEEDED','comments_note':'Only comments included in the post output are saved. This is not the entire discussion.'}
        write_json(folder/'status.json',report);reports.append(report)
        print('  %d files verified; %d issues.'%(len(assets),len(errors)))
    write_json(root/('run-'+run_id+'-report.json'),reports)
    print('Saved to:',root,'\nReview status.json for missing media. Keep your original data until checked.')
