"""Short actions from the reading receipt, independent of reflections and book contents."""


def with_finished_picture(description, node):
    from .reading_illustrations import entry
    picture = entry(node)
    if picture:
        return description + f'；为《{picture["title"]}》画了一幅读后插画'
    return description


def material_description(material):
    if material.get('kind') == 'bookshelf':
        # The frozen description already includes any illustration completed on this node.
        return material.get('description') or ''
    if material.get('kind') == 'reading':
        reading = material.get('reading') or {}
        rows = reading.get('messages') or []
        if rows:
            date = reading.get('active_date') or ''
            source = '收藏的对话' if reading.get('source_mode') == 'existing_bookmark' else '旧日对话'
            return f'回看了{date + " 的" if date else ""}{source}（{len(rows)} 条摘录）'
    return ''


def activity_description(experience):
    if not isinstance(experience, dict) or experience.get('activity_type') != 'browse_bookmarks':
        return ''
    return '；'.join(text for material in experience.get('materials') or [] if isinstance(material, dict)
                    and (text := material_description(material)))


def brief(snapshot):
    labels = {'essays': '随笔', 'bookmarks': '收藏'}
    lines = [f'{"细看" if item.get("depth") == "deep" else "翻看"}{labels[item["source"]]}《{item["title"]}》'
             for item in snapshot.get('items', ()) if item.get('source') in labels]
    if snapshot.get('unread_items'):
        lines.append(f'另有 {len(snapshot["unread_items"])} 项未读到')
    return '；'.join(lines)


def observed_shelf_sources(payload):
    """Return only public shelf kinds frozen for the current node."""
    snapshot = payload.get('bookshelf_snapshot') or {}
    sources = []
    for item in snapshot.get('items') or []:
        if isinstance(item, dict) and item.get('source') in {'bookmarks', 'essays'}:
            if item['source'] not in sources:
                sources.append(item['source'])
    if sources:
        return sources
    reading = payload.get('reading') or {}
    if reading.get('messages'):
        return ['bookmarks' if reading.get('source_mode') == 'existing_bookmark' else 'history']
    return []
