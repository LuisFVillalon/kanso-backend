"""
Read-only sample Canvas data for demo sandboxes.

Demo users can't connect Canvas, so the read routes show them this instead,
without any Canvas call or database row. It is a made-up SDSU Fall 2026
term: five courses (COMM 103 hides its final grade), work due over the
next week (one quiz, one item late), one missing assignment, two items
graded last week, CS 460's modules (the last one locked) and three
announcements. Dates are fixed
offsets from the caller's local day, so the same day and time zone always
give the same data. No item has an html_url: there is no real Canvas to
link to.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.crud.canvas_data_crud import recently_graded
from app.schemas.canvas_schema import (
    CanvasAnnouncement,
    CanvasAssignment,
    CanvasCourse,
    CanvasItem,
    CanvasModule,
    CanvasModuleItem,
    CanvasModuleRequirement,
    CanvasSubmission,
)

TERM = "Fall 2026"

COURSES = [
    CanvasCourse(id=1001, name="CS 460: Algorithms", course_code="CS 460", term=TERM,
                 current_score=91.4, current_grade="A-"),
    CanvasCourse(id=1002, name="MATH 254: Linear Algebra", course_code="MATH 254", term=TERM,
                 current_score=84.2, current_grade="B"),
    CanvasCourse(id=1003, name="COMM 103: Oral Communication", course_code="COMM 103", term=TERM,
                 grades_hidden=True),
    CanvasCourse(id=1004, name="PSY 101: Introductory Psychology", course_code="PSY 101", term=TERM,
                 current_score=95.0, current_grade="A"),
    CanvasCourse(id=1005, name="ART 157: Introduction to Drawing", course_code="ART 157", term=TERM,
                 current_score=88.7, current_grade="B+"),
]
_CODES = {course.id: course.course_code for course in COURSES}


def _at(today: date, tz: ZoneInfo, days: int, hour: int, minute: int = 59) -> datetime:
    return datetime.combine(today + timedelta(days=days), time(hour, minute), tzinfo=tz)


# (course id, item id, kind, title, days from today, hour, points, submission)
_ASSIGNMENTS = [
    (1001, 5101, "assignment", "Problem Set 6: Dynamic Programming", 1, 23, 50, CanvasSubmission()),
    (1002, 5201, "quiz", "Quiz 4: Eigenvalues", 2, 10, 20, CanvasSubmission()),
    (1004, 5401, "discussion", "Week 9 discussion: Memory", 3, 23, 10, CanvasSubmission(submitted=True)),
    (1005, 5501, "assignment", "Still life sketchbook", 4, 17, 25, CanvasSubmission()),
    (1003, 5301, "assignment", "Informative speech outline", 5, 23, 30, CanvasSubmission()),
    (1001, 5102, "assignment", "Lab 7: Graph search", 6, 23, 40, CanvasSubmission()),
    (1002, 5202, "assignment", "Homework 7", 0, 9, 25,
     CanvasSubmission(submitted=True, late=True)),
    (1001, 5100, "assignment", "Problem Set 5: Greedy algorithms", -6, 23, 50,
     CanvasSubmission(submitted=True, graded=True, score=47, grade="47")),
    (1002, 5200, "quiz", "Quiz 3: Determinants", -5, 10, 20,
     CanvasSubmission(submitted=True, graded=True, score=17, grade="17")),
    (1004, 5400, "assignment", "Reflection paper 2", -3, 23, 20, CanvasSubmission(missing=True)),
]


def _items(today: date, tz: ZoneInfo) -> list[CanvasAssignment]:
    return [
        CanvasAssignment(
            id=item_id,
            kind=kind,
            title=title,
            course_id=course_id,
            course_name=_CODES[course_id],
            due_at=_at(today, tz, days, hour),
            points_possible=points,
            submission=submission,
            quiz_id=item_id + 9000 if kind == "quiz" else None,
            submission_types=["online_quiz"] if kind == "quiz" else ["online_upload"],
        )
        for course_id, item_id, kind, title, days, hour, points, submission in _ASSIGNMENTS
    ]


def overview(today: date, tz: ZoneInfo, start: datetime, end: datetime) -> dict:
    items = _items(today, tz)
    upcoming = sorted(
        (CanvasItem(**item.model_dump(include=set(CanvasItem.model_fields))) for item in items
         if start <= item.due_at < end),
        key=lambda item: item.due_at,
    )
    missing = [
        CanvasItem(**item.model_dump(include=set(CanvasItem.model_fields))) for item in items
        if item.submission.missing
    ]
    graded = [CanvasItem(**item.model_dump(include=set(CanvasItem.model_fields))) for item in items]
    return {"upcoming": upcoming, "missing": missing, "recently_graded": recently_graded(graded, start)}


def courses() -> list[CanvasCourse]:
    return [course.model_copy() for course in COURSES]


def assignments(course_id: int, today: date, tz: ZoneInfo) -> list[CanvasAssignment] | None:
    """The course's assignments, oldest due first, or None if there's no such sample course."""
    if course_id not in _CODES:
        return None
    return sorted((a for a in _items(today, tz) if a.course_id == course_id), key=lambda a: a.due_at)


def modules(course_id: int, today: date, tz: ZoneInfo) -> list[CanvasModule] | None:
    """CS 460 has modules; the other sample courses have none. None if there's no such sample course."""
    if course_id not in _CODES:
        return None
    if course_id != 1001:
        return []

    def done(kind: str = "must_view") -> CanvasModuleRequirement:
        return CanvasModuleRequirement(type=kind, completed=True)

    return [
        CanvasModule(id=601, name="Week 1–4: Foundations", position=1, state="completed",
                     completed_at=_at(today, tz, -30, 12, 0), items=[
                         CanvasModuleItem(id=6011, title="Syllabus", type="Page", requirement=done()),
                         CanvasModuleItem(id=6012, title="Asymptotic analysis notes", type="File", indent=1,
                                          requirement=done()),
                     ]),
        CanvasModule(id=602, name="Week 5–8: Greedy and dynamic programming", position=2, state="started", items=[
            CanvasModuleItem(id=6021, title="Problem Set 5: Greedy algorithms", type="Assignment",
                             due_at=_at(today, tz, -6, 23), points_possible=50,
                             requirement=done("must_submit")),
            CanvasModuleItem(id=6022, title="Problem Set 6: Dynamic Programming", type="Assignment",
                             due_at=_at(today, tz, 1, 23), points_possible=50,
                             requirement=CanvasModuleRequirement(type="must_submit")),
            CanvasModuleItem(id=6023, title="Lab 7: Graph search", type="Assignment",
                             due_at=_at(today, tz, 6, 23), points_possible=40,
                             requirement=CanvasModuleRequirement(type="min_score", min_score=30)),
        ]),
        CanvasModule(id=603, name="Week 9–12: Graphs and flows", position=3, state="locked",
                     unlock_at=_at(today, tz, 8, 0, 0), require_sequential_progress=True, items=[
                         CanvasModuleItem(id=6031, title="Max-flow lecture slides", type="File", locked=True),
                         CanvasModuleItem(id=6032, title="Midterm 2", type="Quiz", locked=True,
                                          due_at=_at(today, tz, 15, 12, 0), points_possible=100),
                     ]),
    ]


def announcements(today: date, tz: ZoneInfo) -> list[CanvasAnnouncement]:
    return [
        CanvasAnnouncement(id=701, title="Office hours moved to Thursday", course_id=1001,
                           message="This week's office hours are on Thursday from 2 to 4 pm in GMCS 405.",
                           posted_at=_at(today, tz, 0, 8, 15), author="Dr. Rivera", read=False),
        CanvasAnnouncement(id=702, title="Quiz 4 covers sections 5.1–5.3", course_id=1002,
                           message="Bring a calculator. Eigenvalues and diagonalization only, no proofs.",
                           posted_at=_at(today, tz, -1, 16, 40), author="Prof. Chen", read=False),
        CanvasAnnouncement(id=703, title="Speech sign-up sheet is open", course_id=1003,
                           message="Pick a speaking slot for the informative speech by Friday.",
                           posted_at=_at(today, tz, -4, 11, 5), author="Ms. Okafor", read=True),
    ]
