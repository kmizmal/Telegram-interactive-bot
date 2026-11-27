"""Handlers for the private (user-facing) chat."""

import random
import time
from string import ascii_letters as letters

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler
from telegram.helpers import mention_html

from db import repository
from db.executor import run_db

from .. import (
    admin_group_id,
    admin_user_ids,
    app_name,
    logger,
    welcome_message,
)
from ..services.captcha import image_choices
from ..utils import delete_message_later


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

    caption = (
        f"👤 {mention_html(user.id, user.first_name)}\n\n"
        f"📱 {user.id}\n\n"
        f"🔗 @{user.username if user.username else '无'}"
    )
    avatar_key = f"avatar|{user.id}"
    photo_id = context.bot_data.get(avatar_key)
    if not photo_id:
        user_photo = await context.bot.get_user_profile_photos(user.id)
        if user_photo.total_count:
            photo_id = user_photo.photos[0][-1].file_id
            context.bot_data[avatar_key] = photo_id
    if photo_id:
        await context.bot.send_photo(
            chat_id,
            photo=photo_id,
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
        file_name = random.choice(image_choices())
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
