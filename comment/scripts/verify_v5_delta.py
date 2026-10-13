"""Offline check: python verify_v5_delta.py --r3 BASE.zip --v5 OLD.zip --patch NEW.zip.

Reconstructs original shipped sources in memory only. Does not install, extract,
overwrite adaptations, connect to a service, or open any database.
"""
import argparse
import hashlib
import json
from pathlib import PurePosixPath
from zipfile import ZipFile


def sha(raw): return hashlib.sha256(raw).hexdigest()


def read_archive(path):
    with ZipFile(path) as z:
        if z.testzip(): raise ValueError('archive CRC mismatch')
        entries = [n for n in z.namelist() if not n.endswith('/')]
        if len(entries) != len(set(entries)): raise ValueError('duplicate archive path')
        names = [n for n in entries if n.endswith('/MANIFEST.json')]
        if len(names) != 1: raise ValueError('ambiguous manifest')
        prefix=names[0].rsplit('/',1)[0]+'/'
        manifest=json.loads(z.read(names[0])); files={}
        for row in manifest['files']:
            name=row['path']; parts=PurePosixPath(name)
            if parts.is_absolute() or '..' in parts.parts or '\\' in name or ':' in name:
                raise ValueError('unsafe path')
            raw=z.read(prefix+name)
            if len(raw)!=row['bytes'] or sha(raw)!=row['sha256']: raise ValueError('hash: '+name)
            files[name]=raw
        if set(entries)!={prefix+n for n in files}|{prefix+'MANIFEST.json'}:
            raise ValueError('unexpected archive member')
        return manifest,files


def merge(old, manifest, delta):
    for row in manifest['files']:
        if row.get('role')=='patch-support': continue
        name=row['path']; parent=old.get(name)
        if (sha(parent) if parent is not None else None)!=row['base_sha256']:
            raise ValueError('parent differs: '+name)
        old[name]=delta[name]
    expected={r['path']:r for r in manifest['target_files']}
    if old.keys()!=expected.keys(): raise ValueError('incomplete merged target')
    for name,raw in old.items():
        if sha(raw)!=expected[name]['sha256'] or len(raw)!=expected[name]['bytes']:
            raise ValueError('target differs: '+name)
        if name.endswith('.py'): compile(raw.decode('utf-8'),name,'exec')


if __name__=='__main__':
    from pathlib import Path
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('r3','v5','patch'): p.add_argument('--'+name,type=Path,required=True)
    args=p.parse_args()
    latest,delta=read_archive(args.patch)
    if latest.get('kind')!='v5-source-delta': raise ValueError('not a V5 delta')
    pins={row['archive']:row['sha256'] for row in latest['baseline']}
    for archive in (args.r3,args.v5):
        if pins.get(archive.name)!=sha(archive.read_bytes()): raise ValueError('baseline archive differs')
    _,files=read_archive(args.r3)
    v5,updates=read_archive(args.v5); merge(files,v5,updates)
    merge(files,latest,delta)
    print('PASS: original V5 baseline + cumulative patch =',len(files),'verified target files; no installation performed')
