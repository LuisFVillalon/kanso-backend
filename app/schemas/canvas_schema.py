from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, computed_field

from app.schemas.limits import CANVAS_HIDDEN_COURSES_MAX, CANVAS_URL_MAX, TAGS_PER_ITEM_MAX
from app.schemas.tag_schema import Tag

CanvasStatus = Literal["active", "invalid", "error"]
CanvasConnectionType = Literal["token", "feed"]
# GET /canvas/overview's window: the next 7 days, or everything upcoming (about 13 months).
CanvasRange = Literal["week", "all"]


class CanvasConnect(BaseModel):
    # The school's Canvas address: a bare host or any page URL on it.
    base_url: str = Field(max_length=CANVAS_URL_MAX)
    # A SecretStr so it can't leak through a repr, a log line or a
    # validation error echoing its input; its format is checked in crud.
    token: SecretStr
    # Replacing the token with one for a different Canvas account or school
    # is refused with canvas_account_changed unless this is true.
    allow_account_change: bool = False


class CanvasFeedConnect(BaseModel):
    # The user's calendar feed link (Canvas → Calendar → Calendar Feed). A
    # SecretStr like the token: anyone holding it can read the calendar. Its
    # length and format are checked in crud.
    feed_url: SecretStr
    # Replacing a connection to another school is refused with
    # canvas_account_changed unless this is true.
    allow_account_change: bool = False


class CanvasConnectionUpdate(BaseModel):
    # Each is left as it is when omitted.
    hidden_course_ids: list[Annotated[int, Field(ge=1)]] | None = Field(None, max_length=CANVAS_HIDDEN_COURSES_MAX)
    auto_complete: bool | None = None


class CanvasCapabilities(BaseModel):
    """What the connection can show. A calendar feed has dates only: upcoming items, and tasks added from them."""

    # GET /canvas/overview's upcoming list.
    upcoming: bool = False
    # GET /canvas/classes: the user's classes with what's due in each.
    classes: bool = False
    # Its missing and recently_graded lists.
    missing: bool = False
    recently_graded: bool = False
    # GET /canvas/courses (grades), and a course's modules and assignments.
    courses: bool = False
    modules: bool = False
    assignments: bool = False
    announcements: bool = False
    # Add to tasks, and the linked-task sync of due dates.
    link_tasks: bool = False
    # The linked-task sync reports whether an assignment was submitted.
    submission_status: bool = False
    # Items carry done (submitted, graded, excused or checked off in Canvas),
    # and the linked-task sync completes tasks whose item is done.
    done_status: bool = False


class CanvasConnectionOut(BaseModel):
    """The connection's masked status. Never carries the token or the feed URL itself."""

    connected: bool
    # "token" (access token) or "feed" (calendar feed link).
    connection_type: CanvasConnectionType | None = None
    capabilities: CanvasCapabilities = CanvasCapabilities()
    # "invalid" means Canvas rejected the token or it has expired: reconnect.
    status: CanvasStatus | None = None
    base_url: str | None = None
    token_last4: str | None = None
    # A feed's link with all but its code's last 4 characters left out.
    feed_url_masked: str | None = None
    # The Canvas account; null for a feed.
    canvas_user_id: int | None = None
    canvas_user_name: str | None = None
    # The IANA zone a feed's all-day items are due in; null for a token.
    time_zone: str | None = None
    token_expires_at: datetime | None = None
    hidden_course_ids: list[int] = []
    # The linked-task sync completes a task once its Canvas item is done.
    auto_complete: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_synced_at: datetime | None = None
    last_error_at: datetime | None = None


# ── Read routes ──────────────────────────────────────────────────────────────
# Trimmed, normalized shapes of what Canvas returns; never raw Canvas JSON.
# Every html_url is an https link on the school's own Canvas host, or null.

CanvasItemKind = Literal["assignment", "quiz", "discussion", "page", "event", "note", "other"]
# Why an item counts as done, in this order when several apply.
CanvasDoneReason = Literal["excused", "graded", "submitted", "marked_complete"]


class CanvasSubmission(BaseModel):
    """The user's own submission for an item."""

    submitted: bool = False
    missing: bool = False
    late: bool = False
    graded: bool = False
    excused: bool = False
    score: float | None = None
    grade: str | None = None
    submitted_at: datetime | None = None
    graded_at: datetime | None = None


def done_reason(submission: CanvasSubmission | None, marked_complete: bool) -> CanvasDoneReason | None:
    if submission is not None:
        if submission.excused:
            return "excused"
        if submission.graded and (submission.submitted or not submission.missing):
            return "graded"
        if submission.submitted:
            return "submitted"
    return "marked_complete" if marked_complete else None


class CanvasItem(BaseModel):
    """Something with a due date: an assignment, quiz, discussion, page, event or planner note."""

    id: int
    kind: CanvasItemKind
    title: str
    course_id: int | None = None
    course_name: str | None = None
    due_at: datetime | None = None
    points_possible: float | None = None
    html_url: str | None = None
    submission: CanvasSubmission | None = None
    # Checked off with "Mark as done" in Canvas's planner; a token's only.
    marked_complete: bool = False

    @computed_field
    @property
    def done_reason(self) -> CanvasDoneReason | None:
        """Why the item is done, or null. Always null for a calendar feed, which carries neither."""
        return done_reason(self.submission, self.marked_complete)

    @computed_field
    @property
    def done(self) -> bool:
        return self.done_reason is not None


class CanvasFeedItem(CanvasItem):
    """An item from a calendar feed: no points or submission, but a UID."""

    # The feed item's UID (e.g. "event-assignment-123").
    uid: str
    # Due on a day with no time: due_at is 11:59:59 pm (for an assignment) or
    # midnight that day in the connection's time_zone.
    all_day: bool = False
    # The day an all-day item falls on, which a task added from it sends as
    # canvas_due_date; null otherwise.
    day: date | None = None
    # Can be added to tasks: only assignments, which tasks follow by
    # canvas_assignment_id.
    linkable: bool = False


class CanvasAssignment(CanvasItem):
    # Quizzes are assignments too: kind "quiz", with quiz_id set for classic quizzes.
    unlock_at: datetime | None = None
    lock_at: datetime | None = None
    quiz_id: int | None = None
    submission_types: list[str] = []


class CanvasCourse(BaseModel):
    id: int
    name: str
    course_code: str | None = None
    term: str | None = None
    html_url: str | None = None
    # The instructor hides the course total: show "Hidden by instructor".
    grades_hidden: bool = False
    current_score: float | None = None
    current_grade: str | None = None
    grading_period: str | None = None
    period_score: float | None = None
    period_grade: str | None = None
    # Unticked under "Courses to show"; only listed with include_hidden=true.
    hidden: bool = False


class CanvasModuleRequirement(BaseModel):
    type: str
    min_score: float | None = None
    completed: bool = False


class CanvasModuleItem(BaseModel):
    id: int
    title: str
    # Canvas's item type: Assignment, Quiz, Page, File, Discussion, ExternalUrl, ExternalTool or SubHeader.
    type: str
    html_url: str | None = None
    indent: int = 0
    locked: bool = False
    due_at: datetime | None = None
    points_possible: float | None = None
    requirement: CanvasModuleRequirement | None = None


class CanvasModule(BaseModel):
    id: int
    name: str
    position: int | None = None
    # locked, unlocked, started or completed; null when the course doesn't track progress.
    state: str | None = None
    unlock_at: datetime | None = None
    completed_at: datetime | None = None
    require_sequential_progress: bool = False
    items: list[CanvasModuleItem] = []
    # False when Canvas left a long module's items out and they weren't fetched.
    items_complete: bool = True


class CanvasAnnouncement(BaseModel):
    id: int
    title: str
    # Plain text: Canvas's HTML is stripped server-side, and long messages are cut short.
    message: str
    posted_at: datetime | None = None
    author: str | None = None
    read: bool | None = None
    course_id: int | None = None
    html_url: str | None = None


class CanvasReadOut(BaseModel):
    # When the data was fetched from Canvas, for "Synced 3 min ago".
    fetched_at: datetime
    # Canvas is rate-limiting the token, so this is the last data fetched.
    busy: bool = False
    # Canvas couldn't be reached or is busy, so this is older than usual.
    stale: bool = False
    # Read-only sample data, for demo sandboxes.
    sample: bool = False


class CanvasOverviewOut(CanvasReadOut):
    range: CanvasRange = "week"
    start: datetime
    end: datetime
    # Due in [start, end), soonest first. Feed items only for a calendar feed;
    # a token's items have no uid, so they never validate as one.
    upcoming: list[CanvasItem | CanvasFeedItem]
    # Past due and not submitted, from Canvas's missing submissions.
    missing: list[CanvasItem]
    # Graded, due in the 7 days before start, newest first, at most 10.
    recently_graded: list[CanvasItem]


class CanvasCoursesOut(CanvasReadOut):
    courses: list[CanvasCourse]


class CanvasClass(BaseModel):
    """One of the user's classes, with what's due in it. A feed's come from its items; a token's from its courses."""

    id: int
    # The course code: from the brackets in a feed item's title (e.g. "CS 460-01"), or Canvas's course_code.
    code: str
    # The full name and term; null for a feed, which doesn't carry them.
    name: str | None = None
    term: str | None = None
    html_url: str | None = None
    # Unticked under "Courses to show"; only listed with include_hidden=true.
    hidden: bool = False
    # Items due from the start of today through 7 days later, and through the end of the "all" range.
    due_this_week: int = 0
    upcoming: int = 0
    # The soonest item due from now on, if any.
    next_due: CanvasItem | CanvasFeedItem | None = None


class CanvasClassesOut(CanvasReadOut):
    classes: list[CanvasClass]


class CanvasCourseTagsUpdate(BaseModel):
    # The user's tag ids, in order; an empty list clears the class's default tags.
    tag_ids: list[Annotated[int, Field(ge=1)]] = Field(max_length=TAGS_PER_ITEM_MAX)


class CanvasCourseTags(BaseModel):
    """A class's default tags for tasks added from it. Deleted tags are left out."""

    course_id: int
    tags: list[Tag]


class CanvasCourseTagsOut(BaseModel):
    courses: list[CanvasCourseTags]


class CanvasModulesOut(CanvasReadOut):
    course_id: int
    modules: list[CanvasModule]


class CanvasAssignmentsOut(CanvasReadOut):
    course_id: int
    assignments: list[CanvasAssignment]


class CanvasAnnouncementsOut(CanvasReadOut):
    announcements: list[CanvasAnnouncement]


class CanvasLinkedTaskSync(BaseModel):
    """One open task added from Canvas, after a linked-task sync."""

    task_id: int
    # False when the assignment couldn't be read, so the task was left alone.
    synced: bool
    # Canvas shows the assignment submitted. Always false for a calendar
    # feed, which doesn't carry submissions.
    submitted: bool = False
    # The assignment is done on Canvas, and why (see CanvasItem). Always
    # false for a calendar feed.
    done: bool = False
    done_reason: CanvasDoneReason | None = None
    # This sync completed the task because the assignment is done and the
    # connection's auto_complete is on; the task now has canvas_completed_at.
    completed: bool = False
    # The assignment's due date on Canvas; null if it has none (or not synced).
    due_at: datetime | None = None
    # The task's due date and time were moved to due_at.
    due_changed: bool = False


class CanvasLinkedSyncOut(CanvasReadOut):
    results: list[CanvasLinkedTaskSync]
