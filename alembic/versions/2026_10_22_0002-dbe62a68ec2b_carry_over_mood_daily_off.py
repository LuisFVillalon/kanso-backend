"""carry profiles.mood_checkins daily false over to the mood reminder switch

Revision ID: dbe62a68ec2b
Revises: f1a5b6c7d8e9
Create Date: 2026-10-22 00:02:00.000000

The mood_reminder notification switch now turns daily mood check-ins on and
off, and the retired mood_checkins.daily setting is no longer read. For every
profile with daily false, mood_reminder is switched off in
notification_preferences.types, merged into the user's other switches and
creating the row if missing. Data only, and safe to run again; downgrade
leaves the switches as they are.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'dbe62a68ec2b'
down_revision: Union[str, Sequence[str], None] = 'f1a5b6c7d8e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO notification_preferences (user_id, types)
        SELECT user_id, '{"mood_reminder": false}'::json
        FROM profiles
        WHERE json_typeof(mood_checkins) = 'object' AND mood_checkins->>'daily' = 'false'
        ON CONFLICT (user_id) DO UPDATE SET types = (
            CASE WHEN json_typeof(notification_preferences.types) = 'object'
                THEN notification_preferences.types::jsonb ELSE '{}'::jsonb END
            || EXCLUDED.types::jsonb
        )::json
        """
    )


def downgrade() -> None:
    pass
