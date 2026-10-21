"""Upper bounds on client-supplied fields.

Generous enough for real use, small enough that one request can't park
megabytes of junk in the database. Notes and drawings are the large ones:
note images are stored inline as base64 data URLs (the editor caps each image
at 2 MB), and the doodle canvas is saved as a PNG data URL.
"""

TITLE_MAX = 500
TEXT_MAX = 10_000
SHORT_MAX = 100
TAG_NAME_MAX = 50
COLOR_MAX = 32
TAGS_PER_ITEM_MAX = 50
NOTE_CONTENT_MAX = 10_000_000
DRAWING_DATA_URL_MAX = 5_000_000
# Reminder lead times, in minutes before a task is due.
REMINDER_MINUTES_MAX = 7 * 24 * 60
# Notification ids per mark-read or dismiss request.
NOTIFICATION_IDS_MAX = 200
# Mood check-in text fields and tags.
MOOD_NOTE_MAX = 280
MOOD_HIGHLIGHT_MAX = 120
MOOD_WORD_MAX = 24
MOOD_TAGS_PER_CHECKIN_MAX = 20
# Linked tasks saved under one parent per request.
LINKED_TASKS_MAX = 100
# Canvas: the pasted school address, the access token, and the courses
# unticked under "Courses to show".
CANVAS_URL_MAX = 2048
CANVAS_TOKEN_MAX = 512
CANVAS_HIDDEN_COURSES_MAX = 500
