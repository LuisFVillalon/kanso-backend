from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import relationship

from app.database.database import Base

google_calendar_event_tags = Table(
    "google_calendar_event_tags",
    Base.metadata,
    Column("event_id", Integer, ForeignKey("google_calendar_events.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)

google_event_override_tags = Table(
    "google_event_override_tags",
    Base.metadata,
    Column("override_id", Integer, ForeignKey("google_event_overrides.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class GoogleCalendarConnection(Base):
    """A user's read-only Google Calendar connection. One per user.

    The refresh token is stored only as AES-GCM ciphertext (core/crypto.py)
    and is never returned or logged. Access tokens are fetched per sync and
    never stored.
    """

    __tablename__ = "google_calendar_connections"

    user_id = Column(String(36), primary_key=True)
    token_ciphertext = Column(LargeBinary, nullable=False)
    token_nonce = Column(LargeBinary, nullable=False)
    # Which CANVAS_TOKEN_KEY version encrypted it, for key rotation.
    key_version = Column(SmallInteger, nullable=False)
    # The Google account, from its primary calendar's id; NULL until the first sync.
    email = Column(Text, nullable=True)
    # When a sync last started, successful or not: paces the refreshes and
    # keeps two from running at once (crud/google_calendar_crud.py).
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    last_synced_at = Column(DateTime(timezone=True), nullable=True)
    # The last sync's stable error code (core/google_calendar.py); NULL if it worked.
    last_error = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class GoogleOAuthState(Base):
    """The OAuth state of a user's connect in progress. One per user, used once."""

    __tablename__ = "google_oauth_states"

    user_id = Column(String(36), primary_key=True)
    # SHA-256 of the state, so a database dump can't finish anyone's connect.
    state_hash = Column(String(64), nullable=False, unique=True)
    # The allowed frontend origin the callback sends the browser back to.
    return_origin = Column(Text, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)


class GoogleCalendar(Base):
    """One calendar in a user's Google account."""

    __tablename__ = "google_calendars"
    __table_args__ = (UniqueConstraint("user_id", "google_calendar_id", name="uq_google_calendars_user_calendar"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String(36), ForeignKey("google_calendar_connections.user_id", ondelete="CASCADE"), nullable=False
    )
    google_calendar_id = Column(Text, nullable=False)
    name = Column(Text, nullable=False)
    color = Column(String(32), nullable=True)
    # IANA zone for all-day events, which don't name one.
    time_zone = Column(String(64), nullable=False, default="UTC")
    # Whether its events are imported. Deselected calendars keep their rows, so tags survive.
    selected = Column(Boolean, nullable=False, default=False)
    # The tag its events take when they have none of their own.
    default_tag_id = Column(Integer, ForeignKey("tags.id", ondelete="SET NULL"), nullable=True)
    # Google's incremental sync position; NULL forces a full sync.
    sync_token = Column(Text, nullable=True)
    # Events starting at or after this instant aren't imported yet.
    synced_through = Column(DateTime(timezone=True), nullable=True)

    default_tag = relationship("Tag")


class GoogleCalendarEvent(Base):
    """One imported event or one instance of a repeating event.

    Occurrences show it with event_id = -id, which can't collide with a kanso
    event's id (crud/event_occurrence_crud.py). Kanso can edit, archive and
    delete it, and set its tags, all in kanso only (crud/google_event_crud.py):
    nothing is written to Google.
    """

    __tablename__ = "google_calendar_events"
    __table_args__ = (
        UniqueConstraint("calendar_id", "google_event_id", name="uq_google_calendar_events_calendar_event"),
        Index("ix_google_calendar_events_user_start", "user_id", "start_date"),
        # "This and following" and "all" changes find a repeating event's instances.
        Index(
            "ix_google_calendar_events_user_series", "user_id", "recurring_event_id",
            postgresql_where=text("recurring_event_id IS NOT NULL"),
        ),
        # The expiry purge finds rows deleted past retention by deleted_at alone.
        Index("ix_google_calendar_events_deleted_at", "deleted_at", postgresql_where=text("deleted_at IS NOT NULL")),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(String(36), nullable=False)
    calendar_id = Column(Integer, ForeignKey("google_calendars.id", ondelete="CASCADE"), nullable=False)
    google_event_id = Column(Text, nullable=False)
    # The repeating event this is an instance of, if any.
    recurring_event_id = Column(Text, nullable=True)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    all_day = Column(Boolean, nullable=False, default=False)
    # The days it covers, inclusive, in time_zone.
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    time_zone = Column(String(64), nullable=False)
    # The event's page in Google Calendar.
    html_link = Column(Text, nullable=True)
    # True when the user chose this event's tags, even none; False takes the calendar's default.
    tags_overridden = Column(Boolean, nullable=False, default=False)
    # False when the user declined it or it's marked "Show as free": shown, but not busy time.
    busy = Column(Boolean, nullable=False, default=True)
    # Where Google puts it, whatever kanso edits: "this and following" counts from it.
    google_starts_at = Column(DateTime(timezone=True), nullable=False)
    # When kanso last edited its details (title through time_zone above). A
    # sync keeps them unless Google changed the event after that; NULL takes Google's.
    edited_at = Column(DateTime(timezone=True), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    # The change that archived it with other instances of its repeating event; NULL if archived on its own.
    archived_by_id = Column(Integer, ForeignKey("google_event_overrides.id", ondelete="SET NULL"), nullable=True)
    # Set while it is in the trash.
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    # The change that deleted it with other instances; NULL if deleted on its own.
    deleted_by_id = Column(Integer, ForeignKey("google_event_overrides.id", ondelete="SET NULL"), nullable=True)
    # Deleted for good: purged from the trash or past its retention. The row
    # stays so a sync doesn't bring the event back.
    cancelled = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    calendar = relationship("GoogleCalendar")
    tags = relationship("Tag", secondary=google_calendar_event_tags)

    @property
    def resolved_tags(self) -> list:
        """The event's own tags, else its calendar's default, else none."""
        if self.tags_overridden:
            return list(self.tags)
        return [self.calendar.default_tag] if self.calendar.default_tag else []



class GoogleEventOverride(Base):
    """A kanso change to an instance of a repeating Google event and the ones
    after it ("this and following"), or to all of them.

    The instances already imported change when it's made; this keeps it for
    the ones later syncs import, which take every override that reaches
    them, oldest first, so the latest change wins (crud/google_event_crud.py).
    """

    __tablename__ = "google_event_overrides"
    __table_args__ = (Index("ix_google_event_overrides_user_series", "user_id", "recurring_event_id"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String(36), ForeignKey("google_calendar_connections.user_id", ondelete="CASCADE"), nullable=False
    )
    # Google's id for the repeating event.
    recurring_event_id = Column(Text, nullable=False)
    # Reaches the instances Google starts at or after this; NULL reaches all of them.
    starts_from = Column(DateTime(timezone=True), nullable=True)
    # "edit", "tags", "archive", "unarchive" or "delete".
    kind = Column(String(16), nullable=False)
    set_at = Column(DateTime(timezone=True), nullable=False)
    # An edit's fields, as EventUpdate names them (times as "HH:MM:SS").
    changes = Column(JSON, nullable=True)
    # Tags: True returns the instances to their calendar's default tag; tags is then empty.
    use_default = Column(Boolean, nullable=False, default=False)
    # A delete: set while it is in the trash; cancelled once it's deleted for good.
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    cancelled = Column(Boolean, nullable=False, default=False)

    tags = relationship("Tag", secondary=google_event_override_tags)
