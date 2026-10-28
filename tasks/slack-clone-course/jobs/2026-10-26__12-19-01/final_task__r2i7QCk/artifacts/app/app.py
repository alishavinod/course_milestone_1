import os, re, json, time, uuid, hashlib, secrets, sqlite3, asyncio
from datetime import datetime, timezone
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

DB='/app/data/app.db'; os.makedirs('/app/data',exist_ok=True); _CONNS={}
NODE=int(os.getenv('NODE_ID','0'))
app=FastAPI()

def now(): return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def get_db():
    c=sqlite3.connect(DB,timeout=0.5,check_same_thread=False)
    c.row_factory=sqlite3.Row
    return c
def init():
    c=get_db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS cursors(token TEXT PRIMARY KEY,channel_id INTEGER,kind TEXT,position INTEGER,created_at TEXT);
    CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
    CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,created_by INTEGER,expires_at REAL,max_uses INTEGER,uses INTEGER DEFAULT 0,invited_user_id INTEGER,revoked INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,users TEXT,UNIQUE(workspace_id,handle));
    '''); c.commit(); c.commit()
init()

def err(status,msg): return JSONResponse({'error':msg},status_code=status)
def user_json(u):
    return {k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def hash_password(p): return hashlib.sha256(('huddle:'+p).encode()).hexdigest()
def create_user(username,password,display_name=None):
    c=get_db()
    try:
        cur=c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(username,hash_password(password),display_name or username)); c.commit()
    except sqlite3.IntegrityError: c.commit(); raise ValueError('conflict')
    u=c.execute('SELECT * FROM users WHERE id=?',(cur.lastrowid,)).fetchone(); c.commit(); return u
def issue_token(uid):
    t=secrets.token_urlsafe(32); c=get_db(); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit(); c.commit(); return t
async def require_user(req):
    h=req.headers.get('authorization','')
    if not h.startswith('Bearer '): return None
    c=get_db(); u=c.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(h[7:],)).fetchone(); c.commit(); return u
def get_user_token(t):
    c=get_db(); u=c.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(t,)).fetchone(); c.commit(); return u
def channel_json(x): return {'id':x['id'],'workspace_id':x['workspace_id'],'name':x['name'],'is_private':bool(x['is_private']),'is_dm':bool(x['is_dm']),'topic':x['topic'],'is_archived':bool(x['is_archived'])}
def workspace_json(x): return {'id':x['id'],'slug':x['slug'],'name':x['name'],'owner_id':x['owner_id'],'join_mode':x['join_mode']}
def role(c,w,u): 
    x=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w,u)).fetchone()
    return x['role'] if x else None
def emit(c,ch,kind,payload):
    n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(ch,)).fetchone()['n']
    c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(ch,n,kind,json.dumps(payload),now())); return n
def msg_json(c,m):
    if not m:return None
    rs=[]
    for r in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(m['id'],)):
        rs.append({'emoji':r['emoji'],'count':r['n'],'user_ids':[int(x) for x in r['ids'].split(',')]})
    replies=c.execute('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(m['id'],)).fetchone()['n']
    u=c.execute('SELECT * FROM users WHERE id=?',(m['author_id'],)).fetchone()
    return {'id':m['id'],'channel_id':m['channel_id'],'author_id':m['author_id'],'author':user_json(u),'body':m['body'],'parent_id':m['parent_id'],'reply_count':replies,'created_at':m['created_at'],'edited_at':m['edited_at'],'files':[],'reactions':rs,'mentions':mentions(c,m['body'],m['channel_id'],m['author_id'])}
def mentions(c,body,ch,author):
    out=[]
    for name in re.findall(r'(?<![\w@])@([A-Za-z0-9_]+)',body):
        ids=[]
        if name=='channel': ids=[x['user_id'] for x in c.execute('SELECT user_id FROM channel_members WHERE channel_id=?',(ch,))]
        elif name!='here':
            x=c.execute('''SELECT u.id FROM users u JOIN members wm ON wm.user_id=u.id
                           JOIN channels ch ON ch.workspace_id=wm.workspace_id
                           WHERE u.username=? AND ch.id=?''',(name,ch)).fetchone()
            ids=[x['id']] if x else []
        for i in ids:
            if i!=author and i not in out: out.append(i)
    return out
def visible(c,ch,u):
    x=c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone()
    if not x:return None
    if x['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch,u)).fetchone(): return None
    return x
def cursor_make(ch,kind,pos):
    t=secrets.token_urlsafe(18); c=get_db(); c.execute('INSERT INTO cursors VALUES(?,?,?,?,?)',(t,ch,kind,pos,time.time())); c.commit(); c.commit(); return t
def cursor_get(t,ch,kind):
    c=get_db(); x=c.execute('SELECT position FROM cursors WHERE token=? AND channel_id=? AND kind=?',(t,ch,kind)).fetchone(); c.commit(); return x['position'] if x else None
def paging(req,ch,kind,total):
    try: lim=int(req.query_params.get('limit','50'))
    except: return None,None,'bad'
    if lim<1 or lim>200:return None,None,'bad'
    tok=req.query_params.get('cursor'); pos=cursor_get(tok,ch,kind) if tok else 0
    if tok and pos is None:return None,None,'bad'
    return lim,pos,None

@app.get('/api/health')
def health(): return {'status':'ok','node_id':NODE}
@app.post('/api/auth/register')
async def register(req:Request):
    try:d=await req.json(); un=d.get('username',''); pw=d.get('password','')
    except:return err(400,'bad input')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return err(400,'bad input')
    try:u=create_user(un,pw,d.get('display_name'))
    except ValueError:return err(409,'exists')
    return JSONResponse({'user':user_json(u),'token':issue_token(u['id'])},201)
@app.post('/api/auth/login')
async def login(req:Request):
    d=await req.json(); c=get_db(); u=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),hash_password(d.get('password','')))).fetchone(); c.commit()
    if not u:return err(401,'invalid')
    return {'user':user_json(u),'token':issue_token(u['id'])}
@app.get('/api/auth/me')
async def me(req:Request):
    u=await require_user(req); return {'user':user_json(u)} if u else err(401,'unauthorized')
@app.get('/api/users/{uid}')
async def getusr(uid:int,req:Request):
    c=get_db();u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close();return {'user':user_json(u)} if u else err(404,'not found')
@app.patch('/api/users/me')
async def patchme(req:Request):
    u=await require_user(req)
    if not u:return err(401,'unauthorized')
    d=await req.json(); allowed=['display_name','timezone','avatar_url','status_text','status_emoji']; c=get_db()
    for k in allowed:
        if k in d:
            if len(str(d[k]))>500:return err(400,'too long')
            c.execute(f'UPDATE users SET {k}=? WHERE id=?',(d[k],u['id']))
    c.commit(); x=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone();c.commit();return {'user':user_json(x)}

@app.post('/api/workspaces')
async def createws(req:Request):
    u=await require_user(req)
    if not u:return err(401,'unauthorized')
    d=await req.json(); slug=d.get('slug','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):return err(400,'bad slug')
    c=get_db()
    try:
        x=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,d.get('name',''),u['id'])); wid=x.lastrowid
        c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner')); cc=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=cc.lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit()
    except sqlite3.IntegrityError:
        c.rollback(); c.commit(); return err(409,'exists')
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.commit();return JSONResponse({'workspace':workspace_json(w),'general_channel':channel_json(ch)},201)
@app.get('/api/workspaces')
async def listws(req:Request):
    u=await require_user(req)
    if not u:return err(401,'unauthorized')
    c=get_db(); a=[workspace_json(x) for x in c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],))];c.close();return {'workspaces':a}
@app.get('/api/workspaces/{slug}')
async def detail(slug:str,req:Request):
    u=await require_user(req)
    if not u:return err(401,'unauthorized')
    c=get_db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:return err(404,'not found')
    inc=req.query_params.get('include_archived') in ('1','true')
    chans=[channel_json(x) for x in c.execute('SELECT ch.* FROM channels ch WHERE ch.workspace_id=? AND (? OR ch.is_archived=0) AND (ch.is_private=0 OR EXISTS (SELECT 1 FROM channel_members cm WHERE cm.channel_id=ch.id AND cm.user_id=?))',(w['id'],inc,u['id']))]
    states=[read_state_for(c,u['id'],x['id']) for x in c.execute('SELECT ch.id FROM channels ch WHERE ch.workspace_id=? AND (? OR ch.is_archived=0) AND (ch.is_private=0 OR EXISTS (SELECT 1 FROM channel_members cm WHERE cm.channel_id=ch.id AND cm.user_id=?))',(w['id'],inc,u['id']))]
    c.close();return {'workspace':workspace_json(w),'channels':chans,'read_state':states}
@app.get('/api/workspaces/{slug}/members')
async def members(slug:str,req:Request):
    u=await require_user(req)
    if not u:return err(401,'unauthorized')
    c=get_db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:return err(404,'not found')
    a=[dict(x) for x in c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],))];c.close();return {'members':a}
@app.patch('/api/workspaces/{slug}')
async def patchws(slug:str,req:Request):
    u=await require_user(req); c=get_db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not u:return err(401,'unauthorized')
    if not w:return err(404,'not found')
    r=role(c,w['id'],u['id'])
    if r not in ('owner','admin'):return err(403,'forbidden')
    d=await req.json()
    if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):return err(400,'bad join mode')
    for k in ('name','join_mode'):
        if k in d:c.execute(f'UPDATE workspaces SET {k}=? WHERE id=?',(d[k],w['id']))
    c.commit();w=c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone();c.commit();return {'workspace':workspace_json(w)}
@app.patch('/api/workspaces/{slug}/members/{uid}')
async def patchmember(slug:str,uid:int,req:Request):
    u=await require_user(req); c=get_db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not u:return err(401,'unauthorized')
    if not w:return err(404,'not found')
    r=role(c,w['id'],u['id']); d=await req.json(); nr=d.get('role')
    if nr=='owner':return err(400,'use transfer')
    if r not in ('owner','admin') or uid==w['owner_id'] or (r=='admin' and role(c,w['id'],uid)=='admin'):return err(403,'forbidden')
    if nr not in ('admin','member','guest'):return err(400,'bad role')
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(nr,w['id'],uid));c.commit();c.close();return {'member':{'user_id':uid,'role':nr}}
@app.post('/api/workspaces/{slug}/transfer_ownership')
async def transfer(slug:str,req:Request):
    u=await require_user(req);c=get_db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not u:return err(401,'unauthorized')
    if not w:return err(404,'not found')
    if u['id']!=w['owner_id']:return err(403,'forbidden')
    d=await req.json(); nid=d.get('new_owner_id')
    if role(c,w['id'],nid)!='admin':return err(400,'target must admin')
    c.execute('UPDATE members SET role="admin" WHERE workspace_id=? AND user_id=?',(w['id'],u['id']));c.execute('UPDATE members SET role="owner" WHERE workspace_id=? AND user_id=?',(w['id'],nid));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(nid,w['id']));c.commit();w=c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone();c.commit();return {'workspace':workspace_json(w)}

@app.post('/api/workspaces/{slug}/channels')
async def createch(slug:str,req:Request):
    u=await require_user(req);c=get_db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not u:return err(401,'unauthorized')
    if not w:return err(404,'not found')
    r=role(c,w['id'],u['id']);d=await req.json();name=d.get('name','')
    if r=='guest' or not r:return err(403,'forbidden')
    if not re.fullmatch(r'[a-z0-9-]{1,32}',name):return err(400,'bad name')
    private=bool(d.get('is_private',False))
    topic=d.get('topic','')
    if not isinstance(topic,str) or len(topic)>250:return err(400,'topic')
    if private and r not in ('owner','admin'):return err(403,'forbidden')
    try:x=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(private),topic));cid=x.lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit()
    except sqlite3.IntegrityError:
        c.rollback(); c.commit(); return err(409,'exists')
    z=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.commit();return JSONResponse({'channel':channel_json(z)},201)
@app.post('/api/channels/{cid}/join')
async def join(cid:int,req:Request):
    u=await require_user(req);c=get_db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not u:return err(401,'unauthorized')
    if not ch:return err(404,'not found')
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone();r=role(c,w['id'],u['id'])
    if ch['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone():return err(403,'private')
    if r=='guest':return err(403,'guest')
    if not r and w['join_mode']=='invite_only':return err(403,'invite')
    if not r:c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
    was=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone()
    c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,u['id']))
    if not was: emit(c,cid,'member.joined',{'user_id':u['id']})
    c.commit();c.close();return {'channel':channel_json(ch)}
@app.post('/api/channels/{cid}/leave')
async def leave(cid:int,req:Request):
    u=await require_user(req);c=get_db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not u:return err(401,'unauthorized')
    if not ch:return err(404,'not found')
    was=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone()
    c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id']))
    if was: emit(c,cid,'member.left',{'user_id':u['id']})
    c.commit();c.close();return {'channel':channel_json(ch)}
@app.get('/api/channels/{cid}/members')
async def chmembers(cid:int,req:Request):
    u=await require_user(req);c=get_db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not u:return err(401,'unauthorized')
    if not ch:return err(404,'not found')
    a=[user_json(x) for x in c.execute('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(cid,))];c.close();return {'members':a}
@app.patch('/api/channels/{cid}')
async def patchch(cid:int,req:Request):
    u=await require_user(req);c=get_db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not u:return err(401,'unauthorized')
    if not ch:return err(404,'not found')
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
    if u['id']!=w['owner_id'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone():return err(403,'forbidden')
    d=await req.json()
    if len(d.get('topic',''))>250:return err(400,'topic')
    c.execute('UPDATE channels SET topic=? WHERE id=?',(d.get('topic',''),cid)); x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); emit(c,cid,'channel.updated',{'channel':channel_json(x)}); c.commit(); c.commit(); return {'channel':channel_json(x)}
async def archive(cid,req,flag):
    u=await require_user(req);c=get_db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not u:return err(401,'unauthorized')
    if not ch:return err(404,'not found')
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
    if u['id']!=w['owner_id']:return err(403,'forbidden')
    c.execute('UPDATE channels SET is_archived=? WHERE id=?',(flag,cid));x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); emit(c,cid,'channel.archived' if flag else 'channel.unarchived',{'channel':channel_json(x)});c.commit();c.commit();return {'channel':channel_json(x)}
@app.post('/api/channels/{cid}/archive')
async def arc(cid:int,req:Request):return await archive(cid,req,1)
@app.post('/api/channels/{cid}/unarchive')
async def unarc(cid:int,req:Request):return await archive(cid,req,0)

@app.post('/api/channels/{cid}/messages')
async def postmsg(cid:int,req:Request):
    u=await require_user(req);c=get_db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not u:return err(401,'unauthorized')
    if not ch:return err(404,'not found')
    if not visible(c,cid,u['id']):return err(403,'forbidden')
    if ch['is_archived']:return err(423,'archived')
    d=await req.json();body=d.get('body','')
    if not body:return err(400,'empty')
    if body.startswith('/') and not body.startswith('//') and not body.startswith('/me ') and not body.startswith('/shrug'):return err(400,'unknown command')
    if body.startswith('/shrug'):body=body[6:].lstrip()+' ¯\\_(ツ)_/¯' if body[6:].strip() else '¯\\_(ツ)_/¯'
    t=now();x=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],body,d.get('parent_id'),t));m=c.execute('SELECT * FROM messages WHERE id=?',(x.lastrowid,)).fetchone();mj=msg_json(c,m);kind='message.reply' if m['parent_id'] else 'message.created';eid=emit(c,cid,kind,{'message':mj});c.commit();c.commit();return JSONResponse({'message':mj},201)
@app.get('/api/channels/{cid}/messages')
async def getmsgs(cid:int,req:Request):
    u=await require_user(req);c=get_db()
    if not u:return err(401,'unauthorized')
    if not c.execute('SELECT 1 FROM channels WHERE id=?',(cid,)).fetchone():return err(404,'not found')
    if not visible(c,cid,u['id']):return err(403,'forbidden')
    lim,pos,bad=paging(req,cid,'messages',0)
    tok=req.query_params.get('cursor')
    if bad:return err(400,'paging')
    if tok:
        a=list(c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 AND id<? ORDER BY id DESC LIMIT ?',(cid,pos,lim)))
    else:
        a=list(c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT ?',(cid,lim)))
    out=[msg_json(c,x) for x in a]
    nxt=cursor_make(cid,'messages',a[-1]['id']) if len(a)==lim and len(a)==lim and c.execute('SELECT 1 FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 AND id<?',(cid,a[-1]['id'])).fetchone() else None
    c.close();return {'messages':out,'next_cursor':nxt}
@app.get('/api/messages/{mid}/replies')
async def replies(mid:int,req:Request):
    u=await require_user(req);c=get_db();p=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not u:return err(401,'unauthorized')
    if not p:return err(404,'not found')
    lim,pos,bad=paging(req,p['channel_id'],'replies',0)
    tok=req.query_params.get('cursor')
    if bad:return err(400,'paging')
    if tok:
        a=list(c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 AND id>? ORDER BY id LIMIT ?',(mid,pos,lim)))
    else:
        a=list(c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id LIMIT ?',(mid,lim)))
    out=[msg_json(c,x) for x in a]
    nxt=cursor_make(p['channel_id'],'replies',a[-1]['id']) if len(a)==lim and c.execute('SELECT 1 FROM messages WHERE parent_id=? AND deleted=0 AND id>?',(mid,a[-1]['id'])).fetchone() else None
    c.close(); return {'replies':out,'next_cursor':nxt}
@app.patch('/api/messages/{mid}')
async def edit(mid:int,req:Request):
    u=await require_user(req);c=get_db();m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not u:return err(401,'unauthorized')
    if not m:return err(404,'not found')
    if m['deleted']:return err(409,'deleted')
    if m['author_id']!=u['id']:return err(403,'forbidden')
    d=await req.json();c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(d.get('body',''),now(),mid));c.commit();m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();x=msg_json(c,m);emit(c,m['channel_id'],'message.updated',x);c.commit();c.commit();return {'message':x}
@app.delete('/api/messages/{mid}')
async def delete(mid:int,req:Request):
    u=await require_user(req);c=get_db();m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not u:return err(401,'unauthorized')
    if not m:return err(404,'not found')
    ch=c.execute('SELECT * FROM channels WHERE id=?',(m['channel_id'],)).fetchone();w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
    if m['author_id']!=u['id'] and w['owner_id']!=u['id']:return err(403,'forbidden')
    c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,));emit(c,m['channel_id'],'message.deleted',{'id':mid});c.commit();c.close();return {'deleted':True}
@app.post('/api/messages/{mid}/reactions')
async def react(mid:int,req:Request):
    u=await require_user(req);c=get_db();m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not u:return err(401,'unauthorized')
    if not m:return err(404,'not found')
    e=(await req.json()).get('emoji','');c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],e));c.commit();rs=[] 
    for r in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(mid,)):rs.append({'emoji':r['emoji'],'count':r['n'],'user_ids':[int(x) for x in r['ids'].split(',')]})
    c.close();return {'reactions':rs}
@app.delete('/api/messages/{mid}/reactions/{emoji}')
async def unreact(mid:int,emoji:str,req:Request):
    u=await require_user(req);c=get_db()
    if not u:return err(401,'unauthorized')
    c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji));c.commit();rs=[]
    for r in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(mid,)):rs.append({'emoji':r['emoji'],'count':r['n'],'user_ids':[int(x) for x in r['ids'].split(',')]})
    c.close();return {'reactions':rs}

def invitation_json(x):
    return {'code':x['code'],'workspace_id':x['workspace_id'],'created_by':x['created_by'],
            'expires_at':x['expires_at'],'max_uses':x['max_uses'],'uses':x['uses'],
            'invited_user_id':x['invited_user_id']}

def invitation_active(x):
    return not x['revoked'] and x['uses'] < x['max_uses'] and (x['expires_at'] is None or x['expires_at'] > time.time())

@app.post('/api/workspaces/{slug}/invitations')
async def create_invitation(slug:str,req:Request):
    u=await require_user(req)
    if not u:return err(401,'unauthorized')
    c=get_db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:return c.commit() or err(404,'not found')
    if role(c,w['id'],u['id']) not in ('owner','admin'):
        c.commit(); return err(403,'forbidden')
    d=await req.json()
    target=d.get('invited_user_id')
    if target is None and d.get('invited_username') is not None:
        q=c.execute('SELECT id FROM users WHERE username=?',(d['invited_username'],)).fetchone()
        if not q:c.commit(); return err(400,'unknown user')
        target=q['id']
    try: max_uses=int(d.get('max_uses',1))
    except: c.commit(); return err(400,'bad max_uses')
    if max_uses<1:c.commit(); return err(400,'bad max_uses')
    try: expires=float(d['expires_in_seconds'])+time.time() if d.get('expires_in_seconds') is not None else None
    except: c.commit(); return err(400,'bad expiry')
    code=secrets.token_urlsafe(18)
    c.execute('INSERT INTO invitations(code,workspace_id,created_by,expires_at,max_uses,invited_user_id) VALUES(?,?,?,?,?,?)',
              (code,w['id'],u['id'],expires,max_uses,target)); c.commit()
    x=c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone();c.commit()
    return JSONResponse({'invitation':invitation_json(x)},201)

@app.post('/api/invitations/{code}/accept')
async def accept_invitation(code:str,req:Request):
    u=await require_user(req)
    if not u:return err(401,'unauthorized')
    c=get_db(); x=c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone()
    if not x:return c.commit() or err(404,'not found')
    if not invitation_active(x):c.commit();return err(404,'not found')
    if x['invited_user_id'] is not None and x['invited_user_id']!=u['id']:
        c.commit();return err(403,'targeted')
    # targeted invitations can only be consumed once, regardless of max_uses
    if x['invited_user_id'] is not None and x['uses']>0:
        c.commit();return err(404,'used')
    cur=c.execute('UPDATE invitations SET uses=uses+1 WHERE code=? AND revoked=0 AND uses<max_uses AND (expires_at IS NULL OR expires_at>?)',
                  (code,time.time()))
    if cur.rowcount!=1:c.rollback();c.commit();return err(404,'not found')
    c.execute('INSERT OR IGNORE INTO members(workspace_id,user_id,role) VALUES(?,?,?)',(x['workspace_id'],u['id'],'member'))
    c.commit(); w=c.execute('SELECT * FROM workspaces WHERE id=?',(x['workspace_id'],)).fetchone();c.commit()
    return {'workspace':workspace_json(w)}

@app.get('/api/workspaces/{slug}/invitations')
async def list_invitation_route(slug:str,req:Request):
    u=await require_user(req)
    if not u:return err(401,'unauthorized')
    c=get_db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:return c.commit() or err(404,'not found')
    if role(c,w['id'],u['id']) not in ('owner','admin'):
        c.commit();return err(403,'forbidden')
    xs=c.execute('SELECT * FROM invitations WHERE workspace_id=?',(w['id'],)).fetchall()
    out=[invitation_json(x) for x in xs if invitation_active(x)]
    c.commit();return {'invitations':out}

@app.delete('/api/workspaces/{slug}/invitations/{code}')
async def revoke_invitation_route(slug:str,code:str,req:Request):
    u=await require_user(req)
    if not u:return err(401,'unauthorized')
    c=get_db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:return c.commit() or err(404,'not found')
    if role(c,w['id'],u['id']) not in ('owner','admin'):
        c.commit();return err(403,'forbidden')
    x=c.execute('SELECT * FROM invitations WHERE code=? AND workspace_id=?',(code,w['id'])).fetchone()
    if not x:return c.commit() or err(404,'not found')
    c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(code,));c.commit();c.close();return {'deleted':True}

@app.get('/api/search')
async def search(req:Request):
    u=await require_user(req)
    if not u:return err(401,'unauthorized')
    q=req.query_params.get('q','')
    try: lim=int(req.query_params.get('limit','50'))
    except: return err(400,'paging')
    if lim<1 or lim>200:return err(400,'paging')
    tok=req.query_params.get('cursor'); pos=0
    if tok:
        pos=cursor_get(tok,0,'search')
        if pos is None:return err(400,'paging')
    c=get_db(); wid=req.query_params.get('workspace_id')
    rows=[]
    for m in c.execute('SELECT m.*,ch.workspace_id,ch.is_private FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.deleted=0 AND m.body LIKE ? ORDER BY m.id DESC',('%'+q+'%',)):
        if wid and str(m['workspace_id'])!=str(wid):continue
        if m['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(m['channel_id'],u['id'])).fetchone():continue
        if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(m['channel_id'],u['id'])).fetchone():continue
        rows.append(msg_json(c,m))
    out=rows[pos:pos+lim]; nxt=cursor_make(0,'search',pos+len(out)) if len(rows)>pos+len(out) else None
    c.close();return {'results':out,'next_cursor':nxt}

@app.post('/api/messages/{mid}/pin')
async def pin(mid:int,req:Request):
    u=await require_user(req)
    if not u:return err(401,'unauthorized')
    c=get_db();m=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
    if not m:return c.commit() or err(404,'not found')
    if not visible(c,m['channel_id'],u['id']):c.commit();return err(403,'forbidden')
    x=c.execute('SELECT * FROM pins WHERE message_id=?',(mid,)).fetchone()
    if not x:
        c.execute('INSERT INTO pins VALUES(?,?,?)',(mid,u['id'],now()));c.commit();x=c.execute('SELECT * FROM pins WHERE message_id=?',(mid,)).fetchone()
    out={'message_id':x['message_id'],'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']};c.close();return {'pin':out}

@app.delete('/api/messages/{mid}/pin')
async def unpin(mid:int,req:Request):
    u=await require_user(req)
    if not u:return err(401,'unauthorized')
    c=get_db();m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not m:return c.commit() or err(404,'not found')
    if not visible(c,m['channel_id'],u['id']):c.commit();return err(403,'forbidden')
    c.execute('DELETE FROM pins WHERE message_id=?',(mid,));c.commit();c.close();return {'unpinned':True}

@app.get('/api/channels/{cid}/pins')
async def pins(cid:int,req:Request):
    u=await require_user(req)
    if not u:return err(401,'unauthorized')
    c=get_db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:return c.commit() or err(404,'not found')
    if not visible(c,cid,u['id']):c.commit();return err(403,'forbidden')
    lim,pos,bad=paging(req,cid,'pins',0)
    if bad:c.commit();return err(400,'paging')
    rows=list(c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? AND m.deleted=0 ORDER BY p.pinned_at DESC LIMIT ? OFFSET ?',(cid,lim,pos)))
    out=[{'message':msg_json(c,r),'pinned_by':r['pinned_by'],'pinned_at':r['pinned_at']} for r in rows]
    nxt=cursor_make(cid,'pins',pos+len(rows)) if len(rows)==lim and c.execute('SELECT COUNT(*) n FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? AND m.deleted=0',(cid,)).fetchone()['n']>pos+len(rows) else None
    c.close();return {'pins':out,'next_cursor':nxt}


@app.websocket('/api/ws')
async def ws_endpoint(ws:WebSocket):
    token=ws.query_params.get('token','')
    u=get_user_token(token)
    if not u:
        await ws.close(code=1008); return
    await ws.accept()
    subs={}
    try:
        while True:
            try:
                raw=await asyncio.wait_for(ws.receive_text(),timeout=0.1)
                data=json.loads(raw)
                typ=data.get('type'); cid=int(data.get('channel_id',0))
                c=get_db()
                if typ=='subscribe':
                    head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
                    subs[cid]=head
                    await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
                elif typ=='resume':
                    since=int(data.get('since_event_id',0))
                    rows=c.execute('SELECT event_id,kind,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall()
                    for r in rows:
                        f=json.loads(r['payload']); f.update({'type':r['kind'],'event_id':r['event_id'],'channel_id':cid})
                        await ws.send_json(f)
                    head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
                    subs[cid]=head
                    await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
                c.commit()
            except asyncio.TimeoutError:
                pass
            c=get_db()
            for cid,last in list(subs.items()):
                rows=c.execute('SELECT event_id,kind,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall()
                for r in rows:
                    f=json.loads(r['payload']); f.update({'type':r['kind'],'event_id':r['event_id'],'channel_id':cid})
                    await ws.send_json(f); subs[cid]=r['event_id']
            c.commit()
    except (WebSocketDisconnect,RuntimeError):
        return

def read_state_for(c, uid, cid):
    row=c.execute('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(uid,cid)).fetchone()
    last=int(row['last_read_event_id']) if row else 0
    rows=c.execute('SELECT event_id,kind,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall()
    unread=mentions_count=0
    for r in rows:
        if r['kind'] != 'message.created':
            continue
        try:
            payload=json.loads(r['payload'])
        except Exception:
            continue
        if r['payload'] and payload.get('id') is not None:
            # legacy payloads are message objects; current events put messages under message
            m=payload
        else:
            m=payload.get('message') if isinstance(payload,dict) else None
        if m and m.get('author_id')!=uid:
            unread += 1
            if uid in (m.get('mentions') or []): mentions_count += 1
    return {'channel_id':cid,'last_read_event_id':last,'unread_count':unread,'mention_count':mentions_count}

@app.get('/api/channels/{cid}/read')
async def get_read(cid:int,req:Request):
    u=await require_user(req)
    if not u:return err(401,'unauthorized')
    c=get_db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:c.commit();return err(404,'not found')
    if not visible(c,cid,u['id']):c.commit();return err(403,'forbidden')
    out=read_state_for(c,u['id'],cid);c.close();return {'read_state':out}

@app.post('/api/channels/{cid}/read')
async def post_read(cid:int,req:Request):
    u=await require_user(req)
    if not u:return err(401,'unauthorized')
    c=get_db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:c.commit();return err(404,'not found')
    if not visible(c,cid,u['id']):c.commit();return err(403,'forbidden')
    try: requested=int((await req.json()).get('last_read_event_id',0))
    except: c.commit();return err(400,'bad event id')
    current=c.execute('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone()
    old=int(current['last_read_event_id']) if current else 0
    value=max(old,requested)
    c.execute('INSERT INTO reads(user_id,channel_id,last_read_event_id) VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read_event_id=excluded.last_read_event_id WHERE excluded.last_read_event_id>reads.last_read_event_id',(u['id'],cid,value))
    c.commit();out=read_state_for(c,u['id'],cid);c.close();return {'read_state':out}

# Replace workspace detail with a version that embeds per-channel read states.
@app.get('/api/workspaces/{slug}/detail_with_read_state')
async def unused_detail_alias(slug:str,req:Request):
    return await detail(slug,req)
