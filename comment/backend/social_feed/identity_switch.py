"""Household membership for real website identity switches, not profile delegation."""
from .household_identity import registered_social_visitor
from .public_wall import PublicWallError


def household_identities(runtime, wall, actor):
    if not actor.startswith('visitor:'):
        return [actor]
    visitor_id = actor[8:]
    current = registered_social_visitor(runtime, visitor_id)
    human_id = visitor_id if current.visitor_kind == 'human' else wall.household_human(visitor_id)
    candidates = [human_id, *[row['ai_visitor_id'] for row in wall.household_links()
                            if row['human_visitor_id'] == human_id]]
    result = []
    for identifier in dict.fromkeys(candidates):
        if not identifier:
            continue
        try:
            registered_social_visitor(runtime, identifier)
        except (PublicWallError, KeyError, ValueError):
            continue
        result.append('visitor:' + identifier)
    return result
