"""Audit the public module only; report locations/categories, never matched values."""
from pathlib import Path
import argparse
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[1]
ASSETS = {
    'GreatVibes-Regular.ttf': '8d509802186f1b51572531ecf313e8098f9a5bfdfaca93f0c9b34467f9982d15',
    'MaShanZheng-Regular.ttf': '6d2546bb189c732a8ca29af9e22457b152387d158aa459e4ac2ce1e51788b7fb',
    'kenney-card-000.png': 'd84e0a1f1ef9e0d7531d19baf83aef33734819aa9b44a649922240561ac8affc',
    'kenney-card-006.png': 'b3a5bf1614463dc4c9d4ecc9847d05279fb1cd44d09dfb7871d863b05af5af6f',
    'kenney-card-014.png': '52cecc8aaa69849ff6e7eec29efdc9e1ca39290a31cd217d16c02d659b247ef2',
    'kenney-frame-000.png': '881d46e36ae1f6a24082cde979d594ef3945354c798ac682c315a295bb26e5da',
    'kenney-frame-006.png': '9b57a0077437a8f0a8bc6789a6b775d4cd81173c3464469995ea420d95e951aa',
    'kenney-frame-014.png': '91c4837d2dceea46097a38fceb4e352df9140fa7cde05ec4e4385e64fadd19be',
}
EXCLUDE = {'.git', 'node_modules', '.venv', '__pycache__', '.pytest_cache', 'dist', 'build'}
PATTERNS = {
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'provider-token': re.compile(r'\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AIza[A-Za-z0-9_-]{30,})'),
    'personal-path': re.compile(r'(?:[A-Z]:[\\/](?:Users|MIRROW)[\\/]|/ho' r'me/[^/]+/|/Us' r'ers/[^/]+/)', re.I),
    'literal-secret': re.compile(r'''(?:api_key|access_token|client_secret|password|cookie)\s*[:=]\s*["'][A-Za-z0-9_./+=-]{20,}["']''', re.I),
}


def audit():
    hits, files = [], []
    for path in sorted(ROOT.rglob('*')):
        relative = path.relative_to(ROOT)
        if not path.is_file() or '.git' in relative.parts:
            continue
        if any(part in EXCLUDE for part in relative.parts):
            hits.append((relative.as_posix(), 0, 'runtime-directory'))
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            hits.append((relative.as_posix(), 0, 'linked-file'))
            continue
        if relative.as_posix() == 'MANIFEST.json':
            continue
        raw = path.read_bytes()
        files.append({'path': relative.as_posix(), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
        if path.suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.apk', '.aab', '.log', '.mp3', '.wav', '.jks', '.keystore'} or path.name == '.env':
            hits.append((relative.as_posix(), 0, 'runtime-or-secret-file'))
        if path.suffix.lower() in {'.png', '.ttf'}:
            # A path allowlist alone cannot distinguish an avatar from a licensed asset.
            if (relative.parent.as_posix() != 'backend/social_feed/decor_presets'
                    or ASSETS.get(path.name) != hashlib.sha256(raw).hexdigest()):
                hits.append((relative.as_posix(), 0, 'unexpected-binary'))
            continue
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            hits.append((relative.as_posix(), 0, 'unexpected-binary'))
            continue
        if path.suffix == '.py':
            compile(text, relative.as_posix(), 'exec')
        for number, line in enumerate(text.splitlines(), 1):
            for label, pattern in PATTERNS.items():
                if pattern.search(line):
                    hits.append((relative.as_posix(), number, label))
    return hits, files


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', action='store_true')
    args = parser.parse_args()
    hits, files = audit()
    for name, line, category in hits:
        print(f'{name}:{line}: {category}')
    if hits:
        raise SystemExit(1)
    if args.manifest:
        manifest = {'package': 'MIRROW-Comment-open', 'kind': 'source-integration-kit',
                    'ui_contract': 'active-inner-wall-and-embedded-rosters', 'files': files}
        (ROOT / 'MANIFEST.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'PASS: {len(files)} module files; release gate clean')
