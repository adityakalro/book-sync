"""Private LAN/WireGuard page for explicit onboarding and reading-progress sync."""
import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
from urllib.parse import urlsplit

import httpx

from manual_sync import sync_registry
from sync_dry_run import ROOT, SERVER, private_json, state_lock


class Jobs:
    def __init__(self,data_root,onboard=None,sync=None):
        self.root=Path(data_root)
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.lock=threading.Lock()
        self.running=False
        self.phase='Ready'
        self.onboard=onboard or self.discover
        self.sync=sync or (lambda notify:sync_registry(self.root,notify))
        saved=self.root/'web-last-sync.json'
        self.last=json.loads(saved.read_text()) if saved.exists() else None

    def status(self):
        return {'connected':(self.root/'crosspoint-auth.json').exists(),
                'running':self.running,'phase':self.phase,'last':self.last}

    def discover(self):
        result=subprocess.run([sys.executable,str(ROOT/'onboarding.py'),
            '--root',str(self.root/'onboarding'),'--amazon-auth',str(self.root/'amazon-kindle-download-device.json'),
            '--readest-auth',str(self.root/'readest-device.json'),'--kfxlib-parent',str(self.root/'vendor/python')],
            capture_output=True,timeout=900,cwd=ROOT)
        if result.returncode:
            raise ValueError('Could not refresh the book libraries. Check the saved connections.')
        report=json.loads((self.root/'onboarding/last-run.json').read_text())
        from collections import Counter
        return dict(Counter(e['status'] for e in report['events']))

    def connect(self,username,password,remember):
        if not remember:
            raise ValueError('Confirm saving the sign-in key to enable one-click sync.')
        if not username or not password:
            raise ValueError('Enter the sync username and password used on your X4.')
        key=hashlib.md5(password.encode()).hexdigest()
        try:
            r=httpx.get(SERVER+'/users/auth',headers={'x-auth-user':username,'x-auth-key':key},
                        timeout=20,follow_redirects=False)
        except httpx.HTTPError:
            raise ValueError('Could not reach CrossPoint. Try again.') from None
        if r.status_code!=200 or r.json().get('authorized')!='OK':
            raise ValueError('CrossPoint did not accept that login.')
        private_json(self.root/'crosspoint-auth.json',{'username':username,'auth_key':key,
            'server':SERVER,'purpose':'User-approved manual reading-progress sync'})

    def start(self):
        if not (self.root/'crosspoint-auth.json').exists():
            raise ValueError('Connect your X4 sync account first.')
        if not self.lock.acquire(blocking=False):return False
        self.running=True;self.phase='Finding new books…'
        threading.Thread(target=self.run,daemon=True).start()
        return True

    def run(self):
        started=datetime.now(timezone.utc).isoformat()
        try:
            with state_lock(self.root/'manual-sync-job'):
                discovery=self.onboard()
                books=self.sync(lambda text:setattr(self,'phase',text))
                failed=any(b['status'] in ('error','unconfirmed','changed') for b in books)
                self.last={'status':'needs_attention' if failed else 'complete',
                    'started_at':started,'finished_at':datetime.now(timezone.utc).isoformat(),
                    'discovery':discovery,'books':books}
        except (ValueError,OSError,subprocess.TimeoutExpired):
            self.last={'status':'error','started_at':started,
                'finished_at':datetime.now(timezone.utc).isoformat(),
                'message':'Sync could not finish. Check the connection and try again.','books':[]}
        finally:
            try:private_json(self.root/'web-last-sync.json',self.last)
            finally:
                self.phase='Ready';self.running=False;self.lock.release()


PAGE='''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Book Sync</title>
<style nonce="NONCE">
:root{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#183c35;background:#f4f3ed;font-size:16px}
*{box-sizing:border-box}body{margin:0}main{max-width:760px;margin:auto;padding:40px 22px 60px}header{display:flex;align-items:center;justify-content:space-between;margin-bottom:58px}.brand{font-weight:760;font-size:19px;letter-spacing:-.5px}.pill{font-size:12px;border:1px solid #b8c8bf;border-radius:30px;padding:7px 11px}.eyebrow{text-transform:uppercase;letter-spacing:2px;font-size:11px;font-weight:700;color:#60776e}h1{font-size:clamp(35px,7vw,51px);font-weight:650;line-height:1.1;letter-spacing:-2px;margin:12px 0 18px}.intro{color:#5d7069;line-height:1.6;max-width:520px;margin-bottom:30px}.card{background:white;border:1px solid #dde3db;border-radius:20px;padding:26px;margin:18px 0;box-shadow:0 5px 15px #183c3504}h2{font-size:18px;letter-spacing:-.3px;margin:0 0 14px}.muted{font-size:14px;color:#63766e;line-height:1.6}.primary{width:100%;background:#205747;color:white;border:0;border-radius:12px;padding:17px 20px;font-size:17px;font-weight:650;cursor:pointer}.primary:disabled{opacity:.58;cursor:wait}.primary:focus-visible,input:focus-visible{outline:3px solid #a8cbb7;outline-offset:3px}#phase{min-height:24px;font-size:14px;text-align:center;margin:14px 0 0;color:#536c61}label{display:block;font-size:14px;margin:15px 0 6px}input:not([type=checkbox]){width:100%;padding:12px;border-radius:9px;border:1px solid #c6d1c9;background:#fcfdfb;font:inherit}.check{display:flex;gap:10px;align-items:flex-start;line-height:1.5;margin:18px 0}.check input{margin-top:4px}.secondary{background:#e9f0e9;color:#205747;font-size:15px;padding:13px}.row{padding:15px 0;border-bottom:1px solid #edf0ea}.row:last-child{border-bottom:0}.book{font-weight:650;font-size:15px;margin-bottom:5px}.result{color:#63766e;font-size:14px}.error{color:#9c443b}.meta{font-size:12px;color:#718178;margin:12px 0}.empty{color:#718178;font-size:14px}.steps{display:grid;grid-template-columns:1fr 1fr;gap:22px}p{line-height:1.6}.steps strong{display:block;font-size:14px}.steps p{font-size:13px;color:#64796d;margin:7px 0 0}footer{margin-top:30px;font-size:12px;color:#7b8a80}#message{font-size:14px;color:#9c443b} [hidden]{display:none!important}@media(max-width:480px){main{padding-top:25px}header{margin-bottom:38px}.card{padding:21px}.steps{grid-template-columns:1fr;gap:16px}}
</style><main><header><div class="brand">▤ &nbsp;Book Sync</div><span class="pill">Private access</span></header>
<div class="eyebrow">Kindle &amp; X4</div><h1>Pick up where<br>you left off.</h1><p class="intro">Find new books and bring your reading progress together. Sync runs only when you ask.</p>
<section class="card" id="connect"><h2>Connect your X4 account</h2><p class="muted">Use the sync.crosspointreader.com login configured on your X4. A private sign-in key will be saved on this Pi so future syncs take one click. Your password is not saved.</p>
<form id="login"><label for="username">Sync username</label><input id="username" name="username" autocomplete="username" required><label for="password">Sync password</label><input id="password" name="password" type="password" autocomplete="current-password" required>
<label class="check"><input type="checkbox" id="remember" required><span>Save my CrossPoint sign-in key on this Pi for reading and updating progress.</span></label><button class="primary secondary" id="connectButton">Connect account</button><p id="message" role="status"></p></form></section>
<section class="card"><button id="sync" class="primary" disabled>Sync now</button><p id="phase" role="status" aria-live="polite">Checking connection…</p></section>
<section class="card"><h2 id="resultTitle">Last sync</h2><div id="results"><p class="empty">No sync has run from this page yet.</p></div><p class="meta" id="time"></p></section>
<section class="card steps"><div><strong>Before you sync</strong><p>Sync your current reader so its latest position reaches the cloud.</p></div><div><strong>On your other reader</strong><p>Use Sync Progress on the X4, or Sync to Furthest Page Read on Kindle, to pick up the update.</p></div></section><footer>Home network or WireGuard VPN · Progress moves forward</footer></main>
<script nonce="NONCE">
const csrf='CSRF';let polling=false;const $=id=>document.getElementById(id);
async function call(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-Book-Sync':csrf}:{},body:body?JSON.stringify(body):undefined,cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.error||'Connection failed');return d;}
function render(s){$('connect').hidden=s.connected;$('sync').disabled=!s.connected||s.running;$('sync').textContent=s.running?'Syncing…':'Sync now';$('phase').textContent=s.running?s.phase:s.connected?'Ready when you are.':'Connect your X4 account to start.';
if(s.last){$('resultTitle').textContent=s.last.status==='complete'?'Sync complete':s.last.status==='needs_attention'?'Some books need attention':'Sync did not finish';$('results').replaceChildren();if(s.last.message){const p=document.createElement('p');p.textContent=s.last.message;p.className='error';$('results').append(p)}for(const b of s.last.books||[]){const row=document.createElement('div');row.className='row';const title=document.createElement('div');title.className='book';title.textContent=b.title;const detail=document.createElement('div');detail.className='result'+(['error','unconfirmed','changed'].includes(b.status)?' error':'');detail.textContent=b.message;row.append(title,detail);$('results').append(row)}const d=s.last.discovery;if(d){const p=document.createElement('p');p.className='meta';p.textContent=(d.ready||0)+' new maps · '+(d.waiting_for_epub||0)+' waiting for an EPUB in Readest';$('results').append(p)}$('time').textContent=s.last.finished_at?'Finished '+new Date(s.last.finished_at).toLocaleString():'';}}
async function refresh(){try{render(await call('/api/status'))}catch(e){$('phase').textContent='Could not reach the Pi. Check your home Wi-Fi or WireGuard connection.'}}
$('sync').addEventListener('click',async()=>{$('sync').disabled=true;try{await call('/api/sync',{});await refresh()}catch(e){$('phase').textContent=e.message;$('sync').disabled=false}});
$('login').addEventListener('submit',async e=>{e.preventDefault();$('connectButton').disabled=true;$('message').textContent='Connecting…';try{await call('/api/connect',{username:$('username').value,password:$('password').value,remember:$('remember').checked});$('password').value='';await refresh()}catch(e){$('message').textContent=e.message}finally{$('connectButton').disabled=false}});
refresh();setInterval(refresh,2500);
</script></html>'''


def handler_for(jobs,hosts,networks):
    csrf=secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def allowed(self):
            try:ip=ipaddress.ip_address(self.client_address[0])
            except ValueError:return False
            return self.headers.get('Host') in hosts and any(ip in n for n in networks)
        def send(self,status,body,kind='application/json',nonce=None):
            data=body.encode() if isinstance(body,str) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type',kind+'; charset=utf-8')
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'none'; connect-src 'self'; script-src 'nonce-"+(nonce or '')+"'; style-src 'nonce-"+(nonce or '')+"'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers();self.wfile.write(data)
        def do_GET(self):
            if not self.allowed():self.send(403,{'error':'Use the home network or WireGuard VPN.'});return
            if self.path=='/':
                nonce=secrets.token_urlsafe(24)
                self.send(200,PAGE.replace('NONCE',nonce).replace('CSRF',csrf),'text/html',nonce)
            elif self.path=='/api/status':self.send(200,jobs.status())
            else:self.send(404,{'error':'Not found'})
        def do_POST(self):
            if not self.allowed():self.send(403,{'error':'Private network required'});return
            if (self.headers.get('Origin')!='http://'+self.headers.get('Host','')
                    or not secrets.compare_digest(self.headers.get('X-Book-Sync',''),csrf)):
                self.send(403,{'error':'Reload this page and try again.'});return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=8192:raise ValueError('Invalid request size')
                data=json.loads(self.rfile.read(size))
                if not isinstance(data,dict):raise ValueError('Invalid request')
            except (ValueError,UnicodeError):self.send(400,{'error':'Invalid request'});return
            if self.path=='/api/connect':
                try:jobs.connect(str(data.get('username','')).strip(),str(data.get('password','')),data.get('remember') is True)
                except ValueError as e:self.send(400,{'error':str(e)});return
                self.send(200,{'connected':True})
            elif self.path=='/api/sync':
                try:started=jobs.start()
                except ValueError as e:self.send(400,{'error':str(e)});return
                self.send(202 if started else 409,{'started':started,'error':None if started else 'A sync is already running.'})
            else:self.send(404,{'error':'Not found'})
    return Handler


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=ROOT/'.private/web-config.json')
    p.add_argument('--data-root',type=Path,default=ROOT/'.private')
    args=p.parse_args();os.umask(0o077)
    config=json.loads(args.config.read_text())
    jobs=Jobs(args.data_root.resolve())
    handler=handler_for(jobs,set(config['hosts']),[ipaddress.ip_network(n) for n in config['networks']])
    server=ThreadingHTTPServer((config.get('bind','0.0.0.0'),int(config.get('port',8780))),handler)
    server.daemon_threads=True
    print('Book Sync page ready; sync runs only when requested.',flush=True)
    try:server.serve_forever()
    finally:server.server_close()


if __name__=='__main__':main()
