import os
from logging.config import fileConfig

from dotenv import load_dotenv
from sqlalchemy import create_engine, pool

from alembic import context

# Load .env
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

# Alembic Config
config = context.config

# Logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import Base and all models so autogenerate can see every table
from app.database.database import Base
from app.models import (  # ensure every model is visible to autogenerate
    archived_term_model,
    calendar_settings_model,
    canvas_connection_model,
    drawing_model,
    habit_log_model,
    habit_model,
    habit_tag_model,
    mood_checkin_model,
    note_model,
    note_session_model,
    note_tag_model,
    notification_model,
    profile_model,
    tag_model,
    task_model,
    task_tag_model,
    term_rollover_model,
)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # NullPool is required when connecting through a Transaction-mode pgBouncer
    # (port 6543). It ensures Alembic never holds a connection open between
    # migration steps, which would violate pgBouncer's transaction-level
    # connection multiplexing.
    connectable = create_engine(
        DATABASE_URL,
        poolclass=pool.NullPool,
        connect_args={"gssencmode": "disable", "connect_timeout": 10},
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
