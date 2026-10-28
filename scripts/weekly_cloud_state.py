"""Durable PRIVATE GitHub release state. No local laptop or cache dependency."""
from pathlib import Path
import sys,subprocess,json,os,tarfile,tempfile,argparse,hashlib,hmac,shutil
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from nfl2026.weekly_research.core import Store,canonical,sha,clock,load,immutable
TAG='research-state-v1'
def gh(*args):return subprocess.run(['gh',*args],check=True,capture_output=True,text=True,encoding='utf-8').stdout
def settings():
    repo=os.environ['RESEARCH_PRIVATE_STATE_REPO'];key=os.environ['RESEARCH_STATE_HMAC_KEY']
    if not key or not repo or repo=='racingmodel/nfl-2026-visualizer':raise ValueError('Durable state requires an explicitly private repository and signing key')
    meta=json.loads(gh('api','repos/'+repo))
    if meta.get('private') is not True:raise ValueError('State repository must be private')
    return repo,key
def archive(state,destination,key):
    manifest={str(p.relative_to(state)).replace('\\','/'):sha(p) for p in state.rglob('*') if p.is_file() and '.writer-lock' not in p.parts}
    receipt=dict(schema='private-weekly-state-v1',files=manifest,created_at_utc=clock().isoformat());receipt['signature']=hmac.new(key.encode(),canonical(receipt),hashlib.sha256).hexdigest()
    with tarfile.open(destination,'w:gz') as tf:
        import io
        b=canonical(receipt);i=tarfile.TarInfo('state_manifest.json');i.size=len(b);tf.addfile(i,io.BytesIO(b))
        for name in manifest:tf.add(state/name,arcname=name,recursive=False)
def unpack(path,out,key):
    out=Path(out).resolve();out.mkdir(parents=True,exist_ok=False)
    with tarfile.open(path,'r:gz') as tf:
        for m in tf.getmembers():
            if not (out/m.name).resolve().is_relative_to(out) or not (m.isdir() or m.isfile()):raise ValueError('Unsafe state archive')
        tf.extractall(out,filter='data')
    m=load(out/'state_manifest.json');signature=m.pop('signature')
    if not hmac.compare_digest(signature,hmac.new(key.encode(),canonical(m),hashlib.sha256).hexdigest()):raise ValueError('State authority signature differs')
    for n,h in m['files'].items():
        if sha(out/n)!=h:raise ValueError('State hash differs')
    (out/'state_manifest.json').unlink() # operational wrapper; original manifest lives in immutable archive
    for p in out.glob('2026-w*/HEAD.json'):Store(p.parent).head()
def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['restore','save']);p.add_argument('--out',type=Path,required=True);p.add_argument('--bootstrap',default='false');a=p.parse_args();repo,key=settings()
    if a.command=='restore':
        try:release=json.loads(gh('api',f'repos/{repo}/releases/tags/{TAG}'))
        except subprocess.CalledProcessError:
            if a.bootstrap.lower()=='true':a.out.mkdir(parents=True,exist_ok=False);return
            raise ValueError('No durable state release; explicit first bootstrap required')
        assets=sorted([v for v in release['assets'] if v['name'].startswith('state-') and v['name'].endswith('.tar.gz')],key=lambda x:x['name'],reverse=True)
        if not assets:
            if a.bootstrap.lower()=='true':a.out.mkdir(parents=True,exist_ok=False);return
            raise ValueError('No durable accepted state; no silent reset')
        with tempfile.TemporaryDirectory() as td:
            gh('release','download',TAG,'--repo',repo,'--pattern',assets[0]['name'],'--dir',td);file=Path(td)/assets[0]['name'];expected=assets[0]['name'].split('-')[-1].removesuffix('.tar.gz')
            if sha(file)!=expected:raise ValueError('State asset identity differs')
            unpack(file,a.out,key)
    else:
        if not any(a.out.glob('2026-w*/HEAD.json')):raise ValueError('No accepted state to save')
        for pth in a.out.glob('2026-w*/HEAD.json'):Store(pth.parent).head()
        with tempfile.TemporaryDirectory() as td:
            pth=Path(td)/'state.tar.gz';archive(a.out,pth,key);name='state-'+clock().strftime('%Y%m%dT%H%M%S%fZ')+'-'+sha(pth)+'.tar.gz';q=pth.with_name(name);pth.rename(q)
            try:gh('api',f'repos/{repo}/releases/tags/{TAG}')
            except subprocess.CalledProcessError:gh('release','create',TAG,'--repo',repo,'--title','Immutable private weekly research state','--notes','Signed immutable state snapshots; no public site/model promotion.')
            gh('release','upload',TAG,str(q),'--repo',repo) # no clobber; preserve all historical versions
        print('Durable private state saved',flush=True)
if __name__=='__main__':main()
