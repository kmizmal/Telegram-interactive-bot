"""Synchronous data-access functions.

Handlers call these through :func:`db.executor.run_db` so that SQLAlchemy work
never blocks the asyncio event loop and no ORM instance crosses a thread
boundary: every function returns a plain DTO or a scalar.
"""

from dataclasses import dataclass
from datetime import datetime

from db.database import session_scope
from db.model import FormnStatus, MediaGroupMesssage, MessageMap, User


@dataclass(frozen=True)
class UserDTO:
    user_id: int
    message_thread_id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    is_premium: bool | None = None


@dataclass(frozen=True)
class MediaGroupDTO:
    message_id: int
    caption_html: str | None


def _user_dto(row: User) -> UserDTO:
    return UserDTO(
        user_id=row.user_id,
        message_thread_id=row.message_thread_id or 0,
        first_name=row.first_name,
        last_name=row.last_name,
        username=row.username,
        is_premium=row.is_premium,
    )


def get_or_create_user(
    user_id,
    first_name=None,
    last_name=None,
    username=None,
    is_premium=None,
) -> UserDTO:
    """Look up a user and create it in the same short transaction if absent."""
    with session_scope() as session:
        row = session.query(User).filter(User.user_id == user_id).one_or_none()
        if row is None:
            row = User(
                user_id=user_id,
                first_name=first_name,
                last_name=last_name,
                username=username,
                is_premium=is_premium,
                message_thread_id=0,
            )
            session.add(row)
            session.flush()
        return _user_dto(row)


def get_user_by_user_id(user_id) -> UserDTO | None:
    with session_scope() as session:
        row = session.query(User).filter(User.user_id == user_id).one_or_none()
        return _user_dto(row) if row else None


def get_user_by_thread(message_thread_id) -> UserDTO | None:
    with session_scope() as session:
        row = (
            session.query(User)
            .filter(User.message_thread_id == message_thread_id)
            .one_or_none()
        )
        return _user_dto(row) if row else None


def set_user_thread(user_id, message_thread_id) -> None:
    with session_scope() as session:
        row = session.query(User).filter(User.user_id == user_id).one_or_none()
        if row is not None:
            row.message_thread_id = message_thread_id


def set_user_thread_and_create_map(
    user_id,
    message_thread_id,
    user_chat_message_id,
    group_chat_message_id,
) -> None:
    """Persist the new forum topic and its first message map in one commit."""
    with session_scope() as session:
        row = session.query(User).filter(User.user_id == user_id).one_or_none()
        if row is not None:
            row.message_thread_id = message_thread_id
        session.add(
            MessageMap(
                user_chat_message_id=user_chat_message_id,
                group_chat_message_id=group_chat_message_id,
                user_id=user_id,
            )
        )


def get_formn_status(message_thread_id) -> str | None:
    with session_scope() as session:
        row = (
            session.query(FormnStatus)
            .filter(FormnStatus.message_thread_id == message_thread_id)
            .one_or_none()
        )
        return row.status if row else None


def set_formn_status(message_thread_id, status, chat_id=None) -> None:
    with session_scope() as session:
        row = (
            session.query(FormnStatus)
            .filter(FormnStatus.message_thread_id == message_thread_id)
            .one_or_none()
        )
        if row is None:
            row = FormnStatus(
                message_thread_id=message_thread_id, chat_id=chat_id, status=status
            )
            session.add(row)
        else:
            row.status = status


def find_message_map_by_user_chat(message_id) -> int | None:
    with session_scope() as session:
        row = (
            session.query(MessageMap)
            .filter(MessageMap.user_chat_message_id == message_id)
            .one_or_none()
        )
        return row.group_chat_message_id if row else None


def find_message_map_by_group_chat(message_id) -> int | None:
    with session_scope() as session:
        row = (
            session.query(MessageMap)
            .filter(MessageMap.group_chat_message_id == message_id)
            .one_or_none()
        )
        return row.user_chat_message_id if row else None


def create_message_map(user_chat_message_id, group_chat_message_id, user_id) -> None:
    with session_scope() as session:
        session.add(
            MessageMap(
                user_chat_message_id=user_chat_message_id,
                group_chat_message_id=group_chat_message_id,
                user_id=user_id,
            )
        )


def create_message_maps_batch(rows) -> None:
    """Insert all (user_chat_id, group_chat_id, user_id) tuples in one commit."""
    with session_scope() as session:
        session.add_all(
            MessageMap(
                user_chat_message_id=user_chat_id,
                group_chat_message_id=group_chat_id,
                user_id=user_id,
            )
            for user_chat_id, group_chat_id, user_id in rows
        )


def add_media_group_message(chat_id, message_id, media_group_id, caption_html) -> None:
    with session_scope() as session:
        session.add(
            MediaGroupMesssage(
                chat_id=chat_id,
                message_id=message_id,
                media_group_id=media_group_id,
                is_header=False,
                caption_html=caption_html,
            )
        )


def get_media_group_messages(media_group_id, chat_id) -> list[MediaGroupDTO]:
    with session_scope() as session:
        rows = (
            session.query(MediaGroupMesssage)
            .filter(
                MediaGroupMesssage.media_group_id == media_group_id,
                MediaGroupMesssage.chat_id == chat_id,
            )
            .all()
        )
        return [
            MediaGroupDTO(message_id=row.message_id, caption_html=row.caption_html)
            for row in rows
        ]


def delete_media_group_messages(media_group_id, chat_id) -> None:
    with session_scope() as session:
        (
            session.query(MediaGroupMesssage)
            .filter(
                MediaGroupMesssage.media_group_id == media_group_id,
                MediaGroupMesssage.chat_id == chat_id,
            )
            .delete(synchronize_session=False)
        )


def get_all_users() -> list[UserDTO]:
    with session_scope() as session:
        return [_user_dto(row) for row in session.query(User).all()]


def get_message_ids_for_user(user_id) -> list[int]:
    with session_scope() as session:
        rows = (
            session.query(MessageMap.user_chat_message_id)
            .filter(MessageMap.user_id == user_id)
            .all()
        )
        return [row[0] for row in rows]


def delete_message_maps(message_ids) -> None:
    with session_scope() as session:
        (
            session.query(MessageMap)
            .filter(MessageMap.user_chat_message_id.in_(message_ids))
            .delete(synchronize_session=False)
        )


def count_old_message_maps(cutoff: datetime) -> int:
    with session_scope() as session:
        return (
            session.query(MessageMap)
            .filter(MessageMap.created_at < cutoff)
            .count()
        )


def delete_old_message_maps(cutoff: datetime) -> int:
    with session_scope() as session:
        return (
            session.query(MessageMap)
            .filter(MessageMap.created_at < cutoff)
            .delete(synchronize_session=False)
        )


def delete_message_maps_for_user(user_id) -> None:
    with session_scope() as session:
        (
            session.query(MessageMap)
            .filter(MessageMap.user_id == user_id)
            .delete(synchronize_session=False)
        )
