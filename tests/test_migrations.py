"""Alembic history: one head, so `alembic upgrade head` on deploy is unambiguous; and data migrations."""

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from app.crud.notification_crud import get_preferences, update_preferences
from app.models.notification_model import NotificationPreferences
from app.models.profile_model import Profile

ROOT = Path(__file__).resolve().parent.parent


def test_migrations_have_a_single_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"Multiple Alembic heads {heads}; re-point the newest migration's down_revision."


def test_migration_revision_ids_are_unique():
    # Alembic only warns on a reused id and keeps one of the files, so the head check alone misses it.
    revisions = [_load_path(path).revision for path in (ROOT / "alembic" / "versions").glob("*.py")]
    duplicates = sorted({revision for revision in revisions if revisions.count(revision) > 1})
    assert not duplicates, f"Alembic revision ids used more than once {duplicates}; give the newest migration a new id."


def _load_migration(name: str):
    return _load_path(next((ROOT / "alembic" / "versions").glob(f"*_{name}.py")))


def _load_path(path: Path):
    name = path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mood_remind_off_carries_over_to_the_mood_notification_switches(db):
    kept, fresh, nudged, unset = (f"00000000-0000-0000-0000-00000000010{n}" for n in range(4))
    db.add_all([
        Profile(user_id=kept, name="Kept", mood_checkins={"remind": "off", "daily": True}),
        Profile(user_id=fresh, name="Fresh", mood_checkins={"remind": "off"}),
        Profile(user_id=nudged, name="Nudged", mood_checkins={"remind": "shutoff"}),
        Profile(user_id=unset, name="Unset"),
    ])
    update_preferences(
        db, kept, {"types": {"task_reminder": False, "mood_reminder": True}, "mood_reminder_minutes": 45}
    )
    db.flush()

    migration = _load_migration("carry_over_mood_remind_off")
    with Operations.context(MigrationContext.configure(db.connection())):
        migration.upgrade()
        migration.upgrade()
    db.expire_all()

    off = {"task_reminder": True, "mood_reminder": False, "mood_missed": False}
    assert get_preferences(db, kept).types == {**off, "task_reminder": False}
    assert get_preferences(db, kept).mood_reminder_minutes == 45
    assert get_preferences(db, fresh).types == off
    for user_id in (nudged, unset):
        assert db.query(NotificationPreferences).filter(NotificationPreferences.user_id == user_id).first() is None
