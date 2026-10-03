"""Create a source-only archive from an audited, hash-checked release manifest."""
from pathlib import Path
import argparse
import hashlib
import json
import zipfile
from check_release import ROOT, audit

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    hits,current=audit()
    if hits:raise SystemExit('Release gate failed; run check_release.py for locations.')
    manifest=ROOT/'RELEASE_MANIFEST.json'
    expected=json.loads(manifest.read_text(encoding='utf-8'))['files']
    if current!=expected:raise SystemExit('Manifest is stale; rerun check_release.py --manifest.')
    if args.output.resolve().is_relative_to(ROOT):raise SystemExit('Place the archive outside the source tree.')
    with zipfile.ZipFile(args.output,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for rel in sorted(expected):archive.write(ROOT/rel,rel)
        archive.write(manifest,manifest.name)
    with zipfile.ZipFile(args.output) as archive:
        assert set(archive.namelist())==set(expected)|{manifest.name}
        assert archive.testzip() is None
        for rel,digest in expected.items():assert hashlib.sha256(archive.read(rel)).hexdigest()==digest
    print(f'PASS: {len(expected)+1} archive entries verified against manifest.')

if __name__=='__main__':main()
