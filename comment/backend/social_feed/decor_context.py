"""Bounded factual shelf projection; pictures are references, not visual evidence."""
from .decor_models import HomeDesign


def shelf_facts(state):
    home = HomeDesign.model_validate(state.get('home') or {})
    return {'status':'available','name':home.theme.shelf_name,
            'exhibits':[{'name':e.name,'description':e.description,
                         'human_note':e.human_note,'ai_note':e.ai_note,
                         'has_display_image':bool(e.image)} for e in home.exhibits],
            'image_evidence':'仅提供物品文字资料和有无展示图；此处没有读取图片像素。'}
