from sqlalchemy import JSON, BigInteger, Column, DateTime, LargeBinary, SmallInteger, String, Text, func

from app.database.database import Base


class CanvasConnection(Base):
    """A user's Canvas LMS connection and the school it belongs to. One per user.

    connection_type is "token" (a personal access token for the Canvas API)
    or "feed" (the user's private calendar feed link, core/canvas_feed.py).
    Either secret, the token or the whole feed URL, is stored only as AES-GCM
    ciphertext (core/crypto.py) in the token_* columns; the key lives outside
    the database. token_last4 (the feed code's last 4 for a feed) is the only
    secret-derived value the API ever returns.
    """

    __tablename__ = "canvas_connections"

    user_id = Column(String(36), primary_key=True)
    connection_type = Column(String(16), nullable=False, default="token", server_default="token")
    # Normalized "https://host", see core/canvas_client.normalize_base_url.
    base_url = Column(Text, nullable=False)
    token_ciphertext = Column(LargeBinary, nullable=False)
    token_nonce = Column(LargeBinary, nullable=False)
    # Which CANVAS_TOKEN_KEY version encrypted it, for key rotation.
    key_version = Column(SmallInteger, nullable=False)
    token_last4 = Column(String(4), nullable=False)
    # The token owner's Canvas account, from GET /users/self when connecting.
    # NULL for a feed, which doesn't say whose it is.
    canvas_user_id = Column(BigInteger, nullable=True)
    canvas_user_name = Column(Text, nullable=True)
    # NULL when Canvas didn't say (or the token never expires).
    token_expires_at = Column(DateTime(timezone=True), nullable=True)
    # "active"; "invalid" once Canvas rejects the token (401) or no longer
    # knows the feed (it was reset); "error" while
    # Canvas can't be reached.
    status = Column(String(16), nullable=False, default="active")
    # Canvas course ids the user unticked under "Courses to show".
    hidden_course_ids = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_synced_at = Column(DateTime(timezone=True), nullable=True)
    last_error_at = Column(DateTime(timezone=True), nullable=True)
