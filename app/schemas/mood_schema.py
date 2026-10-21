from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal, get_args

from pydantic import AfterValidator, BaseModel, BeforeValidator, Field, model_validator

from app.schemas.limits import (
    MOOD_HIGHLIGHT_MAX,
    MOOD_NOTE_MAX,
    MOOD_TAGS_PER_CHECKIN_MAX,
    MOOD_WORD_MAX,
    TAG_NAME_MAX,
)

MoodPeriodKind = Literal["day", "week", "month"]
MOOD_PERIOD_KINDS: tuple[str, ...] = get_args(MoodPeriodKind)

# The mood-tag picker's built-in list. Users add custom tags alongside these;
# none of them are task tags.
DEFAULT_MOOD_TAGS: tuple[str, ...] = (
    "Slept well", "Exercised", "Outdoors", "Social", "Focused", "Busy", "Tired", "Anxious", "Sick", "Family",
)
_DEFAULT_BY_KEY = {tag.casefold(): tag for tag in DEFAULT_MOOD_TAGS}

# The optional fields each kind of check-in may carry; note and tags fit every kind.
KIND_FIELDS: dict[str, tuple[str, ...]] = {
    "day": ("energy", "stress"),
    "week": ("workload", "highlight"),
    "month": ("progress", "word"),
}

# How far past the server's UTC date a period may start, so a caller whose
# local day is already tomorrow can still log it.
FUTURE_SKEW = timedelta(days=1)


def _normalize_tags(tags: list[str]) -> list[str]:
    """Trim, drop blanks and case-insensitive repeats, and spell default tags the default way."""
    seen: dict[str, str] = {}
    for tag in tags:
        name = tag.strip()
        key = name.casefold()
        if name and key not in seen:
            seen[key] = _DEFAULT_BY_KEY.get(key, name)
    if len(seen) > MOOD_TAGS_PER_CHECKIN_MAX:
        raise ValueError(f"A check-in may have at most {MOOD_TAGS_PER_CHECKIN_MAX} tags.")
    return list(seen.values())


MoodTagList = Annotated[
    list[Annotated[str, Field(max_length=TAG_NAME_MAX)]],
    Field(max_length=MOOD_TAGS_PER_CHECKIN_MAX * 2),
    AfterValidator(_normalize_tags),
]
Score = Annotated[int, Field(ge=1, le=5)]


class MoodCheckinSave(BaseModel):
    """A whole check-in. Saving replaces the period's check-in, so a field left out is cleared."""

    period_kind: MoodPeriodKind
    # The local day; the Monday of a week; the 1st of a month.
    period_start: date
    mood: Score
    energy: Score | None = None
    stress: Score | None = None
    workload: int | None = Field(None, ge=0, le=2)
    highlight: str | None = Field(None, max_length=MOOD_HIGHLIGHT_MAX)
    progress: Score | None = None
    word: str | None = Field(None, max_length=MOOD_WORD_MAX)
    note: str | None = Field(None, max_length=MOOD_NOTE_MAX)
    tags: MoodTagList = []

    @model_validator(mode="after")
    def _check_period(self):
        start = self.period_start
        if self.period_kind == "week" and start.weekday() != 0:
            raise ValueError("A week check-in's period_start must be a Monday.")
        if self.period_kind == "month" and start.day != 1:
            raise ValueError("A month check-in's period_start must be the 1st of the month.")
        if start > datetime.now(UTC).date() + FUTURE_SKEW:
            raise ValueError("period_start must not be in the future.")
        for kind, fields in KIND_FIELDS.items():
            if kind != self.period_kind:
                misplaced = [f for f in fields if getattr(self, f) is not None]
                if misplaced:
                    raise ValueError(f"{', '.join(misplaced)} only applies to {kind} check-ins.")
        return self


class MoodCheckin(BaseModel):
    id: int
    period_kind: MoodPeriodKind
    period_start: date
    mood: int
    energy: int | None = None
    stress: int | None = None
    workload: int | None = None
    highlight: str | None = None
    progress: int | None = None
    word: str | None = None
    note: str | None = None
    tags: list[str] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MoodTags(BaseModel):
    # The built-in list, in picker order.
    defaults: list[str]
    # Tags the user has added on their own check-ins, most recently used first.
    custom: list[str]


# ── Profile settings ─────────────────────────────────────────────────────────

MoodRemind = Literal["shutoff", "shutoff+30", "off"]


class MoodCheckinSettings(BaseModel):
    """Which check-ins the dashboard nudges for, and when. Daily is on by default, weekly and monthly opt-in."""

    daily: bool = True
    weekly: bool = False
    monthly: bool = False
    # Nudge at "Call it a day", 30 minutes after it, or not at all.
    remind: MoodRemind = "shutoff"
    show_in_reports: bool = True


class MoodCheckinSettingsUpdate(BaseModel):
    """Only the fields given change; null restores a field's default."""

    daily: bool | None = None
    weekly: bool | None = None
    monthly: bool | None = None
    remind: MoodRemind | None = None
    show_in_reports: bool | None = None


def merge_mood_settings(stored: dict | None, changes: dict) -> dict:
    """Apply a MoodCheckinSettingsUpdate's set fields to the stored overrides.

    Only fields the user has set are stored, so a field never touched follows
    its default, and a NULL column means "all defaults".
    """
    merged = {**(stored or {}), **changes}
    return {k: v for k, v in merged.items() if v is not None}


def resolve_mood_settings(stored: dict | MoodCheckinSettings | None) -> MoodCheckinSettings:
    if isinstance(stored, MoodCheckinSettings):
        return stored
    known = {k: v for k, v in (stored or {}).items() if k in MoodCheckinSettings.model_fields}
    return MoodCheckinSettings(**known)


# The profile's stored overrides, read back with every default filled in.
ResolvedMoodSettings = Annotated[MoodCheckinSettings, BeforeValidator(resolve_mood_settings)]
