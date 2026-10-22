"""Mood check-ins: upsert, range reads, validation, delete, mood tags, profile settings, demo seed, account deletion."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.auth import UserInfo, get_current_user
from app.crud.account_crud import delete_user_rows
from app.crud.demo_crud import seed_demo_data
from app.main import app
from app.models.mood_checkin_model import MoodCheckin
from app.models.profile_model import Profile
from app.schemas.mood_schema import DEFAULT_MOOD_TAGS
from tests.conftest import OTHER_USER_ID, USER_ID

DAY = date(2026, 10, 14)  # A Wednesday.
MONDAY = date(2026, 10, 12)
FIRST = date(2026, 10, 1)


def _save(client, **body):
    return client.put("/save-mood-checkin", json={"period_kind": "day", "period_start": DAY.isoformat(), **body})


def _get(client, start=date(2026, 10, 1), end=date(2026, 10, 31), **params):
    return client.get(
        "/get-mood-checkins", params={"start_date": start.isoformat(), "end_date": end.isoformat(), **params}
    )


def _row(db, period_start: date, kind="day", user_id=USER_ID, mood=3, **fields) -> MoodCheckin:
    row = MoodCheckin(user_id=user_id, period_kind=kind, period_start=period_start, mood=mood, **fields)
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def other_client(client):
    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        id=OTHER_USER_ID, email="other@example.com", provider="email", is_anonymous=False
    )
    return client


# ── Save ─────────────────────────────────────────────────────────────────────

def test_save_creates_a_check_in_and_returns_it(client):
    resp = _save(client, mood=4, energy=3, stress=2, note="Good day", tags=["Focused"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["period_kind"] == "day"
    assert body["period_start"] == "2026-10-14"
    assert (body["mood"], body["energy"], body["stress"]) == (4, 3, 2)
    assert body["note"] == "Good day"
    assert body["tags"] == ["Focused"]
    assert body["workload"] is None and body["word"] is None
    assert body["created_at"] and body["updated_at"]


def test_saving_the_same_period_again_replaces_it_in_place(client, db):
    first = _save(client, mood=2, energy=1, note="Rough start", tags=["Tired"]).json()
    second = _save(client, mood=5).json()

    assert second["id"] == first["id"]
    assert second["mood"] == 5
    # A save is the whole form, so fields left out are cleared.
    assert second["energy"] is None and second["note"] is None and second["tags"] == []
    assert second["created_at"] == first["created_at"]
    assert db.query(MoodCheckin).filter(MoodCheckin.user_id == USER_ID).count() == 1


def test_each_kind_and_period_is_its_own_check_in(client, db):
    _save(client, mood=3)
    _save(client, period_start=(DAY - timedelta(days=1)).isoformat(), mood=3)
    _save(client, period_kind="week", period_start=MONDAY.isoformat(), mood=4, workload=2, highlight="Shipped it")
    _save(client, period_kind="month", period_start=FIRST.isoformat(), mood=4, progress=5, word="Steady")
    assert db.query(MoodCheckin).filter(MoodCheckin.user_id == USER_ID).count() == 4


def test_two_users_can_check_in_for_the_same_day(client, other_client, db):
    _row(db, DAY, mood=1)
    assert _save(other_client, mood=5).status_code == 200
    moods = {r.user_id: r.mood for r in db.query(MoodCheckin).all()}
    assert moods == {USER_ID: 1, OTHER_USER_ID: 5}


@pytest.mark.parametrize(
    "body",
    [
        {"mood": 0},
        {"mood": 6},
        {"mood": 3, "energy": 6},
        {"mood": 3, "stress": 0},
        {"mood": 3, "note": "x" * 281},
        {"mood": 3, "tags": ["x" * 51]},
        {"mood": 3, "tags": [f"tag {i}" for i in range(21)]},
        {"period_kind": "year", "mood": 3},
        {},
    ],
)
def test_save_rejects_out_of_range_values(client, body):
    assert _save(client, **body).status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"workload": 1},
        {"highlight": "Nope"},
        {"progress": 3},
        {"word": "Nope"},
        {"period_kind": "week", "period_start": MONDAY.isoformat(), "energy": 3},
        {"period_kind": "week", "period_start": MONDAY.isoformat(), "word": "Nope"},
        {"period_kind": "month", "period_start": FIRST.isoformat(), "stress": 3},
        {"period_kind": "month", "period_start": FIRST.isoformat(), "workload": 0},
    ],
)
def test_save_rejects_fields_that_belong_to_another_kind(client, body):
    resp = _save(client, mood=3, **body)
    assert resp.status_code == 422
    assert "only applies to" in resp.text


def test_week_check_ins_start_on_a_monday_and_month_check_ins_on_the_1st(client):
    assert _save(client, period_kind="week", period_start=DAY.isoformat(), mood=3).status_code == 422
    assert _save(client, period_kind="week", period_start=MONDAY.isoformat(), mood=3).status_code == 200
    assert _save(client, period_kind="month", period_start=DAY.isoformat(), mood=3).status_code == 422
    assert _save(client, period_kind="month", period_start=FIRST.isoformat(), mood=3).status_code == 200


def test_period_start_may_be_at_most_a_day_past_the_server_date(client):
    today = datetime.now(UTC).date()
    assert _save(client, period_start=(today + timedelta(days=1)).isoformat(), mood=3).status_code == 200
    assert _save(client, period_start=(today + timedelta(days=2)).isoformat(), mood=3).status_code == 422


def test_tags_are_trimmed_deduplicated_and_default_tags_keep_their_spelling(client):
    tags = _save(client, mood=4, tags=["  slept WELL ", "Slept well", "", "  ", "Gym", "gym"]).json()["tags"]
    assert tags == ["Slept well", "Gym"]


# ── Read ─────────────────────────────────────────────────────────────────────

def test_get_returns_the_users_check_ins_in_range_oldest_first(client, db):
    _row(db, date(2026, 9, 30))
    inside = [
        _row(db, MONDAY, kind="week", mood=4, workload=1),
        _row(db, DAY, mood=5, tags=["Social"]),
        _row(db, date(2026, 10, 13), mood=2),
        _row(db, FIRST, kind="month", word="Steady"),
    ]
    _row(db, date(2026, 11, 1))
    _row(db, DAY, user_id=OTHER_USER_ID)

    resp = _get(client)
    assert resp.status_code == 200
    body = resp.json()
    assert [(c["period_start"], c["period_kind"]) for c in body] == [
        ("2026-10-01", "month"), ("2026-10-12", "week"), ("2026-10-13", "day"), ("2026-10-14", "day"),
    ]
    assert {c["id"] for c in body} == {r.id for r in inside}
    assert next(c for c in body if c["period_start"] == "2026-10-14")["tags"] == ["Social"]


def test_get_filters_by_kind(client, db):
    _row(db, DAY)
    _row(db, MONDAY, kind="week")
    _row(db, FIRST, kind="month")

    assert [c["period_kind"] for c in _get(client, kinds="day").json()] == ["day"]
    assert [c["period_kind"] for c in _get(client, kinds="week, month").json()] == ["month", "week"]
    assert len(_get(client).json()) == 3


def test_get_validates_the_range_and_kinds(client):
    assert _get(client, start=DAY, end=DAY - timedelta(days=1)).status_code == 400
    assert _get(client, start=date(2026, 1, 1), end=date(2026, 12, 31)).status_code == 200
    assert _get(client, start=date(2026, 1, 1), end=date(2027, 1, 2)).status_code == 400
    assert _get(client, kinds="day,year").status_code == 422
    assert _get(client, kinds=",").status_code == 422
    assert client.get("/get-mood-checkins", params={"start_date": "2026-10-01"}).status_code == 422


# ── Delete ───────────────────────────────────────────────────────────────────

def test_delete_removes_the_check_in_and_returns_it_for_undo(client, db):
    row = _row(db, DAY, mood=4, note="Nice", tags=["Outdoors"])
    resp = client.delete(f"/del-mood-checkin/{row.id}")
    assert resp.status_code == 200
    assert resp.json()["note"] == "Nice"
    assert db.query(MoodCheckin).count() == 0
    assert client.delete(f"/del-mood-checkin/{row.id}").status_code == 404

    # Undo is saving it again.
    restored = _save(client, **{k: resp.json()[k] for k in ("mood", "note", "tags")})
    assert restored.status_code == 200 and restored.json()["tags"] == ["Outdoors"]


def test_delete_cannot_touch_another_users_check_in(client, db):
    row = _row(db, DAY, user_id=OTHER_USER_ID)
    assert client.delete(f"/del-mood-checkin/{row.id}").status_code == 404
    assert db.query(MoodCheckin).count() == 1


# ── Mood tags ────────────────────────────────────────────────────────────────

def test_mood_tags_are_the_defaults_plus_the_users_custom_tags(client, db):
    _row(db, DAY - timedelta(days=2), tags=["Gym", "Tired"])
    _row(db, DAY, tags=["Rainy", "gym", "slept well"])
    _row(db, DAY, user_id=OTHER_USER_ID, tags=["Secret"])

    body = client.get("/get-mood-tags").json()
    assert body["defaults"] == list(DEFAULT_MOOD_TAGS)
    assert body["custom"] == ["Rainy", "gym"]


def test_mood_tags_without_check_ins_are_just_the_defaults(client):
    assert client.get("/get-mood-tags").json() == {"defaults": list(DEFAULT_MOOD_TAGS), "custom": []}


# ── Profile settings ─────────────────────────────────────────────────────────

DEFAULT_SETTINGS = {"weekly": False, "monthly": False}


def test_profile_returns_default_mood_settings_until_changed(client, db):
    db.add(Profile(user_id=USER_ID, name="Test"))
    db.flush()
    assert client.get("/get-profile").json()["mood_checkins"] == DEFAULT_SETTINGS


def test_saving_mood_settings_changes_only_the_fields_given(client):
    resp = client.post("/save-profile", json={"name": "Test", "mood_checkins": {"weekly": True}})
    assert resp.json()["mood_checkins"] == {**DEFAULT_SETTINGS, "weekly": True}

    resp = client.post("/save-profile", json={"name": "Test", "mood_checkins": {"monthly": True}})
    assert resp.json()["mood_checkins"] == {**DEFAULT_SETTINGS, "weekly": True, "monthly": True}

    # Saving other profile fields leaves them alone.
    resp = client.post("/save-profile", json={"name": "Renamed"})
    assert resp.json()["mood_checkins"]["weekly"] is True

    # null restores one field's default; a null object restores them all.
    resp = client.post("/save-profile", json={"name": "Test", "mood_checkins": {"weekly": None}})
    assert resp.json()["mood_checkins"] == {**DEFAULT_SETTINGS, "monthly": True}
    resp = client.post("/save-profile", json={"name": "Test", "mood_checkins": None})
    assert resp.json()["mood_checkins"] == DEFAULT_SETTINGS


def test_retired_mood_settings_are_accepted_and_ignored(client, db):
    # Older rows and clients still carry daily, remind and show_in_reports.
    db.add(Profile(
        user_id=USER_ID, name="Test", mood_checkins={"daily": False, "remind": "off", "show_in_reports": False},
    ))
    db.flush()
    assert client.get("/get-profile").json()["mood_checkins"] == DEFAULT_SETTINGS

    resp = client.post("/save-profile", json={
        "name": "Test",
        "mood_checkins": {"weekly": True, "daily": False, "remind": "shutoff+30", "show_in_reports": False},
    })
    assert resp.status_code == 200
    assert resp.json()["mood_checkins"] == {**DEFAULT_SETTINGS, "weekly": True}
    assert db.query(Profile).filter(Profile.user_id == USER_ID).one().mood_checkins == {"weekly": True}


# ── Account deletion and demo seed ───────────────────────────────────────────

def test_account_deletion_removes_only_that_users_check_ins(db):
    _row(db, DAY)
    _row(db, MONDAY, kind="week")
    _row(db, DAY, user_id=OTHER_USER_ID)

    delete_user_rows(db, USER_ID)
    assert [r.user_id for r in db.query(MoodCheckin).all()] == [OTHER_USER_ID]


def test_demo_seed_adds_aligned_check_ins_before_today(db):
    today = date(2026, 10, 20)
    seed_demo_data(db, USER_ID, local_date=today.isoformat())

    rows = db.query(MoodCheckin).filter(MoodCheckin.user_id == USER_ID).all()
    days = [r for r in rows if r.period_kind == "day"]
    weeks = [r for r in rows if r.period_kind == "week"]
    months = [r for r in rows if r.period_kind == "month"]
    assert 40 <= len(days) < 60
    assert all(today - timedelta(days=60) <= r.period_start < today for r in days)
    assert all(1 <= r.mood <= 5 and r.workload is None for r in days)
    assert weeks and all(r.period_start.weekday() == 0 and r.period_start < today for r in weeks)
    assert months and all(r.period_start.day == 1 and r.word for r in months)
    assert db.query(Profile).filter(Profile.user_id == USER_ID).one().mood_checkins == {
        "weekly": True,
        "monthly": True,
    }

    # Reseeding starts over rather than piling up.
    seed_demo_data(db, USER_ID, local_date=today.isoformat())
    assert db.query(MoodCheckin).filter(MoodCheckin.user_id == USER_ID).count() == len(rows)
