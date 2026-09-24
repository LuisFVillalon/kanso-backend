from sqlalchemy import Column, DateTime, String, Text, func

from app.database.database import Base


class Drawing(Base):
    __tablename__ = "drawings"

    user_id = Column(String(36), primary_key=True)
    image_data_url = Column(Text, nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
