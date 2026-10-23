"""
Seed data generator for "Try the demo" sandboxes.

Builds a fresh dataset (tasks, habits + streak history, notes + editing
time, mood check-ins, a term tracker and an archived term) anchored to the
visitor's local date, so the demo always looks current no matter when
someone clicks "Try the demo".
seed_demo_data() first wipes every row the sandbox user owns, so reloading
the demo always starts from the same baseline.
"""

import random
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.crud.account_crud import delete_user_rows
from app.crud.habit_crud import resolve_today
from app.models.archived_term_model import ArchivedTerm
from app.models.calendar_settings_model import CalendarSettings
from app.models.habit_log_model import HabitLog
from app.models.habit_model import Habit
from app.models.mood_checkin_model import MoodCheckin
from app.models.note_model import Note
from app.models.note_session_model import NoteSession
from app.models.profile_model import Profile
from app.models.tag_model import Tag
from app.models.task_model import Task


def _make_tag(db: Session, name: str, color: str, user_id: str) -> Tag:
    tag = Tag(name=name, color=color, user_id=user_id)
    db.add(tag)
    db.flush()
    return tag


# Note bodies are Tiptap HTML, the same format the editor saves. They start
# below the title, which the editor renders separately from the body.
_NOTE_BST = """
<p>Every node's left subtree holds <mark>smaller keys</mark> and its right subtree
<mark>larger keys</mark>, so a lookup discards half the remaining tree at each step.</p>
<h3>Operations</h3>
<ul>
  <li><strong>Search / insert:</strong> O(h), where h is the tree's height</li>
  <li><strong>Delete:</strong> swap in the in-order successor when a node has two children</li>
  <li><strong>In-order traversal</strong> visits keys in sorted order</li>
</ul>
<h3>Why balance matters</h3>
<p>Inserting sorted data degrades the tree into a linked list (h = n). Self-balancing
variants like <em>AVL</em> and <em>red-black trees</em> keep h = O(log n).</p>
<blockquote><p>Exam tip: know how to trace a left and a right rotation by hand.</p></blockquote>
"""

_NOTE_RETRO = """
<h3>What went well</h3>
<ul>
  <li>Shipped the new dashboard two days early</li>
  <li>Code reviews turned around within a day</li>
</ul>
<h3>What to improve</h3>
<ul>
  <li>Standups keep running past 15 minutes</li>
  <li>Flaky integration test blocked two merges</li>
</ul>
<p><strong>Action item:</strong> timebox standup and move deep dives to a follow-up.</p>
"""

_NOTE_RECIPE = """
<p>About 20 minutes, serves two.</p>
<ol>
  <li>Slice chicken thigh thin and toss with soy sauce and cornstarch</li>
  <li>Sear in a very hot pan, then set aside</li>
  <li>Stir fry garlic, ginger and broccoli for 3 minutes</li>
  <li>Return the chicken with a splash of <mark>oyster sauce</mark> and serve over rice</li>
</ol>
"""

_NOTE_HABITS = """
<ul>
  <li><strong>Habit stacking:</strong> attach a new habit to one you already do</li>
  <li><strong>Make it easy:</strong> reduce friction for good habits, add it for bad ones</li>
  <li><strong>Never miss twice:</strong> one missed day is an accident, two is a new pattern</li>
</ul>
<p>Pairs well with the streak tracker: the goal is consistency, not perfection.</p>
"""


def seed_demo_data(db: Session, user_id: str, local_date: str | None = None) -> None:
    """Wipe and repopulate a demo sandbox with sample data dated relative to
    the caller's local date (server date if omitted)."""
    delete_user_rows(db, user_id)

    today = resolve_today(local_date)
    now = datetime.now(UTC)

    # Weekly and monthly check-ins are opt-in; the demo turns them on to show them off.
    db.add(Profile(user_id=user_id, name="Guest", mood_checkins={"weekly": True, "monthly": True}))
    db.add(CalendarSettings(
        user_id=user_id,
        title="Fall Semester",
        start_date=today - timedelta(days=40),
        end_date=today + timedelta(days=70),
    ))
    # The term before it, archived, so the month calendar shows a past term too.
    db.add(ArchivedTerm(
        user_id=user_id,
        title="Summer Session",
        start_date=today - timedelta(days=120),
        end_date=today - timedelta(days=50),
    ))

    school_tag = _make_tag(db, "School", "#8b5cf6", user_id)
    work_tag = _make_tag(db, "Work", "#3b82f6", user_id)
    personal_tag = _make_tag(db, "Personal", "#22c55e", user_id)
    health_tag = _make_tag(db, "Health", "#f97316", user_id)

    # ── Tasks: a mix of overdue, due-today, and upcoming ──────────────────────
    # (title, days offset from today, due time, completed, priority, est. hours, tags)
    task_defs = [
        ("Send client status report", -3, time(17, 0), False, 1, 1.0, [work_tag]),
        ("Renew car registration", -1, time(12, 0), False, None, 0.5, [personal_tag]),
        ("Book dentist appointment", -5, time(9, 0), True, None, 0.25, [health_tag]),
        ("Data structures problem set 4", 0, time(23, 59), False, 2, 3.0, [school_tag]),
        ("Team standup notes", 0, time(9, 30), True, None, 0.5, [work_tag]),
        ("Grocery run", 0, time(18, 0), False, None, 1.0, [personal_tag]),
        ("Review BST rotations for midterm", 1, time(20, 0), False, 3, 2.0, [school_tag]),
        ("Prep slides for Friday demo", 2, time(10, 0), False, 4, 2.5, [work_tag]),
        ("Plan weekend hike", 4, time(19, 0), False, None, 0.5, [personal_tag, health_tag]),
        ("Annual health checkup", 7, time(8, 30), False, None, 1.5, [health_tag]),
    ]
    for title, offset, due_time, completed, priority, hours, tags in task_defs:
        due = today + timedelta(days=offset)
        created = now - timedelta(days=abs(offset) + 1)
        # Tasks due today were finished an hour ago, so the daily debrief's
        # "completed today" list isn't empty; older ones soon after creation.
        finished = now - timedelta(hours=1) if offset == 0 else created + timedelta(hours=2)
        db_task = Task(
            title=title,
            completed=completed,
            priority=priority,
            due_date=datetime.combine(due, time(0, 0)),
            due_time=due_time,
            estimated_time=hours,
            created_date=created,
            completed_date=finished if completed else None,
            user_id=user_id,
        )
        db_task.tags = tags
        db.add(db_task)

    # ── Habits: streak history up to today, plus an older broken run ──────────
    # (title, consecutive days logged up to and including today, est. hours, tags)
    habit_defs = [
        ("Morning meditation", 10, 0.25, [health_tag]),
        ("Read 20 pages", 6, 0.5, [personal_tag]),
        ("Review flashcards", 4, 0.5, [school_tag]),
        ("No-sugar diet", 13, None, [health_tag]),
    ]
    for title, streak_days, hours, tags in habit_defs:
        habit = Habit(
            title=title,
            user_id=user_id,
            current_streak=streak_days,
            max_streak=streak_days,
            estimated_time=hours,
        )
        habit.tags = tags
        db.add(habit)
        db.flush()

        for i in range(streak_days):
            db.add(HabitLog(habit_id=habit.id, logged_date=today - timedelta(days=i)))
        # An older, disconnected log day so the habit's history view has
        # some texture beyond a single unbroken streak.
        db.add(HabitLog(habit_id=habit.id, logged_date=today - timedelta(days=streak_days + 2)))

    # ── Notes, each with a few editing sessions for the time-tracking stats ──
    # (title, html, days since last edit, session lengths in minutes, tags)
    note_defs = [
        ("Binary search trees", _NOTE_BST, 0, [45, 30, 25], [school_tag]),
        ("Sprint 14 retro", _NOTE_RETRO, 2, [20, 10], [work_tag]),
        ("Atomic Habits: key ideas", _NOTE_HABITS, 1, [15], [personal_tag]),
        ("Weeknight stir fry", _NOTE_RECIPE, 5, [10], []),
    ]
    for title, content, days_ago, session_minutes, tags in note_defs:
        edited = now - timedelta(days=days_ago, hours=3)
        note = Note(
            title=title,
            content=content.strip(),
            user_id=user_id,
            created_date=edited - timedelta(days=len(session_minutes)),
            updated_date=edited,
        )
        note.tags = tags
        db.add(note)
        db.flush()

        # One session per day, the most recent ending at the note's last edit.
        for i, minutes in enumerate(session_minutes):
            ended = edited - timedelta(days=i)
            db.add(NoteSession(
                note_id=note.id,
                user_id=user_id,
                started_at=ended - timedelta(minutes=minutes),
                ended_at=ended,
            ))

    _seed_mood_checkins(db, user_id, today)

    db.commit()


# Month words for the monthly check-ins, most recent first.
_MONTH_WORDS = ("Steady", "Hectic", "Fresh start")
_WEEK_HIGHLIGHTS = (
    "Finished the problem set early",
    "Long hike on Saturday",
    "Demo went well at work",
    "Caught up with old friends",
    "Got the midterm study plan sorted",
    "Cooked every night",
    "Quiet weekend to recharge",
    "Nailed the presentation",
    "Started a new book",
)
# Days of daily check-ins seeded before today.
_MOOD_DAYS = 60


def _seed_mood_checkins(db: Session, user_id: str, today: date) -> None:
    """About 80% of the last 60 days checked in, the past weeks and months too.

    Today is left open so the dashboard nudge still has something to ask.
    A fixed seed keeps the sample the same on every reset.
    """
    rng = random.Random(7)
    for offset in range(_MOOD_DAYS, 0, -1):
        day = today - timedelta(days=offset)
        if rng.random() < 0.2:
            continue
        # Weekends run a little brighter, Mondays a little lower.
        lift = {0: -0.6, 5: 0.6, 6: 0.5}.get(day.weekday(), 0.0)
        mood = min(5, max(1, round(3.4 + lift + rng.gauss(0, 0.9))))
        tags = []
        if mood >= 4 and rng.random() < 0.6:
            tags.append(rng.choice(("Exercised", "Outdoors", "Social", "Slept well")))
        if mood <= 2:
            tags.append(rng.choice(("Tired", "Busy", "Anxious")))
        elif rng.random() < 0.3:
            tags.append(rng.choice(("Focused", "Busy", "Family")))
        db.add(MoodCheckin(
            user_id=user_id,
            period_kind="day",
            period_start=day,
            mood=mood,
            energy=min(5, max(1, mood + rng.choice((-1, 0, 0, 1)))),
            stress=min(5, max(1, 6 - mood + rng.choice((-1, 0, 1)))),
            tags=list(dict.fromkeys(tags)),
        ))

    # The past complete Monday-to-Sunday weeks within the window.
    this_monday = today - timedelta(days=today.weekday())
    week = this_monday - timedelta(weeks=1)
    for highlight in _WEEK_HIGHLIGHTS:
        if week < today - timedelta(days=_MOOD_DAYS):
            break
        db.add(MoodCheckin(
            user_id=user_id,
            period_kind="week",
            period_start=week,
            mood=rng.choice((3, 4, 4, 5)),
            workload=rng.choice((1, 1, 2)),
            highlight=highlight,
        ))
        week -= timedelta(weeks=1)

    # The past complete months within the window.
    month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    for word in _MONTH_WORDS:
        if month < today - timedelta(days=_MOOD_DAYS):
            break
        db.add(MoodCheckin(
            user_id=user_id,
            period_kind="month",
            period_start=month,
            mood=rng.choice((3, 4)),
            progress=rng.choice((3, 4)),
            word=word,
        ))
        month = (month - timedelta(days=1)).replace(day=1)
