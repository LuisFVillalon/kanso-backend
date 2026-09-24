from sqlalchemy.orm import Session


def get_owned[ModelT](db: Session, model: type[ModelT], record_id: int, user_id: str) -> ModelT | None:
    """Fetch a row by primary key, scoped to its owning user.

    Every user-owned table (tasks, notes, tags, habits, ...) is looked up the
    same way: match the id, then confirm user_id ownership. Centralizing it
    here means that check can't drift or get skipped in a new crud module.
    """
    return db.query(model).filter(model.id == record_id, model.user_id == user_id).first()
