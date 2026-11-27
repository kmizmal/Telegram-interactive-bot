"""SQLite schema for the bot.

The schema intentionally uses only stdlib ``sqlite3``-compatible DDL so a fresh
install can be initialized without SQLAlchemy or an ORM model layer.
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user (
    id INTEGER NOT NULL PRIMARY KEY,
    user_id INTEGER,
    first_name VARCHAR(64),
    last_name VARCHAR(64),
    username VARCHAR(64),
    is_premium BOOLEAN,
    updated_at DATETIME,
    message_thread_id INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_user_user_id ON user (user_id);
CREATE INDEX IF NOT EXISTS ix_user_thread ON user (message_thread_id);

CREATE TABLE IF NOT EXISTS message_map (
    id INTEGER NOT NULL PRIMARY KEY,
    user_chat_message_id INTEGER,
    group_chat_message_id INTEGER,
    user_id INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_message_map_user_chat ON message_map (user_chat_message_id);
CREATE INDEX IF NOT EXISTS ix_message_map_group_chat ON message_map (group_chat_message_id);
CREATE INDEX IF NOT EXISTS ix_message_map_user ON message_map (user_id);

CREATE TABLE IF NOT EXISTS formn_status (
    id INTEGER NOT NULL PRIMARY KEY,
    chat_id INTEGER,
    message_thread_id INTEGER,
    status VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS ix_formn_status_thread ON formn_status (message_thread_id);

CREATE TABLE IF NOT EXISTS media_group_message (
    id INTEGER NOT NULL PRIMARY KEY,
    chat_id INTEGER,
    message_id INTEGER,
    media_group_id INTEGER,
    is_header BOOLEAN,
    caption_html VARCHAR(65536)
);
CREATE INDEX IF NOT EXISTS ix_media_group_lookup ON media_group_message (media_group_id, chat_id);

CREATE TABLE IF NOT EXISTS user_state (
    user_id INTEGER PRIMARY KEY,
    is_human INTEGER NOT NULL DEFAULT 0,
    vcode TEXT,
    is_human_error_time REAL
);
"""
