"""
In-process cache of Canvas data, per user and per request.

kanso runs on one small Fly machine, so a dict in memory is enough (like
core/rate_limit.py); a restart just means the next panel load calls Canvas.
Entries are the normalized shapes the read routes return, before hidden
courses are filtered out, so changing "Courses to show" applies at once.
Each key starts with the connection (user, school and Canvas account), so a
replaced token for another account never sees the old account's data; the
router also evicts a user's entries whenever the connection is replaced or
removed. The cache holds at most MAX_ENTRIES, dropping the least recently
used.
"""

import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

MAX_ENTRIES = 2000


@dataclass(frozen=True)
class Entry:
    fetched_at: datetime
    data: Any


_entries: OrderedDict[tuple, Entry] = OrderedDict()
_lock = threading.Lock()


def get(key: tuple) -> Entry | None:
    with _lock:
        entry = _entries.get(key)
        if entry is not None:
            _entries.move_to_end(key)
        return entry


def put(key: tuple, data: Any, fetched_at: datetime) -> Entry:
    entry = Entry(fetched_at=fetched_at, data=data)
    with _lock:
        _entries[key] = entry
        _entries.move_to_end(key)
        while len(_entries) > MAX_ENTRIES:
            _entries.popitem(last=False)
    return entry


def evict_user(user_id: str) -> None:
    """Drop every entry for user_id. Keys start with the user's id."""
    with _lock:
        for key in [k for k in _entries if k[0] == user_id]:
            del _entries[key]


def clear() -> None:
    with _lock:
        _entries.clear()
