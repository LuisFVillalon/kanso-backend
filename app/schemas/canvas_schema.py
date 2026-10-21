from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr

from app.schemas.limits import CANVAS_HIDDEN_COURSES_MAX, CANVAS_URL_MAX

CanvasStatus = Literal["active", "invalid", "error"]


class CanvasConnect(BaseModel):
    # The school's Canvas address: a bare host or any page URL on it.
    base_url: str = Field(max_length=CANVAS_URL_MAX)
    # A SecretStr so it can't leak through a repr, a log line or a
    # validation error echoing its input; its format is checked in crud.
    token: SecretStr
    # Replacing the token with one for a different Canvas account or school
    # is refused with canvas_account_changed unless this is true.
    allow_account_change: bool = False


class CanvasConnectionUpdate(BaseModel):
    hidden_course_ids: list[Annotated[int, Field(ge=1)]] = Field(max_length=CANVAS_HIDDEN_COURSES_MAX)


class CanvasConnectionOut(BaseModel):
    """The connection's masked status. Never carries the token itself."""

    connected: bool
    # "invalid" means Canvas rejected the token or it has expired: reconnect.
    status: CanvasStatus | None = None
    base_url: str | None = None
    token_last4: str | None = None
    canvas_user_id: int | None = None
    canvas_user_name: str | None = None
    token_expires_at: datetime | None = None
    hidden_course_ids: list[int] = []
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_synced_at: datetime | None = None
    last_error_at: datetime | None = None


# ── Read routes ──────────────────────────────────────────────────────────────
# Trimmed, normalized shapes of what Canvas returns; never raw Canvas JSON.
# Every html_url is an https link on the school's own Canvas host, or null.

CanvasItemKind = Literal["assignment", "quiz", "discussion", "page", "event", "note", "other"]


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
    start: datetime
    end: datetime
    # Due in [start, end), soonest first.
    upcoming: list[CanvasItem]
    # Past due and not submitted, from Canvas's missing submissions.
    missing: list[CanvasItem]
    # Graded, due in the 7 days before start, newest first, at most 10.
    recently_graded: list[CanvasItem]


class CanvasCoursesOut(CanvasReadOut):
    courses: list[CanvasCourse]


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
    # Canvas shows the assignment submitted: offer to complete the task.
    submitted: bool = False
    # The assignment's due date on Canvas; null if it has none (or not synced).
    due_at: datetime | None = None
    # The task's due date and time were moved to due_at.
    due_changed: bool = False


class CanvasLinkedSyncOut(CanvasReadOut):
    results: list[CanvasLinkedTaskSync]
