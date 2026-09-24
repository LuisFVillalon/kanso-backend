import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Add it to kanso-backend/.env.\n"
        "Use the Session-mode pooler URL from: Supabase → Settings → Database → Connection string."
    )

# Supabase render fix: legacy postgres:// scheme → postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Warn early if the URL still points to the direct db. host (IPv6-only on newer projects).
if "@db." in DATABASE_URL:
    import warnings
    warnings.warn(
        "DATABASE_URL points to the direct Supabase host (db.*), which uses IPv6 only on "
        "newer projects and may be unreachable from some networks. "
        "Switch to the Session-mode pooler URL: Supabase → Settings → Database → Connection string.",
        stacklevel=2,
    )

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    # gssencmode=disable: psycopg2 2.9 added GSSAPI negotiation which sends a
    # probe packet that Supabase pgBouncer does not understand, causing an
    # immediate connection drop. Disabling GSSAPI skips that probe entirely.
    # (psycopg2 never uses server-side prepared statements, so it is already
    # compatible with transaction-mode pgBouncer on port 6543.)
    connect_args={"gssencmode": "disable", "connect_timeout": 10},
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_db_connection() -> None:
    """Probe the database and raise a descriptive RuntimeError on failure.

    Call this from the FastAPI startup event so connection problems surface
    immediately with a readable message rather than a raw psycopg2 trace.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        original = str(exc.orig) if exc.orig else str(exc)
        if "could not translate host name" in original or "Name or service not known" in original:
            raise RuntimeError(
                "Database host unreachable (DNS/IPv6 issue).\n"
                "Your DATABASE_URL likely points to the direct Supabase host (db.*), "
                "which resolves to IPv6 only and cannot be reached from this network.\n"
                "Fix: replace DATABASE_URL with the Session-mode pooler URL from "
                "Supabase → Settings → Database → Connection string.\n"
                f"Original error: {original}"
            ) from exc
        if "Connection refused" in original or "10061" in original:
            raise RuntimeError(
                "Database connection refused. Check that port 5432 is not blocked by a firewall.\n"
                f"Original error: {original}"
            ) from exc
        raise RuntimeError(f"Database connection failed: {original}") from exc
