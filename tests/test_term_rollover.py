"""Term rollover: queuing the next term, auto-archive, and rolling over on read."""

from datetime import date

import pytest

from app.crud.account_crud import delete_user_rows
from app.crud.calendar_crud import roll_over_term
from app.models.archived_term_model import ArchivedTerm
from app.models.calendar_settings_model import CalendarSettings
from app.models.term_rollover_model import TermRollover
from tests.conftest import OTHER_USER_ID, USER_ID

FALL = {"title": "Fall 2026", "start_date": "2026-08-24", "end_date": "2026-12-18"}
SPRING = {"title": "Spring 2027", "start_date": "2027-01-19", "end_date": "2027-05-14"}
DURING_FALL = "2026-10-23"
FALL_LAST_DAY = "2026-12-18"
AFTER_FALL = "2026-12-19"


def _current(client, local_date=DURING_FALL):
    return client.get("/get-calendar-settings", params={"local_date": local_date})


def _rollover(client, local_date=DURING_FALL):
    return client.get("/get-term-rollover", params={"local_date": local_date}).json()


def _terms(body):
    return (body["title"], body["start_date"], body["end_date"])


@pytest.fixture
def fall(client):
    client.patch("/update-calendar-settings", json=FALL)
    return client


# ── Defaults and auto-archive ────────────────────────────────────────────────

def test_rollover_defaults_to_auto_archive_off_and_nothing_queued(client):
    assert _rollover(client) == {"auto_archive": False, "next_term": None, "ended_term": None}


def test_auto_archive_toggles(client):
    body = client.patch("/update-term-rollover", json={"auto_archive": True}).json()
    assert body["auto_archive"] is True
    assert _rollover(client)["auto_archive"] is True
    assert client.patch("/update-term-rollover", json={"auto_archive": False}).json()["auto_archive"] is False


def test_auto_archive_needs_a_boolean(client):
    assert client.patch("/update-term-rollover", json={}).status_code == 422


# ── Saving the next term ─────────────────────────────────────────────────────

def test_save_next_term_queues_it_and_leaves_the_current_term(fall):
    resp = fall.put("/save-next-term", json=SPRING, params={"local_date": DURING_FALL})
    assert resp.status_code == 200
    assert _terms(resp.json()["next_term"]) == ("Spring 2027", "2027-01-19", "2027-05-14")

    assert _current(fall).json()["title"] == "Fall 2026"
    assert _terms(_rollover(fall)["next_term"]) == _terms(SPRING)


def test_saving_again_replaces_the_next_term(fall):
    fall.put("/save-next-term", json=SPRING)
    fall.put("/save-next-term", json={**SPRING, "title": "Spring Semester"})
    assert _rollover(fall)["next_term"]["title"] == "Spring Semester"


def test_next_term_must_start_after_the_current_term_ends(fall):
    for start in ("2026-12-18", "2026-11-01"):
        resp = fall.put("/save-next-term", json={**SPRING, "start_date": start})
        assert resp.status_code == 422
    assert fall.put("/save-next-term", json={**SPRING, "start_date": "2026-12-19"}).status_code == 200


def test_next_term_must_end_after_it_starts(fall):
    for end in ("2027-01-19", "2027-01-01"):
        assert fall.put("/save-next-term", json={**SPRING, "end_date": end}).status_code == 422
    assert fall.put("/save-next-term", json={**SPRING, "title": ""}).status_code == 422
    assert _rollover(fall)["next_term"] is None


def test_next_term_needs_a_current_term(client, db):
    assert client.put("/save-next-term", json=SPRING).status_code == 409
    assert db.query(TermRollover).count() == 0


def test_delete_next_term(fall):
    assert fall.delete("/del-next-term").status_code == 404
    fall.put("/save-next-term", json=SPRING)

    resp = fall.delete("/del-next-term")
    assert resp.status_code == 200 and resp.json()["next_term"] is None
    assert _current(fall, AFTER_FALL).json()["title"] == "Fall 2026"
    assert fall.delete("/del-next-term").status_code == 404


# ── Rollover with auto-archive on ────────────────────────────────────────────

def test_auto_archive_archives_the_ended_term_and_promotes_the_next(fall, db):
    fall.patch("/update-term-rollover", json={"auto_archive": True})
    fall.put("/save-next-term", json=SPRING)

    assert _current(fall, FALL_LAST_DAY).json()["title"] == "Fall 2026"
    assert db.query(ArchivedTerm).count() == 0

    current = _current(fall, AFTER_FALL).json()
    assert _terms(current) == _terms(SPRING)
    archived = fall.get("/get-archived-terms").json()
    assert [_terms(t) for t in archived] == [_terms(FALL)]
    assert _rollover(fall, AFTER_FALL) == {"auto_archive": True, "next_term": None, "ended_term": None}


def test_auto_archive_without_a_next_term_leaves_no_current_term(fall):
    fall.patch("/update-term-rollover", json={"auto_archive": True})

    assert _current(fall, AFTER_FALL).status_code == 404
    assert [t["title"] for t in fall.get("/get-archived-terms").json()] == ["Fall 2026"]


def test_rollover_is_idempotent(fall, db):
    fall.patch("/update-term-rollover", json={"auto_archive": True})
    fall.put("/save-next-term", json=SPRING)

    for _ in range(3):
        _current(fall, AFTER_FALL)
        fall.get("/get-archived-terms", params={"local_date": AFTER_FALL})
        _rollover(fall, AFTER_FALL)
        roll_over_term(db, USER_ID, date(2026, 12, 19))

    assert [t.title for t in db.query(ArchivedTerm).all()] == ["Fall 2026"]
    assert [c.title for c in db.query(CalendarSettings).all()] == ["Spring 2027"]


def test_get_archived_terms_rolls_over_too(fall):
    fall.patch("/update-term-rollover", json={"auto_archive": True})
    body = fall.get("/get-archived-terms", params={"local_date": AFTER_FALL}).json()
    assert [t["title"] for t in body] == ["Fall 2026"]


def test_the_promoted_term_rolls_over_in_turn(fall):
    fall.patch("/update-term-rollover", json={"auto_archive": True})
    fall.put("/save-next-term", json=SPRING)
    _current(fall, AFTER_FALL)

    _current(fall, "2027-05-15")
    assert [t["title"] for t in fall.get("/get-archived-terms").json()] == ["Fall 2026", "Spring 2027"]


# ── Rollover with auto-archive off ───────────────────────────────────────────

def test_without_auto_archive_an_ended_term_with_nothing_queued_stays_current(fall, db):
    assert _current(fall, AFTER_FALL).json()["title"] == "Fall 2026"
    assert db.query(ArchivedTerm).count() == 0
    assert _rollover(fall, AFTER_FALL)["ended_term"] is None


def test_without_auto_archive_the_next_term_is_promoted_and_the_ended_term_kept(fall, db):
    fall.put("/save-next-term", json=SPRING)

    assert _terms(_current(fall, AFTER_FALL).json()) == _terms(SPRING)
    assert db.query(ArchivedTerm).count() == 0
    rollover = _rollover(fall, AFTER_FALL)
    assert rollover["next_term"] is None
    assert _terms(rollover["ended_term"]) == _terms(FALL)


def test_the_kept_ended_term_can_be_archived_later(fall):
    fall.put("/save-next-term", json=SPRING)
    _current(fall, AFTER_FALL)

    resp = fall.post("/archive-ended-term")
    assert resp.status_code == 200 and _terms(resp.json()) == _terms(FALL)
    assert [t["title"] for t in fall.get("/get-archived-terms").json()] == ["Fall 2026"]
    assert _rollover(fall, AFTER_FALL)["ended_term"] is None
    assert fall.post("/archive-ended-term").status_code == 404
    assert _current(fall, AFTER_FALL).json()["title"] == "Spring 2027"


def test_the_kept_ended_term_can_be_dismissed(fall, db):
    assert fall.delete("/del-ended-term").status_code == 404
    fall.put("/save-next-term", json=SPRING)
    _current(fall, AFTER_FALL)

    assert fall.delete("/del-ended-term").json()["ended_term"] is None
    assert db.query(ArchivedTerm).count() == 0


def test_a_manually_archived_term_is_followed_by_the_queued_next_term(fall):
    fall.put("/save-next-term", json=SPRING)
    fall.post("/archive-term")

    assert _current(fall, DURING_FALL).json()["title"] == "Spring 2027"
    assert _rollover(fall)["ended_term"] is None


def test_turning_auto_archive_on_later_archives_an_ended_current_term(fall):
    _current(fall, AFTER_FALL)
    fall.patch("/update-term-rollover", json={"auto_archive": True})

    assert _current(fall, AFTER_FALL).status_code == 404
    assert [t["title"] for t in fall.get("/get-archived-terms").json()] == ["Fall 2026"]


# ── Ownership and account deletion ───────────────────────────────────────────

def test_rollover_only_touches_the_users_own_terms(fall, db):
    db.add(CalendarSettings(
        user_id=OTHER_USER_ID, title="Theirs", start_date=date(2026, 1, 1), end_date=date(2026, 2, 1)
    ))
    db.add(TermRollover(user_id=OTHER_USER_ID, auto_archive=True))
    db.flush()
    fall.patch("/update-term-rollover", json={"auto_archive": True})

    _current(fall, AFTER_FALL)
    assert db.query(CalendarSettings).filter(CalendarSettings.user_id == OTHER_USER_ID).count() == 1
    assert [t.user_id for t in db.query(ArchivedTerm).all()] == [USER_ID]


def test_account_deletion_deletes_the_rollover(db):
    db.add_all([TermRollover(user_id=USER_ID), TermRollover(user_id=OTHER_USER_ID)])
    db.flush()

    delete_user_rows(db, USER_ID)
    assert [r.user_id for r in db.query(TermRollover).all()] == [OTHER_USER_ID]
