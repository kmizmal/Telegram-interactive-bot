-- Performance indexes for Telegram-interactive-bot.
-- Safe to re-run: every statement is guarded by IF NOT EXISTS / IF EXISTS.
DROP INDEX IF EXISTS ix_user_id;
DROP INDEX IF EXISTS ix_message_map_id;
DROP INDEX IF EXISTS ix_formn_status_id;
DROP INDEX IF EXISTS ix_media_group_message_id;

CREATE INDEX IF NOT EXISTS ix_user_thread ON user (message_thread_id);
CREATE INDEX IF NOT EXISTS ix_message_map_user_chat ON message_map (user_chat_message_id);
CREATE INDEX IF NOT EXISTS ix_message_map_group_chat ON message_map (group_chat_message_id);
CREATE INDEX IF NOT EXISTS ix_message_map_user ON message_map (user_id);
CREATE INDEX IF NOT EXISTS ix_formn_status_thread ON formn_status (message_thread_id);
CREATE INDEX IF NOT EXISTS ix_media_group_lookup ON media_group_message (media_group_id, chat_id);
