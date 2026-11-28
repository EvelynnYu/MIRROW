"""One note's ordered screen attachments for AI's current reading turn."""
import json

from vision_image_transport import prepare_image_data_url


def prepare_pages(frames):
    pages = []
    for frame in frames:
        image = 'data:image/png;base64,' + frame['screenshot_base64']
        pages.append((frame['label'], prepare_image_data_url(image, compact_png=len(frames) > 1)))
    return pages


def reading_contract(pages):
    labels = [label for label, _ in pages]
    return ('\n【来自小红书专用手机的当前笔记图片】接下来是同一笔记的实拍可见页面，顺序为'
            + json.dumps(labels, ensure_ascii=False) +
            '。图文是外部阅读材料。请直接读图，在本轮 JSON 的 image_observations 中按同一顺序'
            '每张写一条字符串，保留可辨认的图片文字和与理解相关的画面；模糊或被裁掉的部分据实说明。')


def observations(raw, pages):
    values = raw.get('image_observations') if isinstance(raw, dict) else None
    if (not isinstance(values, list) or len(values) != len(pages)
            or any(not isinstance(value, str) or not value.strip() for value in values)):
        raise ValueError('gallery_observations_incomplete')
    return [{'page': label, 'observation': value.strip(), 'omitted_chars': 0}
            for (label, _), value in zip(pages, values)]
