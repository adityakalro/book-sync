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
import time
from urllib.parse import urlsplit

import httpx

from manual_sync import sync_registry
from service_setup import Connections
from sync_dry_run import ROOT, SERVER, ReadFailure, private_json, state_lock


class Jobs:
    def __init__(self,data_root,onboard=None,sync=None):
        self.root=Path(data_root)
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.connections=Connections(self.root)
        self.custom_jobs=onboard is not None or sync is not None
        self.lock=threading.Lock()
        self.running=False
        self.phase='Ready'
        self.onboard=onboard or self.discover
        self.sync=sync or (lambda notify:sync_registry(self.root,notify))
        saved=self.root/'web-last-sync.json'
        self.last=json.loads(saved.read_text()) if saved.exists() else None

    def status(self):
        return {'connected':self.connections.ready(), 'services':self.connections.status(),
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
        if not self.lock.acquire(blocking=False):
            raise ValueError('Wait for the current sync or setup action to finish.')
        try:
            if (self.root/'crosspoint-auth.json').exists():
                raise ValueError('Disconnect CrossPoint before connecting another account.')
            self.connect_crosspoint(username,password,remember)
        finally:
            self.lock.release()

    def setup_action(self,service,action,data):
        if not self.lock.acquire(blocking=False):
            raise ValueError('Wait for the current sync or setup action to finish.')
        try:
            return self.connections.apply(service,action,data)
        finally:
            self.lock.release()

    def connect_crosspoint(self,username,password,remember):
        if not remember:
            raise ValueError('Confirm saving the sign-in key to enable one-click sync.')
        if not username or not password:
            raise ValueError('Enter the sync username and password used on your X4.')
        key=hashlib.md5(password.encode()).hexdigest()
        for attempt in range(3):
            try:
                r=httpx.get(SERVER+'/users/auth',headers={'x-auth-user':username,'x-auth-key':key},
                            timeout=12,follow_redirects=False)
                break
            except (httpx.ConnectError,httpx.ConnectTimeout):
                if attempt==2:
                    raise ValueError('Could not reach CrossPoint after retrying. Check the Pi’s connection and try again.') from None
                time.sleep(0.25)
            except httpx.HTTPError:
                raise ValueError('CrossPoint did not finish responding. Try again.') from None
        if r.status_code!=200 or r.json().get('authorized')!='OK':
            raise ValueError('CrossPoint did not accept that login.')
        private_json(self.root/'crosspoint-auth.json',{'username':username,'auth_key':key,
            'server':SERVER,'purpose':'User-approved manual reading-progress sync'})

    def start(self):
        if not self.lock.acquire(blocking=False):return False
        if not self.custom_jobs and not self.connections.ready():
            self.lock.release()
            raise ValueError('Connect Kindle, Readest, and at least one destination in Setup.')
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


PAGE_SHELL='''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Book Sync</title>
<style nonce="NONCE">
:root{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#183c35;background:#f4f3ed;font-size:16px}
*{box-sizing:border-box}body{margin:0}main{max-width:760px;margin:auto;padding:40px 22px 60px}header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:45px}nav{display:flex;gap:16px}a{color:#205747;font-weight:650}a.primary{display:block;text-decoration:none;text-align:center}.service-title{display:flex;align-items:center;justify-content:space-between;gap:8px}.service-title h2{margin:0}.service-state{font-size:13px;color:#60776e}.actions{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-top:12px}.actions button{width:auto}.code{font:700 26px ui-monospace,monospace;letter-spacing:3px}.setup-feedback{margin:12px 0 0}.brand{font-weight:760;font-size:19px;letter-spacing:-.5px}.pill{font-size:12px;border:1px solid #b8c8bf;border-radius:30px;padding:7px 11px}.eyebrow{text-transform:uppercase;letter-spacing:2px;font-size:11px;font-weight:700;color:#60776e}h1{font-size:clamp(35px,7vw,51px);font-weight:650;line-height:1.1;letter-spacing:-2px;margin:12px 0 18px}.intro{color:#5d7069;line-height:1.6;max-width:520px;margin-bottom:30px}.card{background:white;border:1px solid #dde3db;border-radius:20px;padding:26px;margin:18px 0;box-shadow:0 5px 15px #183c3504}h2{font-size:18px;letter-spacing:-.3px;margin:0 0 14px}.muted{font-size:14px;color:#63766e;line-height:1.6}.primary{width:100%;background:#205747;color:white;border:0;border-radius:12px;padding:17px 20px;font-size:17px;font-weight:650;cursor:pointer}.primary:disabled{opacity:.58;cursor:wait}.primary:focus-visible,input:focus-visible{outline:3px solid #a8cbb7;outline-offset:3px}#phase{min-height:24px;font-size:14px;text-align:center;margin:14px 0 0;color:#536c61}label{display:block;font-size:14px;margin:15px 0 6px}input:not([type=checkbox]){width:100%;padding:12px;border-radius:9px;border:1px solid #c6d1c9;background:#fcfdfb;font:inherit}.check{display:flex;gap:10px;align-items:flex-start;line-height:1.5;margin:18px 0}.check input{margin-top:4px}.secondary{background:#e9f0e9;color:#205747;font-size:15px;padding:13px}.row{padding:15px 0;border-bottom:1px solid #edf0ea}.row:last-child{border-bottom:0}.book{font-weight:650;font-size:15px;margin-bottom:5px}.result{color:#63766e;font-size:14px}.error{color:#9c443b}.meta{font-size:12px;color:#718178;margin:12px 0}.empty{color:#718178;font-size:14px}.steps{display:grid;grid-template-columns:1fr 1fr;gap:22px}p{line-height:1.6}.steps strong{display:block;font-size:14px}.steps p{font-size:13px;color:#64796d;margin:7px 0 0}footer{margin-top:30px;font-size:12px;color:#7b8a80}#message{font-size:14px;color:#9c443b} [hidden]{display:none!important}@media(max-width:480px){main{padding-top:25px}header{margin-bottom:38px}.card{padding:21px}.steps{grid-template-columns:1fr;gap:16px}}
</style><main><header><div class="brand">▤ &nbsp;Book Sync</div><nav aria-label="Book Sync"><a href="/">Sync</a><a href="/setup">Setup</a></nav></header>'''

PAGE=PAGE_SHELL + '''
<div class="eyebrow">Kindle · X4 · Libby</div><h1>Pick up where<br>you left off.</h1><p class="intro">Find new books and bring your reading and listening progress together. Sync runs only when you ask.</p>
<section class="card" id="setupNotice" hidden><h2>Connect your services</h2><p class="muted">Link Kindle and Readest, then add CrossPoint or Libby to sync your place.</p><a class="primary secondary" href="/setup">Open setup</a></section>
<section class="card"><button id="sync" class="primary" disabled>Sync now</button><p id="phase" role="status" aria-live="polite">Checking connection…</p></section>
<section class="card"><h2 id="resultTitle">Last sync</h2><div id="results"><p class="empty">No sync has run from this page yet.</p></div><p class="meta" id="time"></p></section>
<section class="card steps"><div><strong>Before you sync</strong><p>Sync your current reader or Libby so its latest position reaches the cloud.</p></div><div><strong>On your other reader</strong><p>Use Sync Progress on the X4, Sync to Furthest Page Read on Kindle, or Synchronize Shelf in Libby to pick up the update.</p></div></section><footer>Home network or WireGuard VPN · Kindle uses its furthest-read marker</footer></main>
<script nonce="NONCE">
const csrf='CSRF';let polling=false;const $=id=>document.getElementById(id);
async function call(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-Book-Sync':csrf}:{},body:body?JSON.stringify(body):undefined,cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.error||'Connection failed');return d;}
function render(s){$('setupNotice').hidden=s.connected;$('sync').disabled=!s.connected||s.running;$('sync').textContent=s.running?'Syncing…':'Sync now';$('phase').textContent=s.running?s.phase:s.connected?'Ready when you are.':'Finish setup to start syncing.';
if(s.last){$('resultTitle').textContent=s.last.status==='complete'?'Sync complete':s.last.status==='needs_attention'?'Some books need attention':'Sync did not finish';$('results').replaceChildren();if(s.last.message){const p=document.createElement('p');p.textContent=s.last.message;p.className='error';$('results').append(p)}for(const b of s.last.books||[]){const row=document.createElement('div');row.className='row';const title=document.createElement('div');title.className='book';title.textContent=b.title;const detail=document.createElement('div');detail.className='result'+(['error','unconfirmed','changed'].includes(b.status)?' error':'');detail.textContent=b.message;row.append(title,detail);$('results').append(row)}const d=s.last.discovery;if(d){const p=document.createElement('p');p.className='meta';p.textContent=(d.ready||0)+' new maps · '+(d.waiting_for_epub||0)+' waiting for an EPUB in Readest';$('results').append(p)}$('time').textContent=s.last.finished_at?'Finished '+new Date(s.last.finished_at).toLocaleString():'';}}
async function refresh(){try{render(await call('/api/status'))}catch(e){$('phase').textContent='Could not reach the Pi. Check your home Wi-Fi or WireGuard connection.'}}
$('sync').addEventListener('click',async()=>{$('sync').disabled=true;try{await call('/api/sync',{});await refresh()}catch(e){$('phase').textContent=e.message;$('sync').disabled=false}});
refresh();setInterval(refresh,2500);
</script></html>'''

SETUP_PAGE=PAGE_SHELL + '''<div class="eyebrow">Connections</div>
<h1>Connect your services</h1>
<p class="intro">Link Kindle and Readest to match your books. Connect CrossPoint, Libby, or both to sync your place. This installation shares one set of accounts.</p>
<p class="muted" id="setupStatus" role="status">Checking saved connections…</p>
<section class="card" data-service="kindle">
  <div class="service-title"><h2>Kindle</h2><span class="service-state"></span></div>
  <p class="muted">Sign in on Amazon to link your Send to Kindle library and reading position.</p>
  <div class="setup-controls" hidden>
    <label class="check"><input type="checkbox" class="consent">Allow Book Sync to register a Kindle device and save its access on this installation.</label>
    <button class="primary secondary start">Start Amazon sign-in</button>
    <div class="pairing" hidden>
      <p><a class="provider-link" target="_blank" rel="noopener noreferrer">Open Amazon sign-in</a></p>
      <form class="finish-form">
        <label for="kindle-completion">After signing in, paste the full address of the Amazon completion page.</label>
        <input id="kindle-completion" type="url" autocomplete="off" spellcheck="false" required>
        <div class="actions"><button class="primary secondary">Complete connection</button></div>
      </form>
    </div>
  </div>
  <div class="actions"><button class="primary secondary disconnect" hidden>Disconnect Kindle</button></div>
  <p class="muted setup-feedback" role="status"></p>
</section>
<section class="card" data-service="readest">
  <div class="service-title"><h2>Readest</h2><span class="service-state"></span></div>
  <p class="muted">Link your Readest Cloud library so Book Sync can find and download matching EPUBs.</p>
  <div class="setup-controls" hidden>
    <label class="check"><input type="checkbox" class="consent">Allow Book Sync to save a Readest reader key on this installation.</label>
    <button class="primary secondary start">Get Readest code</button>
    <div class="pairing" hidden>
      <p class="code"></p><p><a class="provider-link" target="_blank" rel="noopener noreferrer">Approve in Readest</a></p>
      <button class="primary secondary finish">Check approval</button>
    </div>
  </div>
  <div class="actions"><button class="primary secondary disconnect" hidden>Disconnect Readest</button></div>
  <p class="muted setup-feedback" role="status"></p>
</section>
<section class="card" data-service="crosspoint">
  <div class="service-title"><h2>CrossPoint</h2><span class="service-state"></span></div>
  <p class="muted">Use the CrossPoint sync account configured on your X4.</p>
  <div class="setup-controls" hidden>
    <form id="crosspoint-login">
      <label for="username">Sync username</label><input id="username" autocomplete="username" required>
      <label for="password">Sync password</label><input id="password" type="password" autocomplete="current-password" required>
      <label class="check"><input type="checkbox" id="remember" required>Save my CrossPoint sign-in key on this installation. The password is used to create the key and is discarded.</label>
      <button class="primary secondary">Connect CrossPoint</button>
    </form>
  </div>
  <div class="actions"><button class="primary secondary disconnect" hidden>Disconnect CrossPoint</button></div>
  <p class="muted setup-feedback" role="status"></p>
</section>
<section class="card" data-service="libby">
  <div class="service-title"><h2>Libby</h2><span class="service-state"></span></div>
  <p class="muted">Link as another Libby device to sync listening progress for matching audiobook loans.</p>
  <div class="setup-controls" hidden>
    <label class="check"><input type="checkbox" class="consent">Allow Book Sync to save a Libby session with access to your cards and loans.</label>
    <button class="primary secondary start">Get Libby code</button>
    <div class="pairing" hidden>
      <p class="code"></p><p class="muted">In Libby, open <strong>Menu → Copy To Another Device</strong> and enter the code.</p>
      <button class="primary secondary finish">Check connection</button>
    </div>
  </div>
  <div class="actions"><button class="primary secondary disconnect" hidden>Disconnect Libby</button></div>
  <p class="muted setup-feedback" role="status"></p>
</section>
<footer>Connections apply to this installation. Disconnecting removes the saved access here. Manage registered devices in the provider’s account settings.</footer>
</main>
<script nonce="NONCE">
const csrf='CSRF';
const cards=[...document.querySelectorAll('[data-service]')];
const get=(card,selector)=>card.querySelector(selector);
async function call(path,body){
  const response=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-Book-Sync':csrf}:{},body:body?JSON.stringify(body):undefined,cache:'no-store'});
  const result=await response.json();
  if(!response.ok)throw Error(result.error||'Connection failed');
  return result;
}
async function refresh(){
  const state=await call('/api/status');
  for(const card of cards){
    const service=state.services[card.dataset.service];
    const connected=service.connected;
    get(card,'.service-state').textContent=connected?'Connected':'Not connected';
    get(card,'.setup-controls').hidden=connected;
    const disconnect=get(card,'.disconnect');
    disconnect.hidden=!service.can_disconnect;
    disconnect.textContent=connected?'Disconnect':'Reset setup';
  }
  document.getElementById('setupStatus').textContent=state.running?'A sync is running. Connection changes wait until it finishes.':state.connected?'Ready to sync.':'Connect Kindle, Readest, and CrossPoint or Libby.';
}
async function act(card,button,task){
  const message=get(card,'.setup-feedback');
  button.disabled=true;message.textContent='Working…';message.classList.remove('error');
  try{message.textContent=await task()||'';await refresh()}
  catch(error){message.textContent=error.message;message.classList.add('error')}
  finally{button.disabled=false}
}
for(const card of cards){
  const service=card.dataset.service;
  const setup=(action,data={})=>call('/api/setup',{service,action,...data});
  const start=get(card,'.start');
  if(start)start.onclick=()=>act(card,start,async()=>{
    const result=await setup('start',{consent:get(card,'.consent').checked});
    const link=get(card,'.provider-link');
    const url=result.login_url||result.verification_uri_complete;
    if(link&&url)link.href=url;
    const code=get(card,'.code');
    if(code)code.textContent=result.code||result.user_code;
    get(card,'.pairing').hidden=false;
  });
  const finish=get(card,'.finish');
  if(finish)finish.onclick=()=>act(card,finish,async()=>{
    await setup('finish');get(card,'.pairing').hidden=true;return 'Connected.';
  });
  const finishForm=get(card,'.finish-form');
  if(finishForm)finishForm.onsubmit=event=>{
    event.preventDefault();
    act(card,get(finishForm,'button'),async()=>{
      await setup('finish',{completion_url:get(finishForm,'input').value});
      finishForm.reset();get(card,'.pairing').hidden=true;return 'Connected.';
    });
  };
  const disconnect=get(card,'.disconnect');
  disconnect.onclick=()=>{
    if(!window.confirm('Disconnect this service from Book Sync?'))return;
    act(card,disconnect,async()=>{
      await setup('disconnect',{confirm:true});
      const pairing=get(card,'.pairing');if(pairing)pairing.hidden=true;
      return 'Disconnected.';
    });
  };
}
const crosspoint=document.getElementById('crosspoint-login');
crosspoint.onsubmit=event=>{
  event.preventDefault();
  act(crosspoint.closest('[data-service]'),get(crosspoint,'button'),async()=>{
    await call('/api/connect',{username:document.getElementById('username').value,password:document.getElementById('password').value,remember:document.getElementById('remember').checked});
    crosspoint.reset();return 'Connected.';
  });
};
refresh().catch(error=>{document.getElementById('setupStatus').textContent=error.message});
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
            if self.path in ('/','/setup'):
                nonce=secrets.token_urlsafe(24)
                page=SETUP_PAGE if self.path=='/setup' else PAGE
                self.send(200,page.replace('NONCE',nonce).replace('CSRF',csrf),'text/html',nonce)
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
            elif self.path=='/api/setup':
                try:result=jobs.setup_action(data.get('service'),data.get('action'),data)
                except (ValueError,ReadFailure) as e:self.send(400,{'error':str(e)});return
                except (httpx.HTTPError,OSError,KeyError,TypeError):self.send(502,{'error':'Could not complete setup. Check the service and try again.'});return
                self.send(200,result)
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
