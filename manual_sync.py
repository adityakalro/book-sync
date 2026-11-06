"""Explicit, forward-only progress handoffs for the private Sync Now page."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx

from sync_dry_run import (DryRunEngine, ReadFailure, ReadOnlySources, SERVER,
                          Snapshot, kindle_position, crosspoint_progress,
                          load_profile, private_json)
from text_mapping import epub_position_details

KINDLE_URL='https://cde-ta-g7g.amazon.com/FionaCDEServiceEngine/sidecar'
DEVICE_ID='book-sync-manual'


class ManualSources(ReadOnlySources):
    def details(self):
        kindle=self._get('Kindle',KINDLE_URL,auth=self.auth,
                         params={'type':'PDOC','key':self.kindle_document})
        cp=self._get('CrossPoint',f'{SERVER}/syncs/progress/{self.crosspoint_document}',headers=self.headers)
        if not cp:
            raise ReadFailure('Open this book on the X4 and use Sync Progress first')
        snap=Snapshot(kindle_position(kindle,self.kindle_document),
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
        payload={'document':profile['crosspoint_document'],'progress':proposal['target_xpointer'],
                 'percentage':fallback['percentage'],'device':'Book Sync','device_id':DEVICE_ID}
        try:
            response=sources.client.put(SERVER+'/syncs/progress',json=payload,headers=sources.headers)
        except httpx.HTTPError:
            raise ReadFailure('Update outcome is unknown. Click Sync again to recheck both readers.') from None
    else:
        target=proposal['target_sidecar_position']
        if not fresh.kindle_position<target<len(mapper.locations)*150:
            raise ValueError('Proposed Kindle position is not a valid forward move')
        guid=kindle['payload'].get('guid')
        if not isinstance(guid,str) or not guid:
            raise ReadFailure('Kindle returned no document sync identifier')
        stamp=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S%z')
        root=Element('annotations',{'version':'1.0','timestamp':stamp})
        book=SubElement(root,'book',{'key':profile['document'],'type':'PDOC','guid':guid})
        SubElement(book,'last_read',{'action':'modify','begin':str(target),'location':'-1','timestamp':stamp})
        try:
            response=sources.client.post(KINDLE_URL,auth=sources.auth,
                content=tostring(root,encoding='utf-8'),headers={'Content-Type':'text/xml; charset=UTF-8'})
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
    credentials=json.loads((data_root/'crosspoint-auth.json').read_text())
    registry=json.loads((data_root/'onboarding/ready-profiles.json').read_text())
    results=[]
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
            sources=ManualSources(data_root/'amazon-device.json',credentials['username'],
                credentials['auth_key'],profile['document'],profile['crosspoint_document'])
            result=apply_handoff(profile,mapper,profile_id,sources,data_root/'manual-sync'/profile['document'])
        except ReadFailure as error:
            result={'status':'error','message':str(error),'writes':0}
        except (ValueError,OSError,KeyError):
            result={'status':'error','message':'Could not validate this book’s map or saved connection.','writes':0}
        finally:
            if sources:sources.close()
        results.append({'title':title,**result})
    return results
