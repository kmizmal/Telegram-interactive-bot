import os
import random
import time
from string import ascii_letters as letters

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
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
from db.database import Base, engine
from db.executor import run_db

from . import (
    admin_group_id,
    admin_user_ids,
    app_name,
    bot_token,
    is_delete_topic_as_ban_forever,
    is_delete_user_messages,
    logger,
    welcome_message,
    disable_captcha,
    message_interval,
)
from .observability import stage_timer
from .utils import delete_message_later

# create_all is safe on an existing database; new indexes are added by
# db/migrations/perf_indexes.py during deployment.
Base.metadata.create_all(bind=engine)


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


async def send_contact_card(
    chat_id, message_thread_id, user: telegram.User, update: Update, context: ContextTypes
):
    buttons = [
        [
            InlineKeyboardButton(
                f"{'🏆 高级会员' if user.is_premium else '✈️ 普通会员'}",
                url="https://github.com/MiHaKun/Telegram-interactive-bot",
            )
        ]
    ]
    if user.username:
        buttons.append(
            [InlineKeyboardButton("👤 直接联络", url=f"https://t.me/{user.username}")]
        )

    user_photo = await context.bot.get_user_profile_photos(user.id)
    caption = (
        f"👤 {mention_html(user.id, user.first_name)}\n\n"
        f"📱 {user.id}\n\n"
        f"🔗 @{user.username if user.username else '无'}"
    )
    if user_photo.total_count:
        await context.bot.send_photo(
            chat_id,
            photo=user_photo.photos[0][-1].file_id,
            caption=caption,
            message_thread_id=message_thread_id,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML",
        )
    else:
        await context.bot.send_contact(
            chat_id,
            phone_number="11111",
            first_name=user.first_name,
            last_name=user.last_name,
            message_thread_id=message_thread_id,
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await run_db(
        repository.get_or_create_user,
        user.id,
        user.first_name,
        user.last_name,
        user.username,
        user.is_premium,
    )
    if user.id in admin_user_ids:
        logger.info(f"{user.first_name}({user.id}) is admin")
        try:
            bg = await context.bot.get_chat(admin_group_id)
            if bg.type == "supergroup" or bg.type == "group":
                logger.info(f"admin group is {bg.title}")
        except Exception as e:
            logger.error(f"admin group error {e}")
            await update.message.reply_html(
                f"⚠️⚠️后台管理群组设置错误，请检查配置。⚠️⚠️\n你需要确保已经将机器人 @{context.bot.username} 邀请入管理群组并且给与了管理员权限。\n错误细节：{e}\n"
            )
            return ConversationHandler.END
        await update.message.reply_html(
            f"你好管理员 {user.first_name}({user.id})\n\n欢迎使用 {app_name} 机器人。\n\n 目前你的配置完全正确。可以在群组 <b> {bg.title} </b> 中使用机器人。"
        )
    else:
        await update.message.reply_html(
            f"{mention_html(user.id, user.full_name)} 同学：\n\n{welcome_message}"
        )


async def check_human(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.user_data.get("is_human", False):
        if context.user_data.get("is_human_error_time", 0) > time.time() - 120:
            await update.message.reply_html("你已经被禁言,请稍后再尝试。")
            return False
        file_name = random.choice(os.listdir("./assets/imgs"))
        code = file_name.replace("image_", "").replace(".png", "")
        file = f"./assets/imgs/{file_name}"
        codes = ["".join(random.sample(letters, 5)) for _ in range(0, 7)]
        codes.append(code)
        random.shuffle(codes)

        photo = context.bot_data.get(f"image|{code}") or file
        buttons = [
            InlineKeyboardButton(text, callback_data=f"vcode_{text}_{user.id}")
            for text in codes
        ]
        button_matrix = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
        sent = await update.message.reply_photo(
            photo,
            f"{mention_html(user.id, user.first_name)}请选择图片中的文字。回答错误将无法联系客服。",
            reply_markup=InlineKeyboardMarkup(button_matrix),
            parse_mode="HTML",
        )
        biggest_photo = sorted(sent.photo, key=lambda x: x.file_size, reverse=True)[0]
        context.bot_data[f"image|{code}"] = biggest_photo.file_id
        context.user_data["vcode"] = code
        await delete_message_later(60, sent.chat.id, sent.message_id, context)
        return False
    return True


async def callback_query_vcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    code = query.data.split("_")[1]
    user_id = query.data.split("_")[2]
    if user_id == str(user.id):
        if code == context.user_data.get("vcode"):
            await query.answer("正确，欢迎。")
            await context.bot.send_message(
                update.effective_chat.id,
                f"{mention_html(user.id, user.first_name)} , 欢迎。",
                parse_mode="HTML",
            )
            context.user_data["is_human"] = True
        else:
            await query.answer("~错误~，禁言2分钟")
            context.user_data["is_human_error_time"] = time.time()
    await query.message.delete()


async def forwarding_message_u2a(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                repository.find_message_map_by_user_chat, reply_in_user_chat
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
    if message_ids:
        await context.bot.delete_messages(target.user_id, message_ids)
    await run_db(repository.delete_message_maps_for_user, target.user_id)


async def _broadcast(context: ContextTypes.DEFAULT_TYPE):
    users = await run_db(repository.get_all_users)
    msg_id, chat_id = context.job.data.split("_")
    success = 0
    failed = 0
    for user in users:
        try:
            await context.bot.copy_message(
                chat_id=user.user_id, from_chat_id=chat_id, message_id=int(msg_id)
            )
            success += 1
        except Exception:
            failed += 1
    logger.info(f"broadcast done: success={success} failed={failed}")


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


if __name__ == "__main__":
    pickle_persistence = PicklePersistence(filepath=f"./assets/{app_name}.pickle")
    application = (
        ApplicationBuilder()
        .token(bot_token)
        .persistence(persistence=pickle_persistence)
        .build()
    )

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
    application.run_polling()
