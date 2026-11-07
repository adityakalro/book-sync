"""Private LAN/WireGuard page for explicit onboarding and reading-progress sync."""
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import subprocess
import threading
from urllib.parse import urlsplit

import httpx

from book_sync.sync.job import refresh_libraries, run_books
from book_sync.web.setup import Connections
from book_sync.errors import ReadFailure
from book_sync.storage import ROOT, private_json, state_lock


class Jobs:
    def __init__(self,data_root,onboard=None,sync=None,plugins=None):
        self.root=Path(data_root)
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.connections=Connections(self.root,plugins)
        self.custom_jobs=onboard is not None or sync is not None
        self.lock=threading.Lock()
        self.running=False
        self.phase='Ready'
        self.onboard=onboard or self.discover
        self.sync=sync or self.sync_books
        self.failed_catalogs=set()
        saved=self.root/'web-last-sync.json'
        self.last=json.loads(saved.read_text()) if saved.exists() else None

    def status(self):
        return {'connected':self.connections.ready(), 'services':self.connections.status(),
                'running':self.running,'phase':self.phase,'last':self.last}

    def discover(self):
        counts,self.failed_catalogs=refresh_libraries(self.root,self.connections.connected())
        return counts

    def sync_books(self,notify):
        return run_books(self.root,self.connections.connected(),notify,self.failed_catalogs)

    def connect(self,username,password,remember):
        # Compatibility for clients using the old CrossPoint setup endpoint.
        return self.setup_action('crosspoint','start',{'username':username,'password':password,
                                                       'consent':remember})

    def setup_action(self,service,action,data):
        if not self.lock.acquire(blocking=False):
            raise ValueError('Wait for the current sync or setup action to finish.')
        try:
            return self.connections.apply(service,action,data)
        finally:
            self.lock.release()

    def start(self):
        if not self.lock.acquire(blocking=False):return False
        if not self.custom_jobs and not self.connections.ready():
            self.lock.release()
            raise ValueError('Connect two readers and a book catalog in Setup.')
        self.running=True;self.phase='Finding new books…'
        threading.Thread(target=self.run,daemon=True).start()
        return True

    def run(self):
        started=datetime.now(timezone.utc).isoformat()
        try:
            with state_lock(self.root/'manual-sync-job'):
                discovery=self.onboard()
                books=self.sync(lambda text:setattr(self,'phase',text))
                failed=bool(discovery.get('discovery_error')) or any(b['status'] in ('error','unconfirmed','changed') for b in books)
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


TEMPLATES = Path(__file__).with_name('templates')
PAGE_SHELL = (TEMPLATES/'base.html').read_text(encoding='utf-8')
PAGE = PAGE_SHELL + (TEMPLATES/'sync.html').read_text(encoding='utf-8')
SETUP_PAGE = PAGE_SHELL + (TEMPLATES/'setup.html').read_text(encoding='utf-8')


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
