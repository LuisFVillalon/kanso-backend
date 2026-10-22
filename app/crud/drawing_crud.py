from sqlalchemy.orm import Session

from app.crud.base import get_user_row
from app.models.drawing_model import Drawing
from app.schemas.drawing_schema import DrawingSave


def get_drawing(db: Session, user_id: str) -> Drawing | None:
    return get_user_row(db, Drawing, user_id)


def upsert_drawing(db: Session, user_id: str, body: DrawingSave) -> Drawing:
    drawing = get_drawing(db, user_id)
    if drawing is None:
        drawing = Drawing(user_id=user_id, image_data_url=body.image_data_url)
        db.add(drawing)
    else:
        drawing.image_data_url = body.image_data_url
    db.commit()
    db.refresh(drawing)
    return drawing


def delete_drawing(db: Session, user_id: str) -> Drawing | None:
    drawing = get_drawing(db, user_id)
    if drawing is None:
        return None
    db.delete(drawing)
    db.commit()
    return drawing
