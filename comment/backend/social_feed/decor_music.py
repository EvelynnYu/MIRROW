"""Read cached ear/playlist metadata, never start or proxy a music player."""
from .decor_models import MusicReference


def music_choices():
    from music_system.service import get_service
    store = get_service().store
    source = list(store.search_shared_songs('', 100))
    for binding in store.bindings()[:30]:
        if binding['subject'] in {'aning','k','shared'}:
            source.extend((store.material('playlist:' + binding['id']) or {}).get('songs',[])[:100])
    choices = {}
    for row in source:
        sid = str(row.get('id') or row.get('song_id') or '')
        if sid.isascii() and sid.isdigit() and len(sid) <= 20:
            choices[sid] = MusicReference(id=sid,title=str(row.get('name') or row.get('title') or '未命名歌曲')[:200],
                                         artist=str(row.get('artist') or '')[:200]).model_dump()
    return list(choices.values())[:300]
