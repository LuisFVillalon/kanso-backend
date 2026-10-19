import logging
import os
import warnings
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database.database import check_db_connection
from app.routers import (
    calendar_router,
    debrief_router,
    demo_router,
    drawing_router,
    habits_router,
    notes_router,
    profile_router,
    tags_router,
    tasks_router,
    trash_router,
    user_router,
)

# Comma-separated list of allowed frontend origins, e.g.
# "http://localhost:3000,https://kanso-web-app.vercel.app"
# New deployment targets (a staging URL, a custom domain) are added via env
# var, not a source change.
_DEFAULT_ORIGINS = "http://localhost:3000,https://kanso-web-app.vercel.app"
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", _DEFAULT_ORIGINS).split(",")
    if origin.strip()
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Log only the host, never credentials.
    logging.getLogger("uvicorn.error").info(
        "Database host: %s", urlparse(os.getenv("DATABASE_URL", "")).hostname
    )

    if not os.getenv("SUPABASE_URL"):
        warnings.warn(
            "SUPABASE_URL is not set — JWTs can't be verified, so every authenticated "
            "endpoint will return 401.",
            stacklevel=1,
        )

    try:
        check_db_connection()
    except Exception as exc:
        warnings.warn(f"Startup DB check failed (app will still start): {exc}", stacklevel=1)

    yield


app = FastAPI(
    title="kanso API",
    description="Tasks, notes, habits and daily-debrief API behind the kanso web app.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tags_router.router)
app.include_router(tasks_router.router)
app.include_router(notes_router.router)
app.include_router(user_router.router)
app.include_router(calendar_router.router)
app.include_router(habits_router.router)
app.include_router(profile_router.router)
app.include_router(drawing_router.router)
app.include_router(debrief_router.router)
app.include_router(demo_router.router)
app.include_router(trash_router.router)


@app.get("/", include_in_schema=False)
def read_root():
    return {"message": "kanso API. See /docs for the interactive reference."}


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok"}
