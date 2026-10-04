"""Run after unpacking: python verify_package.py. No credentials or network."""
import hashlib
import json
from pathlib import Path
import re


def verify(root=None):
    root=Path(root or __file__).resolve()
    if root.is_file():root=root.parent
    manifest=json.loads((root/'MANIFEST.json').read_text(encoding='utf-8'))
    actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p.name!='MANIFEST.json' and '__pycache__' not in p.parts}
    expected={entry['path'] for entry in manifest['files']}
    if actual!=expected:raise RuntimeError('unexpected or missing package files')
    for entry in manifest['files']:
        path=root/entry['path'];raw=path.read_bytes()
        if len(raw)!=entry['bytes'] or hashlib.sha256(raw).hexdigest()!=entry['sha256']:
            raise RuntimeError('source hash mismatch: '+entry['path'])
        if path.suffix=='.py':compile(raw.decode('utf-8'),entry['path'],'exec')
    html=(root/'integration_examples/social_roster_fragment.html').read_text(encoding='utf-8')
    ids=set(re.findall(r'\bid="([^\"]+)"',html))
    for script in ('social-contacts.js','social-sites.js'):
        source=(root/'backend/static/lounge'/script).read_text(encoding='utf-8')
        required=set(re.findall(r'\b(?:el|byId)\(\s*[\'\"]([^\'\"]+)[\'\"]\s*\)',source))
        # This one button belongs to the existing reception settings entry.
        missing=required-ids-{'receptionOpen'}
        if missing:raise RuntimeError('roster DOM mismatch: '+','.join(sorted(missing)))
    for file in (root/'frontend/src/pages').glob('*.tsx'):
        if '<iframe' in file.read_text(encoding='utf-8'):raise RuntimeError('public iframe substituted for inner wall')
    print('PASS: manifest, hashes, Python syntax, inner wall and embedded roster DOM')


if __name__=='__main__':verify()
