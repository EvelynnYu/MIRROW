"""Four-zone recipes for hosts supplying their own authoritative materials.

All published recipe names can use this explicit port. Absent integrations
remain absent; this path never discovers a neighbouring production service.
"""
from copy import deepcopy
import json
from .assembly import assemble_messages


def build_materials(recipe, *, persona='', history=(), materials=None, turn=()):
    from .builder import ContextResult
    from .recipes import get_recipe
    if get_recipe(recipe) is None:
        raise ValueError('unknown_public_recipe')
    result = ContextResult()
    result.system_content = persona
    result.formatted_messages = assemble_messages(
        prefix=[{'role': 'system', 'content': persona}] if persona else [], history=history)
    result.context_messages = [{'role': 'system', 'name': 'mirrow_sources', 'content': json.dumps(
        {'recipe': recipe, 'sources': deepcopy(materials or {})}, ensure_ascii=False)}]
    result.tail_messages = deepcopy(list(turn))
    result.sections = deepcopy(materials or {})
    result.stats = {'source_names': list((materials or {}).keys()), 'history_messages': len(list(history))}
    return result
