"""
Seed data generator for "Try the demo" sandboxes.

Builds a fresh dataset anchored to the visitor's local date, so the demo
always looks current no matter when someone clicks "Try the demo": a
student's term with class, work and personal tags; open tasks (overdue, due
today, upcoming, linked tasks, two linked to the sample Canvas assignments);
about three months of completed tasks, habit logs, note editing sessions and
daily, weekly and monthly mood check-ins, so Reports has history for every
range, drawn so each month shows a different kind of link between mood and
activity (see _plan_history) and energy and stress set apart on days with
the Work and COMM 103 tags (see Mood check-ins); per-weekday work windows
with a rest day; a term tracker and an archived term.

A fixed random seed keeps the sample the same for the same local date.
seed_demo_data() first wipes every row the sandbox user owns, so reloading
the demo always starts from the same baseline.
"""

import math
import random
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
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

    plan = _plan_history(today)
    _seed_open_tasks(db, user_id, today, now, tags)
    _seed_task_history(db, user_id, today, now, tags, rng, plan)
    _seed_habits(db, user_id, today, tags, rng, plan)
    _seed_notes(db, user_id, today, now, tags, rng, plan)
    _seed_mood_checkins(db, user_id, today, plan, _tag_days(db, user_id))

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


# ── History plan ─────────────────────────────────────────────────────────────
# Reports › Mood › Patterns ("Mood & your activity") compares each day's mood
# with the tasks finished, minutes in notes and habits checked off that day,
# one point per day with a daily check-in. Each calendar month of the history
# is drawn to show a different result there, by months back from today:
#   0  this month: about 85% of days checked in; mood higher on days with more
#      tasks done, an "Early sign" while there are under 20 check-ins.
#   1  last month: every day checked in; tasks a strong link, note time a
#      moderate one the other way (mood lower on days with more), habits none.
#   2  the month before: 24 check-ins; habits a moderate link, worded as a
#      hint for having under 30 days; tasks and note time none.
#   3  anything older, where the history starts: check-ins on about a third
#      of days, usually too few for a pattern.
# Across the whole history, note time reads as a slight link in the Year view.
# The labels come from the frontend's analyseCorrelation
# (src/app/utils/calendarReport.ts); tests/test_demo_mood_links.py ports it
# and checks each month's result.

# Per months back and metric: the correlation the day's amount is drawn with
# against mood, and the band its rank correlation with mood over that month's
# check-ins is redrawn until it falls in.
_LINKS = {
    0: {"tasks": (0.85, 0.6, 1.0), "note_minutes": (-0.15, -0.25, 0.0), "habits": (0.0, -0.15, 0.15)},
    1: {"tasks": (0.85, 0.55, 1.0), "note_minutes": (-0.5, -0.47, -0.37), "habits": (0.0, -0.1, 0.1)},
    2: {"tasks": (0.0, -0.15, 0.15), "note_minutes": (-0.2, -0.25, -0.1), "habits": (0.8, 0.5, 1.0)},
    3: {"tasks": (0.5, -1.0, 1.0), "note_minutes": (-0.3, -0.6, 0.0), "habits": (0.2, -1.0, 1.0)},
}
# The band for note time over the year's check-ins, held when the year holds
# months 1 and 2.
_YEAR_NOTE_LINK = (-0.29, -0.24)
# Months with fewer check-ins aren't held to a band.
_MIN_CHECK_INS = 10
_MAX_DRAWS = 30
# Mood runs a little brighter on weekends and a little lower on Mondays.
_MOOD_LIFT = {0: -0.2, 5: 0.3, 6: 0.3}
# Typical tasks finished by weekday, Monday first: fewer on weekends and the rest day.
_TASK_BASE = (2.6, 2.6, 2.6, 2.6, 2.4, 1.4, 0.8)
_MAX_NOTE_MINUTES = 120


@dataclass
class _Day:
    """One day of history: the daily check-in's mood (None when skipped) and
    how much got done, counted the way Reports counts it."""

    day: date
    mood: int | None = None
    tasks: int = 0
    note_minutes: int = 0
    habits: int = 0


def _months_back(today: date, day: date) -> int:
    return min((today.year - day.year) * 12 + today.month - day.month, 3)


def _rank_correlation(days: list[_Day], metric: str) -> float:
    """Spearman's ρ between mood and a metric over the days; 0 when either never changes."""
    try:
        return statistics.correlation(
            [getattr(d, metric) for d in days], [d.mood for d in days], method="ranked"
        )
    except statistics.StatisticsError:
        return 0.0


def _open_tasks_finished(today: date) -> Counter[date]:
    """How many of the open tasks' finished ones (_OPEN_TASKS, _LINKED_TASKS) fall on each past day."""
    offsets = [offset for _, offset, _, done, *_ in _OPEN_TASKS if done and offset is not None and offset < 0]
    offsets += [offset for *_, children in _LINKED_TASKS for _, offset, done, _ in children if done and offset < 0]
    return Counter(today + timedelta(days=offset) for offset in offsets)


def _habit_streaks(today: date) -> dict[str, tuple[set[date], date]]:
    """Each habit's current streak days and the missed day just before them."""
    streaks = {}
    for title, _, _, streak, done_today, _, _ in _HABITS:
        newest = today if done_today else today - timedelta(days=1)
        streaks[title] = ({newest - timedelta(days=i) for i in range(streak)}, newest - timedelta(days=streak))
    return streaks


def _habit_options(streaks: dict[str, tuple[set[date], date]], day: date) -> tuple[list[str], list[str]]:
    """The habits logged on a day for their current streak, and the ones that may be."""
    forced, optional = [], []
    for title, _, _, _, _, _, weekdays_only in _HABITS:
        streak, missed = streaks[title]
        if day in streak:
            forced.append(title)
        elif day != missed and not (weekdays_only and day.weekday() >= 5):
            optional.append(title)
    return forced, optional


def _note_options(days_ago: int) -> tuple[list[str], list[str]]:
    """The notes that exist on a day, and those created or last edited that day, which need a session."""
    alive = [title for title, _, _, created, edited in _NOTES if edited <= days_ago <= created]
    forced = [title for title, _, _, created, edited in _NOTES if days_ago in (created, edited)]
    return alive, forced


def _plan_history(today: date) -> list[_Day]:
    """The last HISTORY_DAYS days' check-ins and amounts, each month drawn to its _LINKS."""
    rng = random.Random(today.toordinal())
    days = [_Day(today - timedelta(days=offset)) for offset in range(HISTORY_DAYS, 0, -1)]
    months: dict[int, list[_Day]] = {}
    for d in days:
        months.setdefault(_months_back(today, d.day), []).append(d)

    # Each day's floor and ceiling per metric: open tasks finished that day,
    # habits on a streak, notes created or last edited that day.
    finished = _open_tasks_finished(today)
    streaks = _habit_streaks(today)
    limits: dict[date, dict[str, tuple[int, int]]] = {}
    for d in days:
        forced, optional = _habit_options(streaks, d.day)
        alive, edited = _note_options((today - d.day).days)
        limits[d.day] = {
            "tasks": (finished[d.day], max(6, finished[d.day])),
            "habits": (len(forced), len(forced) + len(optional)),
            "note_minutes": (10 * len(edited), _MAX_NOTE_MINUTES if alive else 0),
        }

    def amount(metric: str, day: date, z: float) -> int:
        if metric == "tasks":
            value = round(_TASK_BASE[day.weekday()] + 1.4 * z)
        elif metric == "habits":
            value = round(3.6 + 1.5 * z)
        else:
            value = 5 * round((35 + 25 * z) / 5)
        low, high = limits[day][metric]
        return min(high, max(low, value))

    # A latent mood per day: the check-in's mood rounds it, and each metric
    # follows it as closely as its _LINKS strength.
    latent: dict[date, float] = {}

    def draw(back: int, month: list[_Day], metric: str) -> bool:
        """Draws the month's amounts for a metric until it lands in its band; False if it never does."""
        strength, low, high = _LINKS[back][metric]
        checked = [d for d in month if d.mood is not None]
        for _ in range(_MAX_DRAWS):
            for d in month:
                z = strength * latent[d.day] + math.sqrt(1 - strength**2) * rng.gauss(0, 1)
                setattr(d, metric, amount(metric, d.day, z))
            if len(checked) < _MIN_CHECK_INS or low <= _rank_correlation(checked, metric) <= high:
                return True
        return False

    for back, month in months.items():
        if back == 1:
            checked = {d.day for d in month}
        elif back == 2:
            checked = {d.day for d in rng.sample(month, min(24, len(month)))}
        else:
            checked = {d.day for d in month if rng.random() < (0.85 if back == 0 else 0.3)}
        # A month whose moods leave no room for its links (nearly all 3s, say) is drawn again.
        for _ in range(_MAX_DRAWS):
            for d in month:
                latent[d.day] = rng.gauss(0, 1)
                if d.day in checked:
                    lift = _MOOD_LIFT.get(d.day.weekday(), 0.0)
                    d.mood = min(5, max(1, round(3.3 + lift + latent[d.day])))
            if all(draw(back, month, metric) for metric in ("tasks", "habits", "note_minutes")):
                break

    if today.month >= 3:
        year = [d for d in days if d.day.year == today.year and d.mood is not None]
        low, high = _YEAR_NOTE_LINK
        for _ in range(_MAX_DRAWS):
            if low <= _rank_correlation(year, "note_minutes") <= high:
                break
            for back, month in months.items():
                draw(back, month, "note_minutes")
    return days


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
# Library shifts fall on Tuesdays and Thursdays and COMM 103 meets on Mondays
# and Wednesdays, so a day's first finished task carries that tag, giving
# Reports enough days with each to compare (see Mood check-ins).
_WEEKDAY_TAGS = {0: "COMM 103", 1: "Work", 2: "COMM 103", 3: "Work"}
# Class tags come up more often than the rest.
_HISTORY_WEIGHTS = {"CS 460": 5, "MATH 254": 4, "PSY 101": 3, "COMM 103": 1, "ART 157": 2,
                    "Work": 1, "Personal": 3, "Health": 2}


def _seed_task_history(
    db: Session, user_id: str, today: date, now: datetime, tags: dict[str, Tag], rng: random.Random,
    plan: list[_Day],
) -> None:
    """Completed tasks for each planned day, less the open tasks finished that day."""
    names = list(_HISTORY_WEIGHTS)
    weights = list(_HISTORY_WEIGHTS.values())
    counters: dict[str, int] = {}
    open_finished = _open_tasks_finished(today)
    for planned in plan:
        day = planned.day
        for i in range(planned.tasks - open_finished[day]):
            tag = rng.choices(names, weights)[0]
            if i == 0 and day.weekday() in _WEEKDAY_TAGS:
                tag = _WEEKDAY_TAGS[day.weekday()]
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
# (title, est. hours, tags, current streak, done today, how readily it's one of
# an earlier day's check-ins, weekdays only)
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
    db: Session, user_id: str, today: date, tags: dict[str, Tag], rng: random.Random, plan: list[_Day]
) -> None:
    """Each habit's current streak, the miss that started it, and before that
    as many check-ins each day as planned, favoring the habits kept up more."""
    streaks = _habit_streaks(today)
    logged = {title: set(streak) for title, (streak, _) in streaks.items()}
    rates = {title: rate for title, _, _, _, _, rate, _ in _HABITS}
    for planned in plan:
        forced, optional = _habit_options(streaks, planned.day)
        # A weighted draw without replacement: each habit's key is u^(1/rate).
        optional.sort(key=lambda title: rng.random() ** (1 / rates[title]), reverse=True)
        for title in optional[: planned.habits - len(forced)]:
            logged[title].add(planned.day)

    for title, hours, tag_names, _, done_today, _, _ in _HABITS:
        newest = today if done_today else today - timedelta(days=1)
        habit = Habit(
            title=title,
            user_id=user_id,
            current_streak=_streak_ending_on(logged[title], newest),
            max_streak=_max_streak_from_dates(logged[title]),
            estimated_time=hours,
        )
        habit.tags = [tags[name] for name in tag_names]
        db.add(habit)
        db.flush()
        db.add_all(HabitLog(habit_id=habit.id, logged_date=day) for day in sorted(logged[title]))



# ── Notes ────────────────────────────────────────────────────────────────────
# (title, html, tags, days since created, days since last edit)
_NOTES = [
    ("Binary search trees", demo_notes.BST, ["CS 460"], 30, 0),
    ("Dynamic programming patterns", demo_notes.DYNAMIC_PROGRAMMING, ["CS 460"], 12, 1),
    ("Eigenvalues cheat sheet", demo_notes.EIGENVALUES, ["MATH 254"], 9, 2),
    ("Memory: encoding, storage, retrieval", demo_notes.MEMORY, ["PSY 101"], 20, 3),
    ("Informative speech outline", demo_notes.SPEECH_OUTLINE, ["COMM 103"], 6, 0),
    ("Value and shading", demo_notes.DRAWING, ["ART 157"], 45, 10),
    ("Internship applications", demo_notes.INTERNSHIPS, ["Work"], 35, 4),
    ("Library desk retro", demo_notes.RETRO, ["Work"], 25, 18),
    ("Fall semester goals", demo_notes.SEMESTER_GOALS, ["Personal"], 90, 40),
    ("Weekly review template", demo_notes.WEEKLY_REVIEW, ["Personal"], 70, 7),
    ("Atomic Habits: key ideas", demo_notes.HABITS, ["Personal", "Health"], 60, 15),
    ("Weeknight stir fry", demo_notes.RECIPE, [], 50, 22),
]


def _seed_notes(
    db: Session, user_id: str, today: date, now: datetime, tags: dict[str, Tag], rng: random.Random,
    plan: list[_Day],
) -> None:
    """Notes of different ages, with editing sessions adding up to each day's
    planned minutes, on the day each note was created and the day it was last edited."""
    sessions: dict[str, list[tuple[datetime, datetime]]] = {title: [] for title, *_ in _NOTES}
    _, edited_today = _note_options(0)
    minutes_by_day = [(p.day, p.note_minutes) for p in plan] + [(today, 5 * rng.randint(3, 8) * len(edited_today))]
    for day, minutes in minutes_by_day:
        if not minutes:
            continue
        alive, edited = _note_options((today - day).days)
        titles = edited or [rng.choice(alive)]
        if len(titles) == 1 and minutes >= 60 and len(alive) > 1:
            titles.append(rng.choice([title for title in alive if title != titles[0]]))
        # Back to back, the last one ending between 19:00 and 22:40 UTC.
        ended = _at(day, rng.randint(19, 22), rng.choice((0, 20, 40)), now)
        for i, title in enumerate(titles):
            length = minutes // len(titles) + (minutes % len(titles) if i == 0 else 0)
            sessions[title].append((ended - timedelta(minutes=length), ended))
            ended -= timedelta(minutes=length + rng.randint(15, 45))

    for title, content, tag_names, _, _ in _NOTES:
        ranges = sorted(sessions[title])
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
# Reports › Mood › Patterns ("Tags & feelings") compares each day's energy and
# stress on days a tag's tasks were finished or its notes edited with the days
# without it. On days with Work energy drops to 2 (1 if it was low already)
# and on days with COMM 103 stress climbs to 4 (5 if it was high already),
# clear in a month as well as the year; the other tags leave the check-ins as
# drawn. Mood is never changed, so "Mood & your activity" keeps the links
# _plan_history draws.
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


def _tag_days(db: Session, user_id: str) -> dict[date, set[str]]:
    """The tags on each day's finished tasks and edited notes, by UTC day."""
    days: dict[date, set[str]] = defaultdict(set)
    finished = (
        db.query(Task.completed_date, Tag.name)
        .join(Task.tags)
        .filter(Task.user_id == user_id, Task.completed_date.isnot(None))
    )
    edited = (
        db.query(NoteSession.started_at, Tag.name)
        .join(Note, Note.id == NoteSession.note_id)
        .join(Note.tags)
        .filter(NoteSession.user_id == user_id)
    )
    for at, name in [*finished, *edited]:
        days[(at if at.tzinfo is None else at.astimezone(UTC)).date()].add(name)
    return days


def _seed_mood_checkins(
    db: Session, user_id: str, today: date, plan: list[_Day], tag_days: dict[date, set[str]]
) -> None:
    """The planned daily check-ins, energy and stress set by the day's tags
    where they call for it, and every past week and month in the history.

    Today, this week and this month are left open so the dashboard nudges
    still have something to ask. A fixed seed keeps the sample the same on
    every reset.
    """
    rng = random.Random(7)
    for planned in plan:
        day, mood = planned.day, planned.mood
        if mood is None:
            continue
        tags = []
        if mood >= 4 and rng.random() < 0.6:
            tags.append(rng.choice(("Exercised", "Outdoors", "Social", "Slept well")))
        if mood <= 2:
            tags.append(rng.choice(("Tired", "Busy", "Anxious")))
        elif rng.random() < 0.3:
            tags.append(rng.choice(("Focused", "Busy", "Family")))
        # Energy and stress follow mood halfway, so a tag's days stand out from the rest.
        energy = min(5, max(1, round((mood + 3.5) / 2) + rng.choice((-1, 0, 0, 1))))
        stress = min(5, max(1, round((8.5 - mood) / 2) + rng.choice((-1, 0, 1))))
        if "Work" in tag_days.get(day, ()):
            energy = 1 if energy <= 2 else 2
        if "COMM 103" in tag_days.get(day, ()):
            stress = 5 if stress >= 4 else 4
        db.add(MoodCheckin(
            user_id=user_id,
            period_kind="day",
            period_start=day,
            mood=mood,
            energy=energy,
            stress=stress,
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
