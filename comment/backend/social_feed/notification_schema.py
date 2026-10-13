"""Explicit, backup-gated expansion of existing interaction receipts to visitors."""
from .public_wall import PublicWallError


def visitor_inbox_ready(db):
    row=db.execute("SELECT sql FROM sqlite_master WHERE name='wall_notifications'").fetchone()
    return bool(row and "'visitor:*'" in row[0])


def migrate(wall, *, backup_ready=False):
    if backup_ready is not True:
        raise PublicWallError('social_notification_backup_required')
    with wall._connect() as db:
        if visitor_inbox_ready(db):return
        db.execute('BEGIN IMMEDIATE')
        # No table references this receipt table. Preserve every ID and read/announce fact.
        sql=db.execute("SELECT sql FROM sqlite_master WHERE name='wall_notifications'").fetchone()[0]
        replacement=sql.replace('wall_notifications','wall_notifications_expanded',1).replace(
            "CHECK (recipient IN ('aning','k'))", "CHECK (recipient IN ('aning','k') OR recipient GLOB 'visitor:*')")
        if replacement==sql or "recipient IN ('aning','k'))" in replacement:
            raise PublicWallError('social_notification_schema_unknown')
        db.execute(replacement)
        db.execute('INSERT INTO wall_notifications_expanded SELECT * FROM wall_notifications')
        db.execute('DROP TABLE wall_notifications')
        db.execute('ALTER TABLE wall_notifications_expanded RENAME TO wall_notifications')
        db.execute('CREATE INDEX wall_notifications_recipient ON wall_notifications(recipient,read_at,announced_at,created_at)')
