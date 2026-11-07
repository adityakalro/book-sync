"""Explicit, forward-only progress handoffs for the private Sync Now page."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import httpx

from book_sync.errors import MissingProgress, ReadFailure
from book_sync.mapping.text import epub_position_details
from book_sync.plugins.crosspoint.client import crosspoint_progress, read_progress, write_progress
from book_sync.plugins.kindle.progress import kindle_position, read_sidecar, write_position
from book_sync.storage import private_json
from book_sync.sync.dry_run import DryRunEngine, ReadOnlySources, Snapshot, load_profile


class ManualSources(ReadOnlySources):
    def _get(self,service,url,**kwargs):
        # Retry only reads after transient connection/DNS failures. PUT/POST
        # remain single attempts and ambiguous write outcomes are surfaced.
        for attempt in range(3):
            try:return super()._get(service,url,**kwargs)
            except ReadFailure as error:
                if not str(error).endswith('connection failed') or attempt==2:raise
                time.sleep(0.25)

    def details(self, *, allow_missing=False):
        kindle=read_sidecar(self._get, self.auth, self.kindle_document)
        try:
            position=kindle_position(kindle,self.kindle_document)
        except MissingProgress:
            if not allow_missing:raise
            position=None
        if not self.headers.get('x-auth-key'):
            return Snapshot(position, ''),kindle,{}
        try:
            cp=read_progress(self._get, self.headers, self.crosspoint_document)
        except ReadFailure as error:
            if str(error) != 'CrossPoint read failed: HTTP 404':raise
            cp={}
        if not cp:
            if allow_missing:return Snapshot(position, ''),kindle,{}
            raise MissingProgress('Open this book on the X4 and use Sync Progress first.')
        snap=Snapshot(position,
                      crosspoint_progress(cp,self.crosspoint_document),
                      str(cp.get('device','')),cp.get('timestamp'))
        return snap,kindle,cp


def same_position(a,b):
    return (a.kindle_position,a.crosspoint_xpointer)==(b.kindle_position,b.crosspoint_xpointer)


def apply_handoff(profile,mapper,profile_id,sources,directory):
    """Plan, re-read, write once, and verify. Never automatically retry a PUT/POST."""
    directory=Path(directory);directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    before,kindle,cp=sources.details()
    engine=DryRunEngine(mapper,profile_id)
    decision=engine.observe(before)
    private_json(directory/'observation.json',decision)
    if decision['status']=='in_sync':
        return {'status':'in_sync','message':'Already at the same place','writes':0}
    fresh,kindle,cp=sources.details()
    if not same_position(before,fresh):
        return {'status':'changed','message':'Reading position changed during sync. Try again.','writes':0}
    proposal=decision['proposal']
    direction=proposal['direction']
    private_json(directory/'attempt.json',{'phase':'about_to_write','proposal':proposal,
        'observed':asdict(fresh),'kindle_backup':kindle,'crosspoint_backup':cp,
        'started_at':datetime.now(timezone.utc).isoformat()})
    if direction=='kindle_to_crosspoint':
        epub=Path(profile['files']['epub']['path'])
        if not epub.is_absolute():
            raise ValueError('Live sync requires an absolute validated EPUB path')
        fallback=epub_position_details(epub,proposal['target_xpointer'])
        try:
            response=write_progress(sources.client,sources.headers,profile['crosspoint_document'],
                                    proposal['target_xpointer'],fallback['percentage'])
        except httpx.HTTPError:
            raise ReadFailure('Update outcome is unknown. Click Sync again to recheck both readers.') from None
    else:
        target=proposal['target_sidecar_position']
        if not fresh.kindle_position<target<len(mapper.locations)*150:
            raise ValueError('Proposed Kindle position is not a valid forward move')
        try:
            response=write_position(sources.client,sources.auth,profile['document'],
                                    kindle['payload'].get('guid'),target)
        except httpx.HTTPError:
            raise ReadFailure('Update outcome is unknown. Click Sync again to recheck both readers.') from None
    if response.status_code!=200:
        raise ReadFailure(f'Progress update returned HTTP {response.status_code}. Recheck before trying again.')
    try:
        after,_,_=sources.details()
    except ReadFailure:
        result={'status':'unconfirmed','writes':1,
                'message':'Update accepted, but verification failed. Try again to recheck both readers.'}
        private_json(directory/'result.json',{'result':result})
        return result
    resolved=DryRunEngine(mapper,profile_id).observe(after)
    if direction=='kindle_to_crosspoint':
        verified=after.crosspoint_xpointer==proposal['target_xpointer'] and resolved['status']=='in_sync'
    else:
        verified=after.kindle_position==proposal['target_sidecar_position'] and resolved['status']=='in_sync'
    result={'status':'synced' if verified else 'unconfirmed','writes':1,'direction':direction,
        'message':('X4 progress updated' if direction=='kindle_to_crosspoint' else 'Kindle progress updated')
                  if verified else 'Update accepted, but positions differ. Sync your readers and try again.'}
    private_json(directory/'result.json',{'result':result,'readback':asdict(after),
        'finished_at':datetime.now(timezone.utc).isoformat()})
    return result


def sync_registry(data_root,notify=lambda message:None):
    data_root=Path(data_root)
    crosspoint=data_root/'crosspoint-auth.json'
    credentials=json.loads(crosspoint.read_text()) if crosspoint.exists() else {}
    registry=json.loads((data_root/'onboarding/ready-profiles.json').read_text())
    results=[]
    libby=None
    if (data_root/'libby-device.json').exists():
        from book_sync.plugins.libby.client import Libby
        libby=Libby(data_root/'libby-device.json')
    for item in registry['profiles']:
        notify('Checking a matched book…')
        # Registry path/hash must agree; each profile then revalidates its files,
        # text identity and coordinate checks before being allowed to write.
        import hashlib
        path=Path(item['path'])
        if hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
            results.append({'title':'Book','status':'error','message':'Map changed; onboarding must rebuild it.','writes':0})
            continue
        sources=None
        title='Book'
        try:
            profile,mapper,profile_id=load_profile(path)
            title=profile.get('x4_filename','Book.epub').removesuffix('.epub')
            notify('Syncing '+title+'…')
            loan=libby.match(title,profile['files']['epub']['path']) if libby else None
            if not loan and not credentials:
                results.append({'title':title,'status':'skipped','writes':0,
                                'message':'No matching Libby audiobook or connected X4.'})
                continue
            sources=ManualSources(data_root/'amazon-device.json',credentials.get('username',''),
                credentials.get('auth_key',''),profile['document'],profile['crosspoint_document'])
            if loan:
                from book_sync.sync.audiobook import apply_three_way
                result=apply_three_way(profile,mapper,sources,
                    data_root/'manual-sync'/profile['document'],libby,loan)
                if result['status']=='unavailable':
                    if credentials:
                        result=apply_handoff(profile,mapper,profile_id,sources,
                            data_root/'manual-sync'/profile['document'])
                        result['message']+=' Libby has no saved position for this book.'
                    else:
                        result['status']='skipped'
            else:
                result=apply_handoff(profile,mapper,profile_id,sources,data_root/'manual-sync'/profile['document'])
        except MissingProgress as error:
            result={'status':'skipped','message':str(error),'writes':0}
        except ReadFailure as error:
            result={'status':'error','message':str(error),'writes':0}
        except (ValueError,OSError,KeyError):
            result={'status':'error','message':'Could not validate this book’s map or saved connection.','writes':0}
        finally:
            if sources:sources.close()
        results.append({'title':title,**result})
    if libby:libby.close()
    return results
