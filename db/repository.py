"""Synchronous data-access functions using the stdlib ``sqlite3`` driver.

Handlers call these through :func:`db.executor.run_db` so database work does
not block the asyncio event loop. Every function returns a plain DTO or a
scalar; no sqlite3 connection or row crosses a thread boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from db.database import read_connection, transaction


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


@dataclass(frozen=True)
class UserStateDTO:
    is_human: bool = False
    vcode: str | None = None
    is_human_error_time: float | None = None


def _user_dto(row) -> UserDTO:
    premium = row["is_premium"]
    return UserDTO(
        user_id=row["user_id"],
        message_thread_id=row["message_thread_id"] or 0,
        first_name=row["first_name"],
        last_name=row["last_name"],
        username=row["username"],
        is_premium=bool(premium) if premium is not None else None,
    )


def _state_dto(row) -> UserStateDTO:
    if row is None:
        return UserStateDTO()
    return UserStateDTO(
        is_human=bool(row["is_human"]),
        vcode=row["vcode"],
        is_human_error_time=row["is_human_error_time"],
    )


def get_or_create_user(
    user_id,
    first_name=None,
    last_name=None,
    username=None,
    is_premium=None,
) -> UserDTO:
    """Look up a user, creating it only when the fast read misses."""
    with read_connection() as connection:
        row = connection.execute(
            "SELECT user_id, message_thread_id, first_name, last_name, username, is_premium "
            "FROM user WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    if row is not None:
        return _user_dto(row)

    with transaction() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO user
                (user_id, first_name, last_name, username, is_premium, message_thread_id)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (user_id, first_name, last_name, username, is_premium),
        )
        row = connection.execute(
            "SELECT user_id, message_thread_id, first_name, last_name, username, is_premium "
            "FROM user WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"failed to create user {user_id}")
        return _user_dto(row)


def get_user_by_user_id(user_id) -> UserDTO | None:
    with read_connection() as connection:
        row = connection.execute(
            "SELECT user_id, message_thread_id, first_name, last_name, username, is_premium "
            "FROM user WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return _user_dto(row) if row else None


def get_user_by_thread(message_thread_id) -> UserDTO | None:
    with read_connection() as connection:
        row = connection.execute(
            "SELECT user_id, message_thread_id, first_name, last_name, username, is_premium "
            "FROM user WHERE message_thread_id=? ORDER BY id DESC LIMIT 1",
            (message_thread_id,),
        ).fetchone()
        return _user_dto(row) if row else None


def set_user_thread(user_id, message_thread_id) -> None:
    with transaction() as connection:
        connection.execute(
            "UPDATE user SET message_thread_id=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE user_id=?",
            (message_thread_id, user_id),
        )


def set_user_thread_and_create_map(
    user_id,
    message_thread_id,
    user_chat_message_id,
    group_chat_message_id,
) -> None:
    """Persist the new forum topic and its first message map in one commit."""
    with transaction() as connection:
        connection.execute(
            "UPDATE user SET message_thread_id=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE user_id=?",
            (message_thread_id, user_id),
        )
        connection.execute(
            """
            INSERT INTO message_map
                (user_chat_message_id, group_chat_message_id, user_id, created_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (user_chat_message_id, group_chat_message_id, user_id),
        )


def get_user_state(user_id) -> UserStateDTO:
    with read_connection() as connection:
        row = connection.execute(
            "SELECT is_human, vcode, is_human_error_time FROM user_state WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return _state_dto(row)


def set_user_vcode(user_id, vcode) -> None:
    with transaction() as connection:
        connection.execute(
            """
            INSERT INTO user_state (user_id, is_human, vcode, is_human_error_time)
            VALUES (?, 0, ?, NULL)
            ON CONFLICT(user_id) DO UPDATE SET vcode=excluded.vcode
            """,
            (user_id, vcode),
        )


def mark_user_human(user_id) -> None:
    with transaction() as connection:
        connection.execute(
            """
            INSERT INTO user_state (user_id, is_human, vcode, is_human_error_time)
            VALUES (?, 1, NULL, NULL)
            ON CONFLICT(user_id) DO UPDATE SET is_human=1
            """,
            (user_id,),
        )


def set_user_captcha_error(user_id, timestamp) -> None:
    with transaction() as connection:
        connection.execute(
            """
            INSERT INTO user_state (user_id, is_human, vcode, is_human_error_time)
            VALUES (?, 0, NULL, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                is_human_error_time=excluded.is_human_error_time
            """,
            (user_id, timestamp),
        )


def get_formn_status(message_thread_id) -> str | None:
    with read_connection() as connection:
        row = connection.execute(
            "SELECT status FROM formn_status WHERE message_thread_id=? "
            "ORDER BY id DESC LIMIT 1",
            (message_thread_id,),
        ).fetchone()
        return row["status"] if row else None


def set_formn_status(message_thread_id, status, chat_id=None) -> None:
    with transaction() as connection:
        row = connection.execute(
            "SELECT id FROM formn_status WHERE message_thread_id=? "
            "ORDER BY id DESC LIMIT 1",
            (message_thread_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO formn_status (chat_id, message_thread_id, status) "
                "VALUES (?, ?, ?)",
                (chat_id, message_thread_id, status),
            )
        else:
            connection.execute(
                "UPDATE formn_status SET status=? WHERE id=?",
                (status, row["id"]),
            )


def find_message_map_by_user_chat(user_id, message_id) -> int | None:
    """Resolve a private-chat message ID within its owning user's chat."""
    with read_connection() as connection:
        row = connection.execute(
            """
            SELECT group_chat_message_id FROM message_map
            WHERE user_id=? AND user_chat_message_id=?
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, message_id),
        ).fetchone()
        return row["group_chat_message_id"] if row else None


def find_message_map_by_group_chat(message_id) -> int | None:
    with read_connection() as connection:
        row = connection.execute(
            """
            SELECT user_chat_message_id FROM message_map
            WHERE group_chat_message_id=?
            ORDER BY id DESC LIMIT 1
            """,
            (message_id,),
        ).fetchone()
        return row["user_chat_message_id"] if row else None


def create_message_map(user_chat_message_id, group_chat_message_id, user_id) -> None:
    with transaction() as connection:
        connection.execute(
            """
            INSERT INTO message_map
                (user_chat_message_id, group_chat_message_id, user_id, created_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (user_chat_message_id, group_chat_message_id, user_id),
        )


def create_message_maps_batch(rows) -> None:
    """Insert all (user_chat_id, group_chat_id, user_id) tuples in one commit."""
    values = [
        (user_chat_id, group_chat_id, user_id)
        for user_chat_id, group_chat_id, user_id in rows
    ]
    if not values:
        return
    with transaction() as connection:
        connection.executemany(
            """
            INSERT INTO message_map
                (user_chat_message_id, group_chat_message_id, user_id, created_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """,
            values,
        )


def add_media_group_message(chat_id, message_id, media_group_id, caption_html) -> bool:
    """Insert a media-group row and return whether it is the group's first row."""
    with transaction() as connection:
        existing = connection.execute(
            "SELECT 1 FROM media_group_message "
            "WHERE media_group_id=? AND chat_id=? LIMIT 1",
            (media_group_id, chat_id),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO media_group_message
                (chat_id, message_id, media_group_id, is_header, caption_html)
            VALUES (?, ?, ?, 0, ?)
            """,
            (chat_id, message_id, media_group_id, caption_html),
        )
        return existing is None


def get_media_group_messages(media_group_id, chat_id) -> list[MediaGroupDTO]:
    with read_connection() as connection:
        rows = connection.execute(
            """
            SELECT message_id, caption_html FROM media_group_message
            WHERE media_group_id=? AND chat_id=?
            ORDER BY id
            """,
            (media_group_id, chat_id),
        ).fetchall()
        return [
            MediaGroupDTO(message_id=row["message_id"], caption_html=row["caption_html"])
            for row in rows
        ]


def delete_media_group_messages(media_group_id, chat_id) -> None:
    with transaction() as connection:
        connection.execute(
            "DELETE FROM media_group_message WHERE media_group_id=? AND chat_id=?",
            (media_group_id, chat_id),
        )


def get_users_page(limit, offset) -> list[UserDTO]:
    """Offset pagination; kept for compatibility with small callers."""
    with read_connection() as connection:
        rows = connection.execute(
            "SELECT user_id, message_thread_id, first_name, last_name, username, is_premium "
            "FROM user ORDER BY id LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [_user_dto(row) for row in rows]


def get_users_page_after(limit, after_user_id=0) -> list[UserDTO]:
    """Keyset pagination: return the next page after ``after_user_id``.

    ``user_id`` has a unique index and is the stable ordering key, so this
    avoids the O(n²) rescans of ``LIMIT/OFFSET`` during a broadcast.
    """
    with read_connection() as connection:
        rows = connection.execute(
            "SELECT user_id, message_thread_id, first_name, last_name, username, is_premium "
            "FROM user WHERE user_id > ? ORDER BY user_id LIMIT ?",
            (after_user_id, limit),
        ).fetchall()
        return [_user_dto(row) for row in rows]


def get_all_users() -> list[UserDTO]:
    with read_connection() as connection:
        rows = connection.execute(
            "SELECT user_id, message_thread_id, first_name, last_name, username, is_premium "
            "FROM user ORDER BY id"
        ).fetchall()
        return [_user_dto(row) for row in rows]


def get_message_ids_for_user(user_id) -> list[int]:
    with read_connection() as connection:
        rows = connection.execute(
            "SELECT user_chat_message_id FROM message_map "
            "WHERE user_id=? ORDER BY id",
            (user_id,),
        ).fetchall()
        return [row[0] for row in rows]


def _chunks(values, size=900):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def delete_message_maps(message_ids) -> None:
    if not message_ids:
        return
    with transaction() as connection:
        for chunk in _chunks(list(message_ids)):
            placeholders = ",".join("?" for _ in chunk)
            connection.execute(
                f"DELETE FROM message_map WHERE user_chat_message_id IN ({placeholders})",
                tuple(chunk),
            )


def _format_cutoff(cutoff: datetime) -> str:
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


def count_old_message_maps(cutoff: datetime) -> int:
    with read_connection() as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM message_map WHERE created_at < ?",
            (_format_cutoff(cutoff),),
        ).fetchone()
        return int(row[0])


def delete_old_message_maps(cutoff: datetime) -> int:
    with transaction() as connection:
        cursor = connection.execute(
            "DELETE FROM message_map WHERE created_at < ?",
            (_format_cutoff(cutoff),),
        )
        return int(cursor.rowcount)


def delete_message_maps_for_user(user_id, message_ids=None) -> None:
    """Delete a user's maps, optionally only the listed private-chat message IDs."""
    with transaction() as connection:
        if message_ids is None:
            connection.execute("DELETE FROM message_map WHERE user_id=?", (user_id,))
            return
        if not message_ids:
            return
        for chunk in _chunks(list(message_ids)):
            placeholders = ",".join("?" for _ in chunk)
            connection.execute(
                f"DELETE FROM message_map WHERE user_id=? "
                f"AND user_chat_message_id IN ({placeholders})",
                (user_id, *tuple(chunk)),
            )
