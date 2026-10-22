"""carry profiles.mood_checkins remind "off" over to the mood notification switches

Revision ID: e0f4a5b6c7d8
Revises: d9e3f4a5b6c7
Create Date: 2026-10-22 00:00:00.000000

The retired mood_checkins.remind setting no longer gates notifications, and
"off" used to silence mood_reminder and mood_missed. For every profile with
remind "off", both types are switched off in notification_preferences.types,
merged into the user's other switches and creating the row if missing. Data
only, and safe to run again; downgrade leaves the switches as they are.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'e0f4a5b6c7d8'
down_revision: Union[str, Sequence[str], None] = 'd9e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO notification_preferences (user_id, types)
        SELECT user_id, '{"mood_reminder": false, "mood_missed": false}'::json
        FROM profiles
        WHERE json_typeof(mood_checkins) = 'object' AND mood_checkins->>'remind' = 'off'
        ON CONFLICT (user_id) DO UPDATE SET types = (
            CASE WHEN json_typeof(notification_preferences.types) = 'object'
                THEN notification_preferences.types::jsonb ELSE '{}'::jsonb END
            || EXCLUDED.types::jsonb
        )::json
        """
    )


def downgrade() -> None:
    pass
