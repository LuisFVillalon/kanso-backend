"""Archived terms: archive the current term, list, delete, ownership, demo seed, account deletion."""

from datetime import date

import pytest

from app.core.auth import UserInfo, get_current_user
from app.crud.account_crud import delete_user_rows
from app.crud.demo_crud import seed_demo_data
from app.main import app
from app.models.archived_term_model import ArchivedTerm
from app.models.calendar_settings_model import CalendarSettings
from tests.conftest import OTHER_USER_ID, USER_ID

FALL = {"title": "Fall 2026", "start_date": "2026-08-24", "end_date": "2026-12-18"}


def _term(db, title="Spring 2026", start=date(2026, 1, 20), end=date(2026, 5, 15), user_id=USER_ID) -> ArchivedTerm:
    row = ArchivedTerm(user_id=user_id, title=title, start_date=start, end_date=end)
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def other_client(client):
    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        id=OTHER_USER_ID, email="other@example.com", provider="email", is_anonymous=False
    )
    return client


# ── Archive ──────────────────────────────────────────────────────────────────

def test_archiving_moves_the_current_term_into_the_archive(client, db):
    client.patch("/update-calendar-settings", json=FALL)

    resp = client.post("/archive-term")
    assert resp.status_code == 200
    body = resp.json()
    assert (body["title"], body["start_date"], body["end_date"]) == ("Fall 2026", "2026-08-24", "2026-12-18")
    assert body["id"] and body["archived_at"]

    # The current term is gone, so the user can set up a new one.
    assert client.get("/get-calendar-settings").status_code == 404
    assert [t["id"] for t in client.get("/get-archived-terms").json()] == [body["id"]]


def test_a_new_current_term_can_be_set_up_after_archiving(client):
    client.patch("/update-calendar-settings", json=FALL)
    client.post("/archive-term")

    spring = {"title": "Spring 2027", "start_date": "2027-01-19", "end_date": "2027-05-14"}
    assert client.patch("/update-calendar-settings", json=spring).status_code == 200
    assert client.get("/get-calendar-settings").json()["title"] == "Spring 2027"
    client.post("/archive-term")

    assert [t["title"] for t in client.get("/get-archived-terms").json()] == ["Fall 2026", "Spring 2027"]


def test_archiving_without_a_current_term_is_a_404(client, db):
    assert client.post("/archive-term").status_code == 404
    assert db.query(ArchivedTerm).count() == 0


def test_not_archiving_leaves_the_current_term_as_it_is(client):
    client.patch("/update-calendar-settings", json=FALL)
    client.patch("/update-calendar-settings", json={"title": "Fall Semester"})

    current = client.get("/get-calendar-settings").json()
    assert (current["title"], current["start_date"], current["end_date"]) == (
        "Fall Semester", "2026-08-24", "2026-12-18"
    )
    assert client.get("/get-archived-terms").json() == []


def test_archiving_leaves_other_users_current_term_alone(client, db):
    db.add(CalendarSettings(
        user_id=OTHER_USER_ID, title="Theirs", start_date=date(2026, 9, 1), end_date=date(2026, 12, 1)
    ))
    db.flush()

    assert client.post("/archive-term").status_code == 404
    assert db.query(CalendarSettings).filter(CalendarSettings.user_id == OTHER_USER_ID).count() == 1


# ── List ─────────────────────────────────────────────────────────────────────

def test_list_returns_only_the_users_terms_earliest_first(client, db):
    later = _term(db, "Spring 2026", date(2026, 1, 20), date(2026, 5, 15))
    earlier = _term(db, "Fall 2025", date(2025, 8, 25), date(2025, 12, 19))
    _term(db, "Theirs", user_id=OTHER_USER_ID)

    body = client.get("/get-archived-terms").json()
    assert [t["id"] for t in body] == [earlier.id, later.id]
    assert body[0] == {
        "id": earlier.id,
        "title": "Fall 2025",
        "start_date": "2025-08-25",
        "end_date": "2025-12-19",
        "archived_at": body[0]["archived_at"],
    }


def test_get_calendar_settings_is_unchanged_by_archived_terms(client, db):
    _term(db)
    client.patch("/update-calendar-settings", json=FALL)
    assert set(client.get("/get-calendar-settings").json()) == {"id", "title", "start_date", "end_date"}


# ── Delete ───────────────────────────────────────────────────────────────────

def test_delete_removes_the_term_and_returns_it(client, db):
    term = _term(db)

    resp = client.delete(f"/del-archived-term/{term.id}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Spring 2026"
    assert db.query(ArchivedTerm).filter(ArchivedTerm.id == term.id).first() is None
    assert client.delete(f"/del-archived-term/{term.id}").status_code == 404


def test_delete_another_users_term_is_a_404_and_keeps_it(other_client, db):
    term = _term(db)

    assert other_client.delete(f"/del-archived-term/{term.id}").status_code == 404
    assert db.query(ArchivedTerm).filter(ArchivedTerm.id == term.id).first() is not None
    assert other_client.get("/get-archived-terms").json() == []


def test_delete_leaves_the_current_term_alone(client, db):
    term = _term(db)
    client.patch("/update-calendar-settings", json=FALL)

    client.delete(f"/del-archived-term/{term.id}")
    assert client.get("/get-calendar-settings").json()["title"] == "Fall 2026"


# ── Demo seed and account deletion ───────────────────────────────────────────

def test_demo_seed_adds_one_past_archived_term_and_reseeding_resets_it(db):
    today = date(2026, 10, 20)
    seed_demo_data(db, USER_ID, local_date=today.isoformat())
    _term(db, "Added by the visitor")

    seed_demo_data(db, USER_ID, local_date=today.isoformat())
    terms = db.query(ArchivedTerm).filter(ArchivedTerm.user_id == USER_ID).all()
    assert [t.title for t in terms] == ["Summer Session"]
    assert terms[0].end_date < today
    current = db.query(CalendarSettings).filter(CalendarSettings.user_id == USER_ID).one()
    assert terms[0].end_date < current.start_date


def test_account_deletion_deletes_archived_terms(db):
    _term(db)
    _term(db, user_id=OTHER_USER_ID)

    delete_user_rows(db, USER_ID)
    assert [t.user_id for t in db.query(ArchivedTerm).all()] == [OTHER_USER_ID]

