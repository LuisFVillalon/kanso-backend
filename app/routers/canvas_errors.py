"""
Mapping Canvas failures to HTTP errors for the /canvas routes.

Canvas-specific failures return a stable code as the detail (see
core/canvas_client.py), so the frontend can show the right message.
"""

import logging

from fastapi import HTTPException, status

from app.core import crypto
from app.core.canvas_client import CanvasError, CanvasRateLimited, CanvasTokenInvalid, CanvasUnavailable
from app.crud.canvas_crud import CanvasAccountChanged
from app.crud.canvas_data_crud import CanvasCourseHidden, CanvasNotConnected, CanvasNotSupported

log = logging.getLogger(__name__)


def canvas_http_error(exc: CanvasError | crypto.EncryptionKeyError) -> HTTPException:
    """The HTTP error for a failure while connecting or testing the connection."""
    if isinstance(exc, crypto.EncryptionKeyError):
        log.error("CANVAS | %s", exc)
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="canvas_not_configured")
    if isinstance(exc, CanvasUnavailable):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.code)
    if isinstance(exc, CanvasAccountChanged):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code)
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.code)


def not_connected() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="canvas_not_connected")


def read_http_error(exc: CanvasError | crypto.EncryptionKeyError) -> HTTPException:
    """The HTTP error for a failure while reading Canvas data: a rejected token asks the user to reconnect."""
    if isinstance(exc, CanvasNotConnected):
        return not_connected()
    if isinstance(exc, CanvasTokenInvalid):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code)
    if isinstance(exc, CanvasRateLimited):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="canvas_busy", headers={"Retry-After": "30"}
        )
    if isinstance(exc, CanvasCourseHidden | CanvasNotSupported):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.code)
    return canvas_http_error(exc)
