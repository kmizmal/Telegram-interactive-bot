import asyncio
import os
import time
from datetime import time as time_of_day

from telegram import Update
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)
from telegram.helpers import mention_html

from db import repository
from db.database import init_db
from db.executor import run_db

from . import (
    admin_group_id,
    admin_user_ids,
    app_name,
    bot_token,
    is_delete_topic_as_ban_forever,
    is_delete_user_messages,
    logger,
    disable_captcha,
    message_interval,
)
from .handlers.user import (
    callback_query_vcode,
    check_human,
    send_contact_card,
    start,
)
from .observability import stage_timer
from .services.cleanup import cleanup_old_message_maps, enabled as cleanup_enabled
from .services.update_processor import TokenBucket, configure_builder, user_lock

BROADCAST_MAX_PARALLEL = 5
BROADCAST_RATE_PER_SECOND = 20
DELETE_MESSAGES_CHUNK = 100

# Idempotent schema creation for fresh installs; existing databases are
# upgraded by the migrations under db/migrations/ during deployment.
init_db()


def persistence_interval() -> int:
    try:
        return max(1, int(os.getenv("PERSISTENCE_INTERVAL", "300")))
    except ValueError:
        return 300


def allowed_updates() -> list[str]:
    return ["message", "callback_query"]


def http_settings() -> dict:
    def integer(name, default):
        try:
            return int(os.getenv(name, str(default)))
        except ValueError:
            return default

    return {
        "connection_pool_size": integer("HTTP_POOL_SIZE", 8),
        "pool_timeout": integer("HTTP_POOL_TIMEOUT", 5),
        "connect_timeout": integer("HTTP_CONNECT_TIMEOUT", 5),
        "read_timeout": integer("HTTP_READ_TIMEOUT", 10),
        "write_timeout": integer("HTTP_WRITE_TIMEOUT", 20),
    }


# 延时发送媒体组消息的回调
async def _send_media_group_later(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    media_group_id = job.data
    _, from_chat_id, target_id, direction = job.name.split("_")
    from_chat_id = int(from_chat_id)
    target_id = int(target_id)

    media_group_msgs = await run_db(
        repository.get_media_group_messages, media_group_id, from_chat_id
    )
    if not media_group_msgs:
        return

    if direction == "u2a":
        user = await run_db(repository.get_user_by_user_id, from_chat_id)
        message_thread_id = user.message_thread_id if user else None
    else:
        message_thread_id = None

    sents = await context.bot.copy_messages(
        chat_id=target_id,
        from_chat_id=from_chat_id,
        message_ids=[message.message_id for message in media_group_msgs],
        message_thread_id=message_thread_id,
    )

    rows = []
    for sent, message in zip(sents, media_group_msgs):
        if direction == "u2a":
            rows.append((message.message_id, sent.message_id, from_chat_id))
        else:
            rows.append((sent.message_id, message.message_id, target_id))
    await run_db(repository.create_message_maps_batch, rows)
    await run_db(repository.delete_media_group_messages, media_group_id, from_chat_id)


# 延时发送媒体组消息
async def send_media_group_later(
    delay: float,
    chat_id,
    target_id,
    media_group_id: int,
    direction,
    context: ContextTypes.DEFAULT_TYPE,
):
    name = f"sendmediagroup_{chat_id}_{target_id}_{direction}"
    context.job_queue.run_once(
        _send_media_group_later, delay, chat_id=chat_id, name=name, data=media_group_id
    )
    return name


async def forwarding_message_u2a(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with user_lock(context, update.effective_user.id):
        await _forwarding_message_u2a(update, context)


async def _forwarding_message_u2a(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with stage_timer("handler", handler="u2a"):
        if not disable_captcha and not await check_human(update, context):
            return
        if message_interval:
            if context.user_data.get("last_message_time", 0) > time.time() - message_interval:
                await update.message.reply_html("请不要频繁发送消息。")
                return
            context.user_data["last_message_time"] = time.time()

        user = update.effective_user
        u = await run_db(
            repository.get_or_create_user,
            user.id,
            user.first_name,
            user.last_name,
            user.username,
            user.is_premium,
        )
        chat_id = admin_group_id
        message_thread_id = u.message_thread_id
        if message_thread_id:
            status = await run_db(repository.get_formn_status, message_thread_id)
            if status == "closed":
                await update.message.reply_html(
                    "客服已经关闭对话。如需联系，请利用其他途径联络客服回复和你的对话。"
                )
                return

        first_message_new_thread = False
        if not message_thread_id:
            formn = await context.bot.create_forum_topic(
                chat_id, name=f"{user.full_name}|{user.id}"
            )
            message_thread_id = formn.message_thread_id
            first_message_new_thread = True
            if update.message.media_group_id:
                # Albums enqueue a separate job rather than writing a map below.
                await run_db(repository.set_user_thread, user.id, message_thread_id)
            await context.bot.send_message(
                chat_id,
                f"新的用户 {mention_html(user.id, user.full_name)} 开始了一个新的会话。",
                message_thread_id=message_thread_id,
                parse_mode="HTML",
            )
            await send_contact_card(chat_id, message_thread_id, user, update, context)

        params = {"message_thread_id": message_thread_id}
        if update.message.reply_to_message:
            reply_in_user_chat = update.message.reply_to_message.message_id
            group_message_id = await run_db(
                repository.find_message_map_by_user_chat, user.id, reply_in_user_chat
            )
            if group_message_id:
                params["reply_to_message_id"] = group_message_id

        try:
            if update.message.media_group_id:
                await run_db(
                    repository.add_media_group_message,
                    update.message.chat.id,
                    update.message.message_id,
                    update.message.media_group_id,
                    update.message.caption_html,
                )
                if update.message.media_group_id != context.user_data.get(
                    "current_media_group_id", 0
                ):
                    context.user_data["current_media_group_id"] = (
                        update.message.media_group_id
                    )
                    await send_media_group_later(
                        5, user.id, chat_id, update.message.media_group_id, "u2a", context
                    )
                return

            sent_msg = await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.id,
                **params,
            )
            if first_message_new_thread:
                await run_db(
                    repository.set_user_thread_and_create_map,
                    user.id,
                    message_thread_id,
                    update.message.id,
                    sent_msg.message_id,
                )
            else:
                await run_db(
                    repository.create_message_map,
                    update.message.id,
                    sent_msg.message_id,
                    user.id,
                )
        except BadRequest:
            if is_delete_topic_as_ban_forever:
                await update.message.reply_html(
                    "发送失败，你的对话已经被客服删除。请联系客服重新打开对话。"
                )
            else:
                await run_db(repository.set_user_thread, user.id, 0)
                await update.message.reply_html(
                    "发送失败，你的对话已经被客服删除。请再发送一条消息用来激活对话。"
                )
        except Exception as e:
            await update.message.reply_html(f"发送失败: {e}\n")


async def forwarding_message_a2u(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with user_lock(context, update.effective_user.id):
        await _forwarding_message_a2u(update, context)


async def _forwarding_message_a2u(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with stage_timer("handler", handler="a2u"):
        user = update.effective_user
        await run_db(
            repository.get_or_create_user,
            user.id,
            user.first_name,
            user.last_name,
            user.username,
            user.is_premium,
        )
        message_thread_id = update.message.message_thread_id
        if not message_thread_id:
            return

        found = await run_db(repository.get_user_by_thread, message_thread_id)
        user_id = found.user_id if found else 0
        if not user_id:
            logger.debug(update.message)
            return

        if update.message.forum_topic_created:
            await run_db(repository.set_formn_status, message_thread_id, "opened")
            return
        if update.message.forum_topic_closed:
            await context.bot.send_message(
                user_id, "对话已经结束。对方已经关闭了对话。你的留言将被忽略。"
            )
            await run_db(repository.set_formn_status, message_thread_id, "closed")
            return
        if update.message.forum_topic_reopened:
            await context.bot.send_message(user_id, "对方重新打开了对话。可以继续对话了。")
            await run_db(repository.set_formn_status, message_thread_id, "opened")
            return

        status = await run_db(repository.get_formn_status, message_thread_id)
        if status == "closed":
            await update.message.reply_html("对话已经结束。希望和对方联系，需要打开对话。")
            return

        params = {}
        if update.message.reply_to_message:
            reply_in_admin = update.message.reply_to_message.message_id
            user_chat_message_id = await run_db(
                repository.find_message_map_by_group_chat, reply_in_admin
            )
            if user_chat_message_id:
                params["reply_to_message_id"] = user_chat_message_id

        try:
            if update.message.media_group_id:
                await run_db(
                    repository.add_media_group_message,
                    update.message.chat.id,
                    update.message.message_id,
                    update.message.media_group_id,
                    update.message.caption_html,
                )
                user_data = context.application.user_data[user_id]
                if update.message.media_group_id != user_data.get(
                    "current_media_group_id", 0
                ):
                    user_data["current_media_group_id"] = update.message.media_group_id
                    await send_media_group_later(
                        5,
                        update.effective_chat.id,
                        user_id,
                        update.message.media_group_id,
                        "a2u",
                        context,
                    )
                return

            sent_msg = await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.id,
                **params,
            )
            await run_db(
                repository.create_message_map,
                sent_msg.message_id,
                update.message.id,
                user_id,
            )
        except Exception as e:
            await update.message.reply_html(f"发送失败: {e}\n")


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in admin_user_ids:
        await update.message.reply_html("你没有权限执行此操作。")
        return
    thread_id = update.message.message_thread_id
    await context.bot.delete_forum_topic(update.effective_chat.id, thread_id)
    if not is_delete_user_messages:
        return
    target = await run_db(repository.get_user_by_thread, thread_id)
    if not target:
        return
    message_ids = await run_db(repository.get_message_ids_for_user, target.user_id)
    deleted_ids = []
    failed_count = 0
    for offset in range(0, len(message_ids), DELETE_MESSAGES_CHUNK):
        chunk = message_ids[offset : offset + DELETE_MESSAGES_CHUNK]
        for attempt in range(3):
            try:
                await context.bot.delete_messages(target.user_id, chunk)
            except RetryAfter as error:
                if attempt == 2:
                    logger.warning(f"clear chunk rate limited: {error}")
                    failed_count += len(chunk)
                    break
                delay = error.retry_after
                await asyncio.sleep(
                    delay.total_seconds()
                    if hasattr(delay, "total_seconds")
                    else float(delay)
                )
            except BadRequest as error:
                logger.warning(f"clear chunk failed: {error}")
                failed_count += len(chunk)
                break
            else:
                deleted_ids.extend(chunk)
                break
    if deleted_ids:
        await run_db(
            repository.delete_message_maps_for_user, target.user_id, deleted_ids
        )
    if failed_count:
        logger.warning(
            f"clear retained {failed_count} message maps for user {target.user_id} "
            "after failed deletions"
        )


async def _broadcast(context: ContextTypes.DEFAULT_TYPE):
    users = await run_db(repository.get_all_users)
    msg_id, chat_id = context.job.data.split("_")
    bucket = TokenBucket(BROADCAST_RATE_PER_SECOND)
    semaphore = asyncio.Semaphore(BROADCAST_MAX_PARALLEL)
    results = {"success": 0, "failed": 0, "rate_limited": 0, "failures": []}

    async def send_one(user):
        async with semaphore:
            await bucket.acquire()
            for attempt in range(3):
                try:
                    await context.bot.copy_message(
                        chat_id=user.user_id,
                        from_chat_id=chat_id,
                        message_id=int(msg_id),
                    )
                    results["success"] += 1
                    return
                except RetryAfter as error:
                    results["rate_limited"] += 1
                    if attempt == 2:
                        break
                    delay = error.retry_after
                    await asyncio.sleep(
                        delay.total_seconds()
                        if hasattr(delay, "total_seconds")
                        else float(delay)
                    )
                except Exception as error:
                    results["failures"].append((user.user_id, str(error)))
                    break
            results["failed"] += 1

    await asyncio.gather(*(send_one(user) for user in users))
    logger.info(
        f"broadcast done: success={results['success']} failed={results['failed']} "
        f"rate_limited={results['rate_limited']} failures={results['failures'][:10]}"
    )


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in admin_user_ids:
        await update.message.reply_html("你没有权限执行此操作。")
        return
    if not update.message.reply_to_message:
        await update.message.reply_html(
            "这条指令需要回复一条消息，被回复的消息将被广播。"
        )
        return
    context.job_queue.run_once(
        _broadcast,
        0,
        data=f"{update.message.reply_to_message.id}_{update.effective_chat.id}",
    )


async def error_in_send_media_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html("错误的消息类型。退出发送媒体组。后续对话将直接转发。")
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logger.error(f"Exception while handling an update: {context.error} ")
    logger.debug("Exception detail is :", exc_info=context.error)


async def _cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    result = await run_db(
        cleanup_old_message_maps,
        int(os.getenv("MESSAGE_MAP_RETENTION_DAYS", "90")),
    )
    logger.info(f"data cleanup: {result}")


if __name__ == "__main__":
    pickle_persistence = PicklePersistence(
        filepath=f"./assets/{app_name}.pickle",
        update_interval=persistence_interval(),
    )
    http = http_settings()
    builder = (
        ApplicationBuilder()
        .token(bot_token)
        .persistence(persistence=pickle_persistence)
        .connection_pool_size(http["connection_pool_size"])
        .pool_timeout(http["pool_timeout"])
        .connect_timeout(http["connect_timeout"])
        .read_timeout(http["read_timeout"])
        .write_timeout(http["write_timeout"])
    )
    application = configure_builder(builder).build()
    if cleanup_enabled():
        application.job_queue.run_daily(_cleanup_job, time=time_of_day(hour=3, minute=0))

    application.add_handler(CommandHandler("start", start, filters.ChatType.PRIVATE))
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & filters.ChatType.PRIVATE, forwarding_message_u2a
        )
    )
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & filters.Chat([admin_group_id]), forwarding_message_a2u
        )
    )
    application.add_handler(
        CommandHandler("clear", clear, filters.Chat([admin_group_id]))
    )
    application.add_handler(
        CommandHandler("broadcast", broadcast, filters.Chat([admin_group_id]))
    )
    application.add_handler(
        CallbackQueryHandler(callback_query_vcode, pattern="^vcode_")
    )
    application.add_error_handler(error_handler)
    application.run_polling(allowed_updates=allowed_updates())
