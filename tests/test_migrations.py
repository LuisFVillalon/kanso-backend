"""Alembic history: one head, so `alembic upgrade head` on deploy is unambiguous."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parent.parent


def test_migrations_have_a_single_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"Multiple Alembic heads {heads}; re-point the newest migration's down_revision."
