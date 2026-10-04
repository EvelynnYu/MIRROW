"""Public-wall admission for a human/AI pair issued by the reception service.

The visitor repository owns Key validity and visitor kind. The public wall
stores only stable visitor IDs and the confirmed family relation, never Keys.
"""

from .public_wall import PublicWallError, get_public_wall


def has_active_key(runtime, visitor_id: str) -> bool:
    with runtime.database.connection() as db:
        return bool(db.execute('SELECT 1 FROM visitor_keys WHERE visitor_id=? AND revoked_at IS NULL LIMIT 1',
                               (visitor_id,)).fetchone())


def social_visitor(runtime, visitor_id: str, *, require_link: bool = True):
    visitor = runtime.visitor_service.effective_visitor(visitor_id)
    if visitor_id in get_public_wall().removed_contacts():
        raise PublicWallError('identity_unavailable')
    if visitor.status not in {'active', 'suspended'}:
        raise PublicWallError('identity_unavailable')
    if visitor.visitor_kind not in {'human', 'external_ai'}:
        raise PublicWallError('identity_kind_unavailable')
    if require_link and visitor.visitor_kind == 'external_ai':
        human_id = get_public_wall().household_human(visitor_id)
        if not human_id:
            raise PublicWallError('human_binding_required')
        try:
            human = runtime.visitor_service.effective_visitor(human_id)
        except (KeyError, ValueError):
            raise PublicWallError('human_binding_required') from None
        if (human.visitor_kind != 'human' or human.status not in {'active', 'suspended'}
                or not has_active_key(runtime, human_id)):
            raise PublicWallError('human_binding_required')
    return visitor


def registered_social_visitor(runtime, visitor_id: str):
    """Admission is a registered identity, not just possession of an unused Key."""
    visitor = social_visitor(runtime, visitor_id)
    if not get_public_wall().registered_name(visitor_id).strip():
        raise PublicWallError('identity_registration_required')
    return visitor


def verified_household(runtime, human_key: str, ai_keys: list[str]) -> tuple[tuple[str, str], list[tuple[str, str]]]:
    """Verify one human and each distinct AI with their current inbound Keys."""
    human = runtime.keys.authenticate_identity(human_key)
    ais = [runtime.keys.authenticate_identity(key) for key in ai_keys]
    if not human or not ais or any(ai is None for ai in ais):
        raise PublicWallError('invalid_household_keys')
    ai_ids = [ai[1] for ai in ais]
    if human[1] in ai_ids or len(set(ai_ids)) != len(ai_ids):
        raise PublicWallError('invalid_household_keys')
    try:
        human_record = social_visitor(runtime, human[1], require_link=False)
        ai_records = [social_visitor(runtime, ai_id, require_link=False) for ai_id in ai_ids]
    except (KeyError, ValueError, PublicWallError):
        raise PublicWallError('invalid_household_keys') from None
    if human_record.visitor_kind != 'human' or any(record.visitor_kind != 'external_ai' for record in ai_records):
        raise PublicWallError('invalid_household_keys')
    return human, ais


def verified_pair(runtime, human_key: str, ai_key: str) -> tuple[tuple[str, str], tuple[str, str]]:
    human, ais = verified_household(runtime, human_key, [ai_key])
    return human, ais[0]
