"""
Normalizing Canvas JSON into the trimmed shapes the API returns.

Canvas answers are untrusted input: every field is type-checked, anything
malformed is dropped (or the whole item, when it has no id or title), and
every link must stay on the school's Canvas host.
"""

import html
import re
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from app.core.canvas_feed import FeedEvent
from app.schemas.canvas_schema import (
    CanvasAnnouncement,
    CanvasAssignment,
    CanvasCourse,
    CanvasFeedItem,
    CanvasItem,
    CanvasModule,
    CanvasModuleItem,
    CanvasModuleRequirement,
    CanvasSubmission,
)

ANNOUNCEMENT_MESSAGE_MAX = 2000


def as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def as_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def canvas_url(base_url: str, value: Any) -> str | None:
    """value as an absolute https link on the school's Canvas host, or None for anything else.

    The frontend renders these as links, so a javascript: URL or a link to
    another site from a hostile Canvas host never gets through.
    """
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return f"{base_url}{value}"
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or port not in (None, 443) or parts.username or parts.password:
        return None
    if f"https://{(parts.hostname or '').lower()}" != base_url:
        return None
    return value


def feed_entry(base_url: str, event: FeedEvent) -> tuple[CanvasFeedItem, date | None]:
    """A calendar feed event as an item, and the day it falls on when it's all-day (its due_at is then None)."""
    # Point an assignment at its own page, as the API's html_url does, rather
    # than at the calendar the feed links to.
    if event.kind == "assignment" and event.course_id is not None:
        html_url = f"{base_url}/courses/{event.course_id}/assignments/{event.id}"
    else:
        html_url = canvas_url(base_url, event.url)
    all_day = not isinstance(event.start, datetime)
    item = CanvasFeedItem(
        id=event.id,
        kind=event.kind,
        title=event.title,
        course_id=event.course_id,
        course_name=event.course_name,
        due_at=None if all_day else event.start,
        html_url=html_url,
        uid=event.uid,
        all_day=all_day,
        linkable=event.kind == "assignment",
    )
    return item, event.start if all_day else None


def _planner_submission(raw) -> CanvasSubmission | None:
    if not isinstance(raw, dict):
        return None
    flags = ("submitted", "missing", "late", "graded", "excused")
    return CanvasSubmission(**{flag: raw.get(flag) is True for flag in flags})


_PLANNER_KINDS = {
    "assignment": "assignment",
    "quiz": "quiz",
    "discussion_topic": "discussion",
    "wiki_page": "page",
    "calendar_event": "event",
    "planner_note": "note",
}


def parse_planner_item(base_url: str, raw: Any) -> CanvasItem | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("plannable"), dict):
        return None
    plannable = raw["plannable"]
    item_id = as_int(raw.get("plannable_id"))
    title = as_str(plannable.get("title")) or as_str(plannable.get("name"))
    if item_id is None or title is None:
        return None
    kind = _PLANNER_KINDS.get(raw.get("plannable_type"), "other")
    if kind == "assignment" and plannable.get("is_quiz_assignment") is True:
        kind = "quiz"
    return CanvasItem(
        id=item_id,
        kind=kind,
        title=title,
        course_id=as_int(raw.get("course_id")),
        course_name=as_str(raw.get("context_name")),
        due_at=as_datetime(plannable.get("due_at")) or as_datetime(raw.get("plannable_date")),
        points_possible=as_float(plannable.get("points_possible")),
        html_url=canvas_url(base_url, raw.get("html_url")),
        submission=_planner_submission(raw.get("submissions")),
    )


def _assignment_kind(raw: dict) -> str:
    types = raw.get("submission_types")
    if raw.get("is_quiz_assignment") is True or (isinstance(types, list) and "online_quiz" in types):
        return "quiz"
    if isinstance(types, list) and "discussion_topic" in types:
        return "discussion"
    return "assignment"


def parse_missing_item(base_url: str, raw: Any) -> CanvasItem | None:
    if not isinstance(raw, dict) or as_int(raw.get("id")) is None or as_str(raw.get("name")) is None:
        return None
    course = raw.get("course") if isinstance(raw.get("course"), dict) else {}
    return CanvasItem(
        id=as_int(raw["id"]),
        kind=_assignment_kind(raw),
        title=raw["name"],
        course_id=as_int(raw.get("course_id")),
        course_name=as_str(course.get("course_code")) or as_str(course.get("name")),
        due_at=as_datetime(raw.get("due_at")),
        points_possible=as_float(raw.get("points_possible")),
        html_url=canvas_url(base_url, raw.get("html_url")),
        submission=CanvasSubmission(missing=True),
    )


def _submission(raw) -> CanvasSubmission | None:
    if not isinstance(raw, dict):
        return None
    grade = raw.get("grade")
    return CanvasSubmission(
        submitted=as_datetime(raw.get("submitted_at")) is not None,
        missing=raw.get("missing") is True,
        late=raw.get("late") is True,
        graded=raw.get("workflow_state") == "graded",
        excused=raw.get("excused") is True,
        score=as_float(raw.get("score")),
        grade=grade if isinstance(grade, str) else None,
        submitted_at=as_datetime(raw.get("submitted_at")),
        graded_at=as_datetime(raw.get("graded_at")),
    )


def parse_assignment(base_url: str, raw: Any, course_id: int) -> CanvasAssignment | None:
    if not isinstance(raw, dict) or as_int(raw.get("id")) is None or as_str(raw.get("name")) is None:
        return None
    types = raw.get("submission_types")
    return CanvasAssignment(
        id=as_int(raw["id"]),
        kind=_assignment_kind(raw),
        title=raw["name"],
        course_id=course_id,
        due_at=as_datetime(raw.get("due_at")),
        points_possible=as_float(raw.get("points_possible")),
        html_url=canvas_url(base_url, raw.get("html_url")),
        submission=_submission(raw.get("submission")),
        unlock_at=as_datetime(raw.get("unlock_at")),
        lock_at=as_datetime(raw.get("lock_at")),
        quiz_id=as_int(raw.get("quiz_id")),
        submission_types=[t for t in types if isinstance(t, str)] if isinstance(types, list) else [],
    )


def parse_course(base_url: str, raw: Any) -> CanvasCourse | None:
    # Courses outside their dates come back as just {"id", "access_restricted_by_date"}.
    if not isinstance(raw, dict) or as_int(raw.get("id")) is None or as_str(raw.get("name")) is None:
        return None
    course_id = as_int(raw["id"])
    enrollments = raw.get("enrollments") if isinstance(raw.get("enrollments"), list) else []
    student = next(
        (e for e in enrollments if isinstance(e, dict) and e.get("type") in ("student", "StudentEnrollment")), {}
    )
    term = raw.get("term") if isinstance(raw.get("term"), dict) else {}
    hidden_grades = raw.get("hide_final_grades") is True

    def grade(field):
        return None if hidden_grades else student.get(field)

    return CanvasCourse(
        id=course_id,
        name=raw["name"],
        course_code=as_str(raw.get("course_code")),
        term=as_str(term.get("name")),
        html_url=f"{base_url}/courses/{course_id}",
        grades_hidden=hidden_grades,
        current_score=as_float(grade("computed_current_score")),
        current_grade=as_str(grade("computed_current_grade")),
        grading_period=as_str(student.get("current_grading_period_title")),
        period_score=as_float(grade("current_period_computed_current_score")),
        period_grade=as_str(grade("current_period_computed_current_grade")),
    )


def _module_item(base_url: str, raw) -> CanvasModuleItem | None:
    if not isinstance(raw, dict) or as_int(raw.get("id")) is None or as_str(raw.get("title")) is None:
        return None
    details = raw.get("content_details") if isinstance(raw.get("content_details"), dict) else {}
    requirement = raw.get("completion_requirement")
    return CanvasModuleItem(
        id=as_int(raw["id"]),
        title=raw["title"],
        type=as_str(raw.get("type")) or "Other",
        html_url=canvas_url(base_url, raw.get("html_url")),
        indent=as_int(raw.get("indent")) or 0,
        locked=details.get("locked_for_user") is True,
        due_at=as_datetime(details.get("due_at")),
        points_possible=as_float(details.get("points_possible")),
        requirement=CanvasModuleRequirement(
            type=requirement["type"],
            min_score=as_float(requirement.get("min_score")),
            completed=requirement.get("completed") is True,
        )
        if isinstance(requirement, dict) and as_str(requirement.get("type"))
        else None,
    )


def parse_module(base_url: str, raw: dict, raw_items: list, complete: bool) -> CanvasModule:
    return CanvasModule(
        id=raw["id"],
        name=as_str(raw.get("name")) or "Untitled module",
        position=as_int(raw.get("position")),
        state=as_str(raw.get("state")),
        unlock_at=as_datetime(raw.get("unlock_at")),
        completed_at=as_datetime(raw.get("completed_at")),
        require_sequential_progress=raw.get("require_sequential_progress") is True,
        items=[item for r in raw_items if (item := _module_item(base_url, r)) is not None],
        items_complete=complete,
    )


class _Text(HTMLParser):
    """Collects an HTML fragment's text, leaving out scripts and styles."""

    _SKIP = {"script", "style"}
    _BREAK = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skipping:
            self.parts.append(data)


def plain_text(fragment: str, limit: int = ANNOUNCEMENT_MESSAGE_MAX) -> str:
    """An HTML fragment as plain text: tags dropped, whitespace tidied, cut to limit characters."""
    parser = _Text()
    parser.feed(fragment)
    parser.close()
    text = html.unescape("".join(parser.parts))
    lines = [re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip() for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_announcement(base_url: str, raw: Any) -> CanvasAnnouncement | None:
    if not isinstance(raw, dict) or as_int(raw.get("id")) is None or as_str(raw.get("title")) is None:
        return None
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    context = raw.get("context_code")
    course_id = context.removeprefix("course_") if isinstance(context, str) and context.startswith("course_") else None
    read_state = raw.get("read_state")
    return CanvasAnnouncement(
        id=as_int(raw["id"]),
        title=raw["title"],
        message=plain_text(raw["message"]) if isinstance(raw.get("message"), str) else "",
        posted_at=as_datetime(raw.get("posted_at")),
        author=as_str(author.get("display_name")),
        read={"read": True, "unread": False}.get(read_state),
        course_id=as_int(course_id),
        html_url=canvas_url(base_url, raw.get("html_url")),
    )
