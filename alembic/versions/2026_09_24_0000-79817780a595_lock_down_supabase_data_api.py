"""lock down the Supabase Data API on every public table

Revision ID: 79817780a595
Revises: u1v2w3x4y5z6
Create Date: 2026-09-24 00:00:00.000000

Supabase auto-exposes every table in the `public` schema through its REST
Data API, reachable by anyone holding the project's publishable (anon) key —
which ships in the frontend bundle. Supabase is only used for auth here, and
all data access goes through this backend, so the anon/authenticated roles
should have no access to these tables at all.

1. Enable Row Level Security on every public table. No policies are added,
   so anon/authenticated are denied everything. The backend connects as the
   table owner (`postgres`), which bypasses RLS, so it is unaffected.
2. Revoke the default grants Supabase gives anon/authenticated, as a second
   layer should RLS ever be disabled on a table.
3. Change the default privileges so tables created by future migrations
   aren't granted to anon/authenticated either.

A table not owned by the migrating role can't be altered; instead of failing
the whole deploy, it's reported with a WARNING so it can be fixed by hand.
"""

from typing import Sequence, Union

from alembic import op

revision: str = '79817780a595'
down_revision: Union[str, Sequence[str], None] = 'u1v2w3x4y5z6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        DO $$
        DECLARE
            t record;
            api_roles text;
        BEGIN
            -- anon/authenticated only exist on Supabase; skip cleanly elsewhere.
            SELECT string_agg(quote_ident(rolname), ', ') INTO api_roles
            FROM pg_roles WHERE rolname IN ('anon', 'authenticated');

            FOR t IN
                SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'public'
            LOOP
                IF t.tableowner = current_user THEN
                    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.tablename);
                ELSE
                    RAISE WARNING 'public.% is owned by %, not %; enable RLS on it manually',
                        t.tablename, t.tableowner, current_user;
                END IF;
            END LOOP;

            IF api_roles IS NOT NULL THEN
                EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %s', api_roles);
                EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %s', api_roles);
                EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM %s', api_roles);
                EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %s', api_roles);
            END IF;
        END
        $$;
    """)


def downgrade() -> None:
    # Intentionally a no-op: reverting would re-expose every user's data
    # through the public Data API.
    pass
