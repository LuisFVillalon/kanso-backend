# kanso-backend

The API behind [kanso](https://kanso-web-app.vercel.app), a productivity app for tasks, notes, habits and a daily debrief. It's built with FastAPI, SQLAlchemy 2, Alembic and Postgres (hosted on Supabase), and deployed on Fly.io.

| Repository | Role |
|---|---|
| [kanso-frontend](https://github.com/LuisFVillalon/kanso-frontend) | Next.js web app (start here for the full architecture) |
| **kanso-backend** (this repo) | Data API: tasks, tags, notes, habits, mood check-ins, profile, daily debrief, notifications, Canvas LMS connection, demo sandboxes |
| [kanso-ai](https://github.com/LuisFVillalon/kanso-ai) | LLM-backed learning-resource recommendations |

Interactive API reference: `/docs` on any running instance.

## Highlights

- **Auth without owning passwords.** Users sign in with Supabase Auth; every endpoint verifies the Supabase JWT (ES256/RS256 via the project's JWKS, or HS256 with the project secret) and takes the user id from the token's `sub` claim. No endpoint accepts a user id from the client.
- **Tenant isolation.** Every row carries a `user_id` and every query filters on it. Single-row lookups all go through one helper, `crud/base.py:get_owned`, so the ownership check can't be skipped in a new module.
- **Database closed to direct access.** Supabase exposes `public` tables through an auto-generated REST API. A migration enables Row Level Security and revokes the `anon`/`authenticated` grants on every table (including future ones), so the public Supabase key can't reach the data. The backend connects as the table owner and is unaffected.
- **Per-visitor demo sandboxes.** `POST /demo/seed` accepts only Supabase *anonymous* users and fills their sandbox with sample data dated relative to the visitor's local day. A background task deletes sandboxes older than a day, both their rows and their auth users.
- **Timezone-correct logic.** The server runs in UTC. Habit streaks, "due today" and the daily debrief take the caller's `local_date`/`local_time` rather than the server clock, and note time totals take the caller's UTC offset.
- **A rule-based daily debrief.** `GET /daily-debrief` combines overdue and due-today tasks, today's completions, habit status and workload capacity (committed vs. available minutes, respecting rest days) into a prioritized "focus next" list. No LLM is involved.
- **Third-party tokens encrypted at rest.** A user's Canvas access token is stored only as AES-256-GCM ciphertext, with the key in a Fly secret rather than the database, and is never sent back to the browser.
- **Input bounds and rate limits.** Request schemas cap field lengths and list sizes; demo seeding is rate-limited per user.

## Data model

```mermaid
erDiagram
    TASKS }o--o{ TAGS : task_tags
    NOTES }o--o{ TAGS : note_tags
    HABITS }o--o{ TAGS : habit_tags
    HABITS ||--o{ HABIT_LOGS : "one per logged day"
    NOTES ||--o{ NOTE_SESSIONS : "editing time"
    TASKS ||--o{ TASKS : "parent / subtasks"
```

Every table also carries `user_id`. The single-row-per-user tables are `profiles` (display name and synced UI preferences), `calendar_settings` (the term tracker), `drawings` (the doodle canvas), `notification_preferences` and `canvas_connections` (the encrypted Canvas token); `notification_states` holds read and dismissed state per notification, and `mood_checkins` holds one mood check-in per day, week or month. Join tables and child rows cascade on delete, so removing an account is one pass over `crud/account_crud.py:USER_OWNED_MODELS`.

## Project structure

```
app/
  main.py        # App, CORS, router registration, /health
  core/          # auth.py (JWT verification), supabase_admin.py, rate_limit.py, http_utils.py,
                 # crypto.py (token encryption), canvas_client.py (Canvas API calls)
  routers/       # One per domain; thin handlers that depend on get_current_user + get_db
  crud/          # Queries and business logic (streaks, debrief, demo seed, account deletion)
  models/        # SQLAlchemy models
  schemas/       # Pydantic request/response models; limits.py holds the input bounds
  database/      # Engine and session setup (Supabase pooler / pgBouncer-safe)
tests/           # pytest suite (needs a throwaway Postgres, see below)
alembic/         # Migrations (date-prefixed); run automatically on every deploy
```

Routes use verb-style paths (`/get-tasks`, `/create-task`, `/update-task/{id}`, `/del-task/{id}`), except the Canvas ones, which are one resource under `/canvas` with HTTP methods. Each domain has one file per layer: `routers/` → `crud/` → `models/` + `schemas/`.

## Endpoints

All endpoints require `Authorization: Bearer <supabase-jwt>`.

| Area | Endpoints |
|---|---|
| Tasks | `GET /get-tasks` · `POST /create-task` · `PUT /update-task/{id}` · `PATCH /update-task-status/{id}` · `PATCH /archive-task/{id}` · `PATCH /unarchive-task/{id}` · `DELETE /del-task/{id}` · `PATCH /restore-task/{id}` · `DELETE /purge-task/{id}` |
| Tags | `GET /get-tags` · `POST /create-tags` · `PUT /update-tag/{id}` · `PATCH /archive-tag/{id}` · `PATCH /unarchive-tag/{id}` · `DELETE /del-tag/{id}` · `PATCH /restore-tag/{id}` · `DELETE /purge-tag/{id}` |
| Notes | `GET /get-notes` · `POST /create-note` · `PUT /update-note/{id}` · `PATCH /archive-note/{id}` · `PATCH /unarchive-note/{id}` · `DELETE /del-note/{id}` · `PATCH /restore-note/{id}` · `DELETE /purge-note/{id}` |
| Note time | `POST /note-session/start` · `PUT /note-session/end/{id}` · `GET /note-session/total/{note_id}` · `GET /note-session/totals` · `GET /note-session/totals-range` · `POST /note-session/reap` |
| Habits | `GET /get-habits` · `POST /create-habit` · `PUT /update-habit/{id}` · `PATCH /archive-habit/{id}` · `PATCH /unarchive-habit/{id}` · `DELETE /del-habit/{id}` · `PATCH /restore-habit/{id}` · `DELETE /purge-habit/{id}` · `PATCH /toggle-habit/{id}` · `PATCH /toggle-habit-date/{id}` · `POST /verify-streaks` · `GET /habit-history/{id}` |
| Trash | `GET /get-trash` |
| Mood check-ins | `GET /get-mood-checkins?start_date=&end_date=&kinds=` · `PUT /save-mood-checkin` · `DELETE /del-mood-checkin/{id}` · `GET /get-mood-tags` |
| Daily debrief | `GET /daily-debrief?local_date=&local_time=` |
| Calendar activity | `GET /get-calendar-activity?start_date=&end_date=&tz=` |
| Notifications | `GET /get-notifications?tz=` · `POST /mark-notifications-read` · `POST /mark-all-notifications-read?tz=` · `POST /dismiss-notifications` · `GET /get-notification-preferences` · `PATCH /update-notification-preferences` |
| Profile & settings | `GET /get-profile` · `POST /save-profile` · `GET /get-calendar-settings` · `PATCH /update-calendar-settings` |
| Doodle canvas | `GET /get-drawing` · `POST /save-drawing` · `DELETE /delete-drawing` |
| Canvas | `GET /canvas/connection` · `PUT /canvas/connection` · `POST /canvas/connection/test` · `PATCH /canvas/connection` · `DELETE /canvas/connection` (not for demo sandboxes) |
| Account | `POST /update-password` (email accounts only) · `DELETE /delete-account` |
| Demo | `POST /demo/seed?local_date=` (anonymous users only) |

**Archiving.** Tasks, notes, tags and habits carry an `archived_at` timestamp (`null` while active). `GET /get-tasks`, `/get-notes`, `/get-tags` and `/get-habits` return active rows only; pass `?archived=true` to list the archived ones instead. Archiving a task also archives its subtasks, and restoring it restores the subtasks archived with it plus any archived parents above it. Archiving a tag only hides it: tasks, notes and habits keep it in their `tags` lists. Creating or updating a task or note with an archived tag it didn't already carry restores that tag. Archived tasks, notes and habits are left out of the daily debrief and note-time totals, and archived habits are skipped by `POST /verify-streaks`.

**Trash.** The `del-*` endpoints move a task, note, tag or habit to the trash by setting its `deleted_at` instead of removing the row, and work on archived rows too. A deleted item is gone from every list, lookup, count and the debrief, but its tag links, habit logs and note sessions are kept: `PATCH /restore-{kind}/{id}` brings it back intact (into the archive, if it was archived). Deleting a task also deletes its subtasks; restoring it restores the subtasks deleted with it plus any deleted parents above it. A deleted tag is hidden from the `tags` lists of the items carrying it until restored, and naming it on a task, note or habit restores it. `GET /get-trash` lists deleted items newest first, each with its `purge_at`, 30 days after deletion. `DELETE /purge-{kind}/{id}` deletes one for good right away. Items deleted more than 30 days ago are treated as gone everywhere, the trash and restore included, and are permanently deleted, for every user at once, when the app starts and whenever anyone opens the trash, restores or purges from it, or deletes something (`crud/trash_crud.py`), so no scheduler is needed.

**Calendar activity.** `GET /get-calendar-activity` returns what the user planned and did across a range of their local days (up to 366): tasks due in the range, tasks completed in it (by `completed_date`, set when a task is marked complete and cleared when it's reopened), closed note editing sessions that overlap it, and habit completions. It backs the calendar's week view and its weekly, monthly and yearly reports. Completion and session timestamps come back in UTC with a `Z` suffix; `tz`, the caller's IANA time zone (e.g. `America/Los_Angeles`, from `Intl.DateTimeFormat().resolvedOptions().timeZone`), matches them against the caller's local days, correctly across DST changes; an unknown zone is a 422. Archived and deleted tasks, notes and habits are left out, and tasks completed before completion times were recorded have no `completed_date`, so they don't appear as completions.

**Notifications.** In-app alerts, computed on demand from tasks, habits and the profile each time `GET /get-notifications?tz=` is called, so there is no scheduler. `tz` is the caller's IANA time zone, as for calendar activity; due times, the profile's "Day starts at" (`day_start_time`, default 08:00) and "Call it a day" (`shutoff_time`, default 22:00) and quiet hours are local times in it, and every alert time follows DST. Five types exist:

| Type | Fires | Lapses |
|---|---|---|
| `due_today` | At the day start, listing the open tasks due today | At midnight |
| `task_reminder` | `reminder_minutes` before a task's due time (the task's own, else the user's default, 120) | When the final warning fires |
| `final_warning` | 30 minutes before a task's due time | When the task is due |
| `overdue` | Once, when a task's due time passes; for a task with only a due date, at the next day start | Never (for 30 days) |
| `habit_reminder` | An hour before "Call it a day" while any of the day's habits isn't logged; not on rest days | At midnight |

Only open tasks with a `due_date` count, and reminders and final warnings also need a `due_time`; a lead of 30 minutes or less sends only the final warning. Completed, archived and deleted tasks and archived and deleted habits are left out. Each notification has `id`, `type`, `title`, `body`, `fire_at` (UTC, `Z` suffix), the `task_ids` or `habit_ids` it is about, and `read`; the list is newest first. Ids are deterministic, `{type}:{subject}:{occurrence}` (e.g. `overdue:task-12:2026-10-20T16:00` or `due_today:tasks:2026-10-20`), so read and dismissed state, stored per id, holds across reloads and devices; moving a task's due time makes a new occurrence. `POST /mark-notifications-read` and `POST /dismiss-notifications` take `{"ids": [...]}` (up to 200); `POST /mark-all-notifications-read?tz=` marks everything currently listed. Dismissed notifications are left out of the list. Occurrences older than 30 days are no longer listed, and their state rows are deleted, for every user, at startup and whenever anyone marks or dismisses notifications.

`GET /get-notification-preferences` returns `types` (every type, `true` unless switched off), `quiet_hours_start`/`quiet_hours_end` (`"HH:MM"` or `null`) and `default_reminder_minutes`. `PATCH /update-notification-preferences` changes only the fields given: `types` merges, quiet hours are set as a pair (they may run past midnight, e.g. 22:00 to 07:00) and cleared with two `null`s, and a `null` `default_reminder_minutes` restores 120. An alert that fires during quiet hours is delivered when they end, with its `fire_at` moved to then, and is dropped if it would already have lapsed. Tasks take an optional `reminder_minutes` (1 to 10080) on create and update; `null` uses the default.

**Mood check-ins.** A short end-of-day, end-of-week or end-of-month form about the user's mood, for the calendar reports. There is one check-in per user, `period_kind` (`day`, `week` or `month`) and `period_start`, the caller's local date: the day itself, the Monday of a Mon–Sun week, or the 1st of the month. Every check-in has a `mood` from 1 (Rough) to 5 (Great), an optional `note` (up to 280 characters) and `tags`; the other fields depend on the kind:

| Kind | Fields |
|---|---|
| `day` | `energy` (1 Drained to 5 Charged) · `stress` (1 Calm to 5 Maxed) |
| `week` | `workload` (0 too light, 1 about right, 2 too much) · `highlight` (the best part of the week, up to 120 characters) |
| `month` | `progress` (on what matters, 1 none to 5 a lot) · `word` (the month in one word, up to 24 characters) |

`PUT /save-mood-checkin` creates the period's check-in or replaces it in place (same `id`), so editing and backfilling are the same call; it takes the whole form, so a field left out is cleared. A field from another kind, a week not starting on a Monday, a month not starting on the 1st, or a `period_start` more than a day past the server's UTC date is a 422. `GET /get-mood-checkins` returns the check-ins whose `period_start` falls in a range of up to 366 days, oldest first, optionally only the `kinds` given (comma-separated, e.g. `day,week`). `DELETE /del-mood-checkin/{id}` deletes one for good, with no trash, and returns it so the client's Undo can save it again. Mood tags are a list of their own, separate from task tags: each check-in carries up to 20, trimmed and de-duplicated ignoring case, and `GET /get-mood-tags` returns the default list (`Slept well`, `Exercised`, …) plus the custom tags the user has used, most recent first. Correlations with tasks, note time and habits are left to the frontend, which joins these rows with `/get-calendar-activity`. Mood data stays out of the daily debrief and every AI feature.

The profile's `mood_checkins` settings say which check-ins the dashboard nudges for: `daily` (default on), `weekly` and `monthly` (default off), `remind` (`shutoff`, `shutoff+30` or `off`, default `shutoff`, i.e. at "Call it a day") and `show_in_reports` (default on). `GET /get-profile` returns them with defaults filled in; `POST /save-profile` merges the fields given into the saved ones, a `null` field restores its default and a `null` `mood_checkins` restores them all. The demo sandbox comes with about 60 days of check-ins.

**Canvas.** A user connects their school's Canvas LMS with a personal access token (Canvas → Account → Settings → New access token) and the school's Canvas address. `PUT /canvas/connection` takes `{"base_url", "token"}`: the address may be a bare host or any page URL on it and is reduced to `https://host`; it must be https on a public DNS name, with no IP literal, custom port or credentials, and every address it resolves to must be public, since the server calls it. The token is checked with Canvas (`GET /users/self`) before anything is stored, so a failed connect or replace leaves the current connection as it was; its expiry is read from Canvas's list of the user's tokens when Canvas allows it. The token is then encrypted with AES-256-GCM, bound to the user's id, and stored in `canvas_connections`; it is decrypted only to call Canvas and is never returned. `GET /canvas/connection` returns the masked status: `connected`, `status`, `base_url`, `token_last4` (for "Connected · ends in …xxxx"), the Canvas account's id and name, `token_expires_at`, `hidden_course_ids` and timestamps. `status` is `active`, `invalid` (Canvas rejected the token, or it is past its expiry date: show "Reconnect Canvas") or `error` (Canvas couldn't be reached on the last test). `POST /canvas/connection/test` re-checks the token and returns the updated status; an expired token is marked invalid without calling Canvas. The same `PUT` replaces or reconnects; a token for a different Canvas account or school is refused with 409 unless the body adds `"allow_account_change": true`, which also resets `hidden_course_ids`. `PATCH /canvas/connection` takes `{"hidden_course_ids": [...]}` (the courses unticked under "Courses to show", up to 500). `DELETE /canvas/connection` deletes the row and token for good, with no trash; account deletion and the demo reset remove it too. Demo sandboxes get 403 on every Canvas route. Connect and test are limited to 10 calls a minute per user. Canvas failures return a code as the `detail`: `canvas_url_invalid`, `canvas_not_canvas` (the host didn't answer like Canvas), `canvas_token_invalid`, `canvas_forbidden` (422), `canvas_account_changed` (409), `canvas_unavailable` (502: Canvas down, slow or rate-limiting), `canvas_not_connected` (404) and `canvas_not_configured` (503: the server has no `CANVAS_TOKEN_KEY`).

`GET /health` is public and used by Fly.io health checks.

## Running locally

Requires Python 3.12+ and a Postgres database (a Supabase project works as-is).

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
# create .env (see below)
alembic upgrade head
uvicorn app.main:app --reload     # http://localhost:8000/docs
```

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Yes | Postgres connection string. On Supabase, use the pooler URL (the direct `db.*` host is IPv6-only). |
| `SUPABASE_URL` | Yes | Supabase project URL, used to fetch the JWKS for token verification |
| `SUPABASE_SERVICE_ROLE_KEY` | For account & demo endpoints | Service-role key for the Supabase Admin API |
| `SUPABASE_JWT_SECRET` | Only for HS256 projects | Legacy symmetric JWT secret |
| `CANVAS_TOKEN_KEY` | For the Canvas endpoints | 32 random bytes, base64-encoded, that encrypt stored Canvas tokens. Generate one with `python -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())"` and set it with `fly secrets set CANVAS_TOKEN_KEY=<key>`. Without it the app starts with a warning and the Canvas endpoints return 503. Losing or changing it makes stored tokens undecryptable, so users would have to reconnect. To rotate, set `CANVAS_TOKEN_KEY_V2` (then `_V3`, ...): new tokens use the highest version and older keys keep decrypting until every row is re-encrypted. |
| `ALLOWED_ORIGINS` | No | Comma-separated CORS origins (default: `http://localhost:3000,https://kanso-web-app.vercel.app`) |

Linting: `ruff check .` (config in `pyproject.toml`).

Tests: `pip install -r requirements-dev.txt`, then `TEST_DATABASE_URL=postgresql://... pytest`. The tests need Postgres (the queries use Postgres-only SQL) and drop and recreate every table in that database, so point it at an empty throwaway one. Without `TEST_DATABASE_URL` the tests are skipped.

## Deployment

`fly deploy` builds the Docker image; on start the container runs `alembic upgrade head` and then uvicorn. The app runs in Fly's `iad` region (Ashburn), beside the database in AWS `us-east-1`, since each request makes several database round trips. One machine is kept warm to avoid cold starts.

Merges to `main` deploy automatically: `.github/workflows/ci-deploy.yml` runs `ruff check` and the test suite against a throwaway Postgres on every pull request and push to `main`, and on `main` runs `flyctl deploy --remote-only` only if they pass. The workflow needs a `FLY_API_TOKEN` repository secret holding a deploy token from `fly tokens create deploy`.

## Author

Luis Fernando Villalon, San Diego State University. [github.com/LuisFVillalon](https://github.com/LuisFVillalon)
