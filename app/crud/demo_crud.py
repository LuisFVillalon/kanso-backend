"""
Seed data generator for "Try the demo" sandboxes.

Builds a fresh dataset anchored to the visitor's local date, so the demo
always looks current no matter when someone clicks "Try the demo": a
student's term with class, work and personal tags; open tasks (overdue, due
today, upcoming, linked tasks, two linked to the sample Canvas assignments);
about three months of completed tasks, habit logs, note editing sessions and
daily, weekly and monthly mood check-ins, so Reports has history for every
range; per-weekday work windows with a rest day; a term tracker and an
archived term.

A fixed random seed keeps the sample the same for the same local date.
seed_demo_data() first wipes every row the sandbox user owns, so reloading
the demo always starts from the same baseline.
"""

import random
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.crud import demo_notes
from app.crud.account_crud import delete_user_rows
from app.crud.habit_crud import _max_streak_from_dates, _streak_ending_on, resolve_today
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

# Days of history behind today: completed tasks, habit logs, note sessions and
# daily mood check-ins all reach this far back.
HISTORY_DAYS = 90

# The class tags match the sample Canvas courses (crud/canvas_sample.py).
_TAGS = {
    "CS 460": "#8b5cf6",
    "MATH 254": "#3b82f6",
    "PSY 101": "#ec4899",
    "COMM 103": "#eab308",
    "ART 157": "#14b8a6",
    "Work": "#64748b",
    "Personal": "#22c55e",
    "Health": "#f97316",
}

# Sunday is the rest day; weekdays "0" (Sunday) to "6" (Saturday), as in rest_days.
_REST_DAYS = [0]
_DAY_WINDOWS = {
    "1": {"start": "08:00", "end": "21:00"},
    "2": {"start": "09:30", "end": "22:00"},
    "3": {"start": "08:00", "end": "21:00"},
    "4": {"start": "09:30", "end": "22:00"},
    "5": {"start": "08:00", "end": "17:00"},
    "6": {"start": "10:00", "end": "14:00"},
}


def _make_tag(db: Session, name: str, color: str, user_id: str) -> Tag:
    tag = Tag(name=name, color=color, user_id=user_id)
    db.add(tag)
    db.flush()
    return tag


def _at(day: date, hour: int, minute: int, now: datetime) -> datetime:
    """A UTC instant on `day`, no later than an hour before now."""
    return min(datetime.combine(day, time(hour, minute), tzinfo=UTC), now - timedelta(hours=1))


def seed_demo_data(db: Session, user_id: str, local_date: str | None = None) -> None:
    """Wipe and repopulate a demo sandbox with sample data dated relative to
    the caller's local date (server date if omitted)."""
    delete_user_rows(db, user_id)

    today = resolve_today(local_date)
    now = datetime.now(UTC)
    rng = random.Random(today.toordinal())

    # Weekly and monthly check-ins are opt-in; the demo turns them on to show them off.
    db.add(Profile(
        user_id=user_id,
        name="Guest",
        mood_checkins={"weekly": True, "monthly": True},
        day_start_time="08:00",
        shutoff_time="22:00",
        rest_days=_REST_DAYS,
        day_windows=_DAY_WINDOWS,
    ))
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

    tags = {name: _make_tag(db, name, color, user_id) for name, color in _TAGS.items()}

    _seed_open_tasks(db, user_id, today, now, tags)
    _seed_task_history(db, user_id, today, now, tags, rng)
    _seed_habits(db, user_id, today, now, tags, rng)
    _seed_notes(db, user_id, today, now, tags, rng)
    _seed_mood_checkins(db, user_id, today)

    db.commit()


# ── Open tasks: overdue, due today, upcoming and undated ─────────────────────
# (title, days from today or None, due time, completed, priority, est. hours, tags, description)
# Priorities are unique among a user's active tasks, so only open tasks get one.
_OPEN_TASKS = [
    ("Reflection paper 2", -3, time(23, 59), False, 1, 2.0, ["PSY 101"],
     "Two pages on a memory experiment from chapter 7."),
    ("Renew car registration", -1, time(12, 0), False, None, 0.5, ["Personal"], None),
    ("Submit library timesheet", -2, time(17, 0), False, None, 0.25, ["Work"], None),
    ("Homework 7", 0, time(9, 0), True, None, 1.5, ["MATH 254"], None),
    ("Pick up prescription", 0, time(13, 0), True, None, 0.5, ["Health"], None),
    ("Grocery run", 0, time(18, 0), False, None, 1.0, ["Personal", "Health"], None),
    ("Read chapter 8: Thinking and language", 0, time(21, 0), False, 4, 1.0, ["PSY 101"], None),
    ("Review BST rotations for midterm", 1, time(20, 0), False, 3, 2.0, ["CS 460"], None),
    ("Library shift", 2, time(14, 0), False, None, 4.0, ["Work"], None),
    ("Still life sketchbook", 4, time(17, 59), False, 6, 3.0, ["ART 157"], None),
    ("Informative speech outline", 5, time(23, 59), False, 5, 2.0, ["COMM 103"], None),
    ("Plan weekend hike", 4, time(19, 0), False, None, 0.5, ["Personal", "Health"], None),
    ("Annual health checkup", 9, time(8, 30), False, None, 1.5, ["Health"], None),
    ("Phone screen: Northwind Labs", 6, time(15, 0), False, 7, 1.0, ["Work"],
     "Review the job post and two STAR stories beforehand."),
    ("Register for spring classes", 15, time(8, 0), False, None, 0.5, ["Personal"], None),
    ("Update resume", None, None, False, None, 1.0, ["Work"], None),
    ("Clean out laptop downloads folder", None, None, False, None, 0.5, ["Personal"], None),
]

# Open tasks linked to the sample Canvas assignments, due when those are, so a
# Canvas sync finds them unchanged: (assignment id, title, days, due time, priority, est. hours, tag).
_CANVAS_TASKS = [
    (5101, "Problem Set 6: Dynamic Programming", 1, time(23, 59), 2, 4.0, "CS 460"),
    (5201, "Quiz 4: Eigenvalues", 2, time(10, 59), 8, 1.0, "MATH 254"),
]

# Parents with their linked tasks, in order:
# (title, days, est. hours, tag, [(title, days, completed, est. hours), ...])
_LINKED_TASKS = [
    ("Final project: route planner", 24, 12.0, "CS 460", [
        ("Pick a map dataset", -6, True, 1.0),
        ("Implement Dijkstra's algorithm", 8, False, 4.0),
        ("Write the project report", 20, False, 3.0),
        ("Record the demo video", 23, False, 1.5),
    ]),
    ("Midterm study plan", 10, 6.0, "MATH 254", [
        ("Redo quiz 3 mistakes", -2, True, 1.0),
        ("Review eigenvalues and diagonalization", 5, False, 2.0),
        ("Take the practice exam", 8, False, 2.0),
    ]),
]


def _seed_open_tasks(db: Session, user_id: str, today: date, now: datetime, tags: dict[str, Tag]) -> None:
    def add(title, offset, due_time, completed, priority, hours, tag_names, description=None,
            parent=None, position=None, **fields) -> Task:
        created = now - timedelta(days=max(abs(offset or 0), 3) + 2)
        # Tasks due today were finished an hour ago, so the daily debrief's
        # "completed today" list isn't empty; older ones the day they were due.
        if offset is None or offset >= 0:
            finished = now - timedelta(hours=1)
        else:
            finished = _at(today + timedelta(days=offset), 18, 30, now)
        task = Task(
            title=title,
            description=description,
            completed=completed,
            priority=priority,
            due_date=None if offset is None else datetime.combine(today + timedelta(days=offset), time(0, 0)),
            due_time=due_time,
            estimated_time=hours,
            created_date=created,
            completed_date=finished if completed else None,
            parent_task_id=parent.id if parent else None,
            position=position,
            user_id=user_id,
            **fields,
        )
        task.tags = [tags[name] for name in tag_names]
        db.add(task)
        return task

    for title, offset, due_time, completed, priority, hours, tag_names, description in _OPEN_TASKS:
        add(title, offset, due_time, completed, priority, hours, tag_names, description)

    for assignment_id, title, offset, due_time, priority, hours, tag in _CANVAS_TASKS:
        add(title, offset, due_time, False, priority, hours, [tag], canvas_assignment_id=assignment_id)

    for title, offset, hours, tag, children in _LINKED_TASKS:
        parent = add(title, offset, time(23, 59), False, None, hours, [tag])
        db.flush()
        for position, (child_title, child_offset, completed, child_hours) in enumerate(children):
            add(child_title, child_offset, time(23, 59), completed, None, child_hours, [tag],
                parent=parent, position=position)


# ── Completed task history ───────────────────────────────────────────────────
# Title templates per tag; {n} counts up from the oldest task of that template.
_HISTORY_TEMPLATES = {
    "CS 460": ("Problem set {n}", "Lab {n} write-up", "Read CLRS chapter {n}"),
    "MATH 254": ("Practice set {n}", "Section {n} practice problems", "Office hours"),
    "PSY 101": ("Review lecture slides", "Flashcard deck {n}"),
    "COMM 103": ("Speech {n} rehearsal", "Peer review {n}"),
    "ART 157": ("Sketchbook page {n}", "Figure drawing study {n}"),
    "Work": ("Library shift", "Tutoring session {n}", "Submit library timesheet"),
    "Personal": ("Laundry", "Call home", "Grocery run", "Pay rent", "Clean room"),
    "Health": ("Gym: upper body", "Meal prep", "Evening run"),
}
# Class tags come up more often than the rest.
_HISTORY_WEIGHTS = {"CS 460": 5, "MATH 254": 4, "PSY 101": 3, "COMM 103": 2, "ART 157": 2,
                    "Work": 3, "Personal": 3, "Health": 2}


def _seed_task_history(
    db: Session, user_id: str, today: date, now: datetime, tags: dict[str, Tag], rng: random.Random
) -> None:
    """Completed tasks on most days of the last HISTORY_DAYS, fewer on weekends and the rest day."""
    names = list(_HISTORY_WEIGHTS)
    weights = list(_HISTORY_WEIGHTS.values())
    counters: dict[str, int] = {}
    for offset in range(HISTORY_DAYS, 0, -1):
        day = today - timedelta(days=offset)
        weekday = day.weekday()
        if weekday == 6:
            count = rng.choice((0, 0, 1))
        elif weekday == 5:
            count = rng.choice((0, 1, 2))
        else:
            count = rng.choice((1, 2, 2, 3, 4))
        for _ in range(count):
            tag = rng.choices(names, weights)[0]
            template = rng.choice(_HISTORY_TEMPLATES[tag])
            counters[template] = counters.get(template, 0) + 1
            finished = _at(day, rng.randint(16, 23), rng.choice((0, 15, 30, 45)), now)
            # Mostly finished on or before the due date, now and then a day late.
            due = day + timedelta(days=rng.choice((0, 0, 1, 2, -1)))
            task = Task(
                title=template.format(n=counters[template]),
                completed=True,
                due_date=datetime.combine(due, time(0, 0)),
                due_time=rng.choice((time(23, 59), time(17, 0), None)),
                estimated_time=rng.choice((0.5, 1.0, 1.5, 2.0, 3.0)),
                created_date=finished - timedelta(days=rng.randint(1, 6), hours=rng.randint(0, 6)),
                completed_date=finished,
                user_id=user_id,
            )
            task.tags = [tags[tag]]
            db.add(task)



# ── Habits ───────────────────────────────────────────────────────────────────
# (title, est. hours, tags, current streak, done today, chance of a log on an
# earlier day, weekdays only)
_HABITS = [
    ("Drink 8 glasses of water", None, ["Health"], 21, True, 0.9, False),
    ("Morning meditation", 0.25, ["Health", "Personal"], 12, True, 0.8, False),
    ("Review flashcards", 0.5, ["PSY 101", "MATH 254"], 5, False, 0.75, True),
    ("Read 20 pages", 0.5, ["Personal"], 4, True, 0.65, False),
    ("Sketch for 15 minutes", 0.25, ["ART 157"], 3, False, 0.55, False),
    ("One LeetCode problem", 0.75, ["CS 460", "Work"], 2, True, 0.5, True),
    ("Gym", 1.0, ["Health"], 0, False, 0.45, False),
]


def _seed_habits(
    db: Session, user_id: str, today: date, now: datetime, tags: dict[str, Tag], rng: random.Random
) -> None:
    """Each habit's current streak, the miss that started it, and patchier history before."""
    for title, hours, tag_names, streak, done_today, rate, weekdays_only in _HABITS:
        newest = today if done_today else today - timedelta(days=1)
        logged = {newest - timedelta(days=i) for i in range(streak)}
        streak_start = newest - timedelta(days=streak)
        for offset in range(HISTORY_DAYS, 0, -1):
            day = today - timedelta(days=offset)
            if day >= streak_start or (weekdays_only and day.weekday() >= 5):
                continue
            if rng.random() < rate:
                logged.add(day)

        habit = Habit(
            title=title,
            user_id=user_id,
            current_streak=_streak_ending_on(logged, newest),
            max_streak=_max_streak_from_dates(logged),
            estimated_time=hours,
        )
        habit.tags = [tags[name] for name in tag_names]
        db.add(habit)
        db.flush()
        db.add_all(HabitLog(habit_id=habit.id, logged_date=day) for day in sorted(logged))



# ── Notes ────────────────────────────────────────────────────────────────────
# (title, html, tags, days since created, days since last edit, editing sessions)
_NOTES = [
    ("Binary search trees", demo_notes.BST, ["CS 460"], 30, 0, 6),
    ("Dynamic programming patterns", demo_notes.DYNAMIC_PROGRAMMING, ["CS 460"], 12, 1, 5),
    ("Eigenvalues cheat sheet", demo_notes.EIGENVALUES, ["MATH 254"], 9, 2, 4),
    ("Memory: encoding, storage, retrieval", demo_notes.MEMORY, ["PSY 101"], 20, 3, 4),
    ("Informative speech outline", demo_notes.SPEECH_OUTLINE, ["COMM 103"], 6, 0, 3),
    ("Value and shading", demo_notes.DRAWING, ["ART 157"], 45, 10, 3),
    ("Internship applications", demo_notes.INTERNSHIPS, ["Work"], 35, 4, 6),
    ("Library desk retro", demo_notes.RETRO, ["Work"], 25, 18, 2),
    ("Fall semester goals", demo_notes.SEMESTER_GOALS, ["Personal"], 85, 40, 3),
    ("Weekly review template", demo_notes.WEEKLY_REVIEW, ["Personal"], 70, 7, 5),
    ("Atomic Habits: key ideas", demo_notes.HABITS, ["Personal", "Health"], 60, 15, 3),
    ("Weeknight stir fry", demo_notes.RECIPE, [], 50, 22, 2),
]


def _seed_notes(
    db: Session, user_id: str, today: date, now: datetime, tags: dict[str, Tag], rng: random.Random
) -> None:
    """Notes of different ages, each with editing sessions spread from its creation to its last edit."""
    for title, content, tag_names, created_days, edited_days, sessions in _NOTES:
        span = created_days - edited_days
        days = sorted({edited_days + round(span * i / max(sessions - 1, 1)) for i in range(sessions)}, reverse=True)
        ranges = []
        for days_ago in days:
            ended = _at(today - timedelta(days=days_ago), rng.randint(16, 23), rng.choice((0, 20, 40)), now)
            ranges.append((ended - timedelta(minutes=rng.randint(10, 50)), ended))
        note = Note(
            title=title,
            content=content.strip(),
            user_id=user_id,
            created_date=ranges[0][0],
            updated_date=ranges[-1][1],
        )
        note.tags = [tags[name] for name in tag_names]
        db.add(note)
        db.flush()
        db.add_all(
            NoteSession(note_id=note.id, user_id=user_id, started_at=started, ended_at=ended)
            for started, ended in ranges
        )



# ── Mood check-ins ───────────────────────────────────────────────────────────
# Month words for the monthly check-ins, most recent first.
_MONTH_WORDS = ("Steady", "Hectic", "Fresh start")
# One per past week, most recent first.
_WEEK_HIGHLIGHTS = (
    "Finished the problem set early",
    "Long hike on Saturday",
    "Phone screen invite came through",
    "Caught up with old friends",
    "Got the midterm study plan sorted",
    "Cooked every night",
    "Quiet weekend to recharge",
    "Nailed the speech rehearsal",
    "Started a new book",
    "First week of classes went smoothly",
    "Beach day with roommates",
    "Moved into the new apartment",
    "Finished the summer stats final",
)
_DAY_NOTES = (
    "Problem set took longer than planned.",
    "Great study session at the library.",
    "Slept badly, dragged all day.",
    "Good talk with my advisor.",
    "Too many deadlines at once.",
    "Ran 5k without stopping!",
)


def _seed_mood_checkins(db: Session, user_id: str, today: date) -> None:
    """About 80% of the last HISTORY_DAYS days checked in, and every past week and month in that span.

    Today, this week and this month are left open so the dashboard nudges
    still have something to ask. A fixed seed keeps the sample the same on
    every reset.
    """
    rng = random.Random(7)
    for offset in range(HISTORY_DAYS, 0, -1):
        day = today - timedelta(days=offset)
        if rng.random() < 0.2:
            continue
        # Weekends run a little brighter, Mondays a little lower, and the
        # weeks around midterms (about a month ago) a little lower still.
        lift = {0: -0.6, 5: 0.6, 6: 0.5}.get(day.weekday(), 0.0)
        if 21 <= offset <= 35:
            lift -= 0.5
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
            note=rng.choice(_DAY_NOTES) if rng.random() < 0.15 else None,
            tags=list(dict.fromkeys(tags)),
        ))

    # The past complete Monday-to-Sunday weeks within the window.
    this_monday = today - timedelta(days=today.weekday())
    week = this_monday - timedelta(weeks=1)
    for highlight in _WEEK_HIGHLIGHTS:
        if week < today - timedelta(days=HISTORY_DAYS):
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

    # The past three complete months, which the window overlaps.
    month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    for word in _MONTH_WORDS:
        db.add(MoodCheckin(
            user_id=user_id,
            period_kind="month",
            period_start=month,
            mood=rng.choice((3, 4)),
            progress=rng.choice((3, 4)),
            word=word,
        ))
        month = (month - timedelta(days=1)).replace(day=1)
