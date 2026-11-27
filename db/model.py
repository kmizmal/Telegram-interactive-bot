from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, func

from .database import Base


class MediaGroupMesssage(Base):
    __tablename__ = "media_group_message"
    __table_args__ = (Index("ix_media_group_lookup", "media_group_id", "chat_id"),)
    id = Column(Integer, primary_key=True)
    chat_id = Column(Integer)
    message_id = Column(Integer)
    media_group_id = Column(Integer)
    is_header = Column(Boolean)
    caption_html = Column(String(1024 * 64))


class FormnStatus(Base):
    __tablename__ = "formn_status"
    __table_args__ = (Index("ix_formn_status_thread", "message_thread_id"),)
    id = Column(Integer, primary_key=True)
    chat_id = Column(Integer)
    message_thread_id = Column(Integer)
    status = Column(String(64))


class MessageMap(Base):
    __tablename__ = "message_map"
    __table_args__ = (
        Index("ix_message_map_user_chat", "user_chat_message_id"),
        Index("ix_message_map_group_chat", "group_chat_message_id"),
        Index("ix_message_map_user", "user_id"),
    )
    id = Column(Integer, primary_key=True)
    user_chat_message_id = Column(Integer)
    group_chat_message_id = Column(Integer)
    user_id = Column(Integer)
    # SQLite cannot ADD COLUMN with a non-constant default; old rows are
    # backfilled by db/migrations/perf_indexes.py.
    created_at = Column(DateTime(timezone=True), nullable=True, default=func.now())


class User(Base):
    __tablename__ = "user"
    __table_args__ = (Index("ix_user_thread", "message_thread_id"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, unique=True, index=True)
    first_name = Column(String(64))
    last_name = Column(String(64))
    username = Column(String(64))
    is_premium = Column(Boolean)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    message_thread_id = Column(Integer, default=0)
