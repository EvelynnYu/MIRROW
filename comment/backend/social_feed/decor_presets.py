"""Small local catalog. Presets import as owned media, not arbitrary CSS or URLs."""
import json
import base64
import re
from pathlib import Path
from .decor_store import DecorError

DIRECTORY = Path(__file__).with_name('decor_presets')
ID = re.compile(r'^[A-Za-z0-9_-]{1,60}$')
CATEGORIES = {'frame','background','card','exhibit'}
FONTS = {'calligraphy':'MaShanZheng-Regular.ttf', 'flower':'GreatVibes-Regular.ttf'}


def font_path(identifier):
    """Only bundled public fonts; no arbitrary filenames or filesystem paths."""
    name = FONTS.get(identifier)
    if name is None:
        raise DecorError('preset_not_found')
    path = DIRECTORY/name
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 10*1024*1024:
        raise DecorError('preset_not_found')
    return path


def catalog(directory=None):
    root = Path(directory or DIRECTORY).resolve()
    try:
        path = root/'catalog.json'
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError()
        data = json.loads(path.read_text(encoding='utf-8'))
        assets, seen = [], set()
        for item in data.get('assets',[])[:100]:
            file = item.get('file','')
            identifier = item.get('id','')
            if (not ID.fullmatch(identifier) or identifier in seen or item.get('category') not in CATEGORIES
                    or not isinstance(file,str) or '/' in file or '\\' in file or ':' in file):
                raise ValueError()
            candidate = root/file
            if (candidate.is_symlink() or candidate.resolve().parent != root or
                    candidate.suffix.lower() not in {'.png','.jpg','.jpeg','.webp','.gif'} or
                    not candidate.is_file() or candidate.stat().st_size > 10*1024*1024):
                raise ValueError()
            name = item.get('name','')
            if not isinstance(name,str) or not 1 <= len(name) <= 60:
                raise ValueError()
            seen.add(identifier)
            asset = {'id':identifier,'name':name,'category':item['category'],'file':file}
            # Tiny bundled PNG swatches only. Never embed large GIFs in the catalog.
            if candidate.suffix.lower()=='.png' and candidate.stat().st_size <= 4096:
                raw = candidate.read_bytes()
                if raw.startswith(b'\x89PNG\r\n\x1a\n'):
                    asset['preview'] = 'data:image/png;base64,'+base64.b64encode(raw).decode('ascii')
            assets.append(asset)
        from .decor_models import Theme, PersonalDesign
        themes = [{'id':Theme(preset=t['id'],accent=t['accent']).preset,'name':str(t['name'])[:60], 'accent':t['accent']} for t in data['themes']]
        frames = [{'id':PersonalDesign(frame=f['id']).frame,'name':str(f['name'])[:60]} for f in data['frames']]
        return {'themes':themes,'frames':frames,'assets':assets}
    except (OSError,ValueError,KeyError,TypeError,AttributeError):
        raise DecorError('preset_catalog_invalid') from None


def import_preset(store, actor, identifier):
    item = next((a for a in catalog()['assets'] if a['id']==identifier),None)
    if item is None:
        raise DecorError('preset_not_found')
    from .decor_media import save_media
    return save_media(store,actor,(DIRECTORY/item['file']).read_bytes())
