"""Conservative source-release gate. Reports locations/categories, never secrets."""
from pathlib import Path
import hashlib
import json
import re
import sys
import subprocess
import shutil

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE = {'.git','node_modules','data','dist','build','.gradle','.venv','__pycache__','.pytest_cache'}
TEXT = {'.py','.ts','.tsx','.css','.md','.txt','.json','.html','.xml','.gradle','.properties','.bat','.yml','.yaml','.ini','.toml','.sh'}
PATTERNS = {
    'private-key':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'provider-key':re.compile(r'\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AIza[A-Za-z0-9_-]{30,}|xox[baprs]-[A-Za-z0-9-]{20,})'),
    'mac-address':re.compile(r'\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b'),
    'personal-path':re.compile(r'(?:[A-Z]:[\\/](?:Users|MIRROW)[\\/]|/ho' r'me/[^/]+/|/Us' r'ers/[^/]+/)',re.I),
    'literal-secret':re.compile(r'''(?:access_token|api_key|password|client_secret|cookie|ssid)\s*[:=]\s*["'][A-Za-z0-9_./+=-]{20,}["']''',re.I),
}

def files():
    for path in sorted(ROOT.rglob('*')):
        rel=path.relative_to(ROOT)
        if any(part in EXCLUDE for part in rel.parts) or not path.is_file():continue
        if rel.name in {'RELEASE_MANIFEST.json'}:continue
        yield path

def audit(strict_tree=False):
    hits=[]; manifest={}
    for path in files():
        rel=path.relative_to(ROOT).as_posix()
        for label,pattern in PATTERNS.items():
            if pattern.search(rel):hits.append((rel,0,'filename-'+label))
        data=path.read_bytes();manifest[rel]=hashlib.sha256(data).hexdigest()
        if path.name.startswith('.env') and path.name!='.env.example':hits.append((rel,0,'environment-file'))
        if path.suffix.lower() in {'.sqlite','.db','.apk','.aab','.jks','.keystore','.mp3','.wav','.log'}:hits.append((rel,0,'runtime-artifact'))
        if path.suffix.lower() in {'.png','.jpg','.jpeg','.webp','.gif'}:
            from PIL import Image
            with Image.open(path) as im:
                if im.getexif() or any(k.lower() in {'exif','xmp','comment','description','software','author','text','xml:com.adobe.xmp'} for k in im.info):hits.append((rel,0,'image-metadata'))
            continue
        if rel=='android/gradle/wrapper/gradle-wrapper.jar':
            if manifest[rel]!='7d3a4ac4de1c32b59bc6a4eb8ecb8e612ccd0cf1ae1e99f66902da64df296172':hits.append((rel,0,'wrapper-hash-mismatch'))
            continue
        try:text=data.decode('utf-8')
        except UnicodeDecodeError: hits.append((rel,0,'unexpected-binary'));continue
        for number,line in enumerate(text.splitlines(),1):
            for label,pattern in PATTERNS.items():
                if pattern.search(line):hits.append((rel,number,label))
            for ip in re.findall(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])',line):
                if ip not in {'127.0.0.1','0.0.0.0'} and all(int(v)<=255 for v in ip.split('.')):hits.append((rel,number,'non-example-ip'))
    if strict_tree:
        for path in ROOT.rglob('*'):
            rel=path.relative_to(ROOT)
            if '.git' in rel.parts or not path.is_file():continue
            if any(part in EXCLUDE for part in rel.parts):hits.append((rel.as_posix(),0,'excluded-runtime-file'))
    repo=subprocess.run(['git','rev-parse','--show-prefix'],cwd=ROOT,capture_output=True) if shutil.which('git') else None
    if repo is not None and repo.returncode==0:
        prefix=repo.stdout.decode('utf-8').strip()
        result=subprocess.run(['git','ls-files','-z','--','.'],cwd=ROOT,capture_output=True,check=True)
        for raw in result.stdout.split(b'\0'):
            if not raw:continue
            rel=raw.decode('utf-8')
            if rel not in manifest and rel!='RELEASE_MANIFEST.json':hits.append((rel,0,'tracked-file-outside-release'))
        staged=subprocess.run(['git','diff','--cached','--name-only','--relative','-z','--','.'],cwd=ROOT,capture_output=True,check=True)
        for raw in staged.stdout.split(b'\0'):
            if not raw:continue
            rel=raw.decode('utf-8')
            if rel not in manifest and rel!='RELEASE_MANIFEST.json':continue
            blob=subprocess.run(['git','show',':'+prefix+rel],cwd=ROOT,capture_output=True)
            if blob.returncode==0 and (not (ROOT/rel).is_file() or blob.stdout!=(ROOT/rel).read_bytes()):hits.append((rel,0,'staged-content-differs'))
    return hits,manifest

if __name__=='__main__':
    hits,manifest=audit(strict_tree='--strict-tree' in sys.argv)
    for rel,line,label in hits:print(f'{rel}:{line}: {label}')
    if hits:sys.exit(1)
    if '--manifest' in sys.argv:
        (ROOT/'RELEASE_MANIFEST.json').write_text(json.dumps({'edition':'2026-10-03','files':manifest},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'PASS: {len(manifest)} publishable files; no matches in the release gate.')
