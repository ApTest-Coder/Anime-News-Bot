from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from pyrogram.enums import ParseMode
from database.db import db
from config import *
from helper.news_job import *
from helper.rss_detector import detect_and_create_source

import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


ADMIN_IDS = [OWNER_ID, ADMIN_ID]


async def check_admin(_, client, update):
    try:
        user_id = update.from_user.id
        # Check if user is owner or in admin list
        return user_id in ADMIN_IDS
    except Exception as e:
        logger.error(f"Exception in check_admin: {e}")
        return False
            
admin = filters.create(check_admin)


@Client.on_callback_query()
async def settings_callback(client: Client, callback_query):
    user_id = callback_query.from_user.id
    cb_data = callback_query.data

    try:
        if cb_data == "about":
            await callback_query.edit_message_media(
                InputMediaPhoto(ABOUT_PIC, ABOUT_MSG),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("• ʙᴀᴄᴋ", callback_data="start"),
                        InlineKeyboardButton("ᴄʟᴏsᴇ •", callback_data="close")
                    ]
                ])
            )

        elif cb_data == "help":
            await callback_query.edit_message_media(
                InputMediaPhoto(HELP_PIC, HELP_MSG),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("• ʙᴀᴄᴋ", callback_data="start"),
                        InlineKeyboardButton("ᴄʟᴏsᴇ •", callback_data="close")
                    ]
                ])
            )

        elif cb_data == "start":
            inline_buttons = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("• ᴀʙᴏᴜᴛ", callback_data="about"),
                    InlineKeyboardButton("ʜᴇʟᴘ •", callback_data="help")
                ]
            ])
            try:
                await callback_query.edit_message_media(
                    InputMediaPhoto(
                        START_PIC,
                        START_MSG.format(
                            first=callback_query.from_user.first_name,
                            last=callback_query.from_user.last_name or "",
                            username=f"@{callback_query.from_user.username}" if callback_query.from_user.username else "None",
                            mention=callback_query.from_user.mention,
                            id=callback_query.from_user.id
                        )
                    ),
                    reply_markup=inline_buttons
                )
            except Exception as e:
                logger.error(f"ᴇʀʀᴏʀ sᴇɴᴅɪɴɢ sᴛᴀʀᴛ/ʜᴏᴍᴇ ᴘʜᴏᴛᴏ: {e}")
                await callback_query.edit_message_text(
                    START_MSG.format(
                        first=callback_query.from_user.first_name,
                        last=callback_query.from_user.last_name or "",
                        username=f"@{callback_query.from_user.username}" if callback_query.from_user.username else "None",
                        mention=callback_query.from_user.mention,
                        id=callback_query.from_user.id
                    ),
                    reply_markup=inline_buttons,
                    parse_mode=ParseMode.HTML
                )

        elif cb_data == "close":
            await callback_query.message.delete()
            try:
                await callback_query.message.reply_to_message.delete()
            except:
                pass

        elif cb_data == "view_rss":
            if user_id not in ADMIN_IDS:
                return await callback_query.answer("⛔️ ᴜɴᴀᴜᴛʜᴏʀɪᴢᴇᴅ!", show_alert=True)
            feeds = await db.get_all_rss()
            text = (
                "📡 ᴀᴄᴛɪᴠᴇ ʀss ꜰᴇᴇᴅs:\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                + ("\n".join(f"🟢 {f}" for f in feeds) if feeds else "⚠️ ɴᴏ ʀss ꜰᴇᴇᴅs ᴄᴏɴꜰɪɢᴜʀᴇᴅ.")
            )
            await callback_query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ ʀᴇᴛᴜʀɴ", callback_data="help")]
                ])
            )

        elif cb_data == "view_chnl":
            if user_id not in ADMIN_IDS:
                return await callback_query.answer("⛔️ ᴜɴᴀᴜᴛʜᴏʀɪᴢᴇᴅ!", show_alert=True)
            channels = await db.get_all_channels()
            text = (
                "📢 ᴀᴄᴛɪᴠᴇ ᴛᴀʀɢᴇᴛ ʀᴏᴜᴛᴇs:\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                + ("\n".join(f"🟢 {c}" for c in channels) if channels else "⚠️ ɴᴏ ᴛᴀʀɢᴇᴛ ᴄʜᴀɴɴᴇʟs ᴄᴏɴꜰɪɢᴜʀᴇᴅ.")
            )
            await callback_query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ ʀᴇᴛᴜʀɴ", callback_data="help")]
                ])
            )

        elif cb_data == "status":
            if user_id not in ADMIN_IDS:
                return await callback_query.answer("⛔️ ᴜɴᴀᴜᴛʜᴏʀɪᴢᴇᴅ!", show_alert=True)
            total = await db.get_total_posted()
            feeds = await db.get_all_rss()
            channels = await db.get_all_channels()
            text = (
                "📊 **ʙᴏᴛ sᴛᴀᴛᴜs**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"🟢 **ᴇɴɢɪɴᴇ:** `ᴏɴʟɪɴᴇ`\n"
                f"📡 **ʀss ꜰᴇᴇᴅs:** `{len(feeds)}/2`\n"
                f"📢 **ᴄʜᴀɴɴᴇʟs:** `{len(channels)}`\n"
                f"📰 **ʟɪꜰᴇᴛɪᴍᴇ ᴘᴏsᴛᴇᴅ:** `{total}` ᴀʀᴛɪᴄʟᴇs\n"
                "━━━━━━━━━━━━━━━━━━━━"
            )
            await callback_query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("📡 ʀss", callback_data="view_rss"),
                        InlineKeyboardButton("📢 ᴄʜᴀɴɴᴇʟs", callback_data="view_chnl")
                    ],
                    [InlineKeyboardButton("◀️ ʙᴀᴄᴋ", callback_data="help")]
                ])
            )

    except Exception as e:
        logger.error(f"ᴄᴀʟʟʙᴀᴄᴋ ᴇʀʀᴏʀ [{cb_data}]: {e}")


# --- Database Commands (Text Input) ---
@Client.on_message(filters.command("add_rss") & admin)
async def add_rss_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Usage: /add_rss <URL>")

    url = message.command[1].strip()

    # Check for duplicate
    existing_sources = await db.get_all_sources()
    if any(s.get("url") == url for s in existing_sources):
        return await message.reply_text(f"⚠️ This source has already been added.\n`{url}`")

    # Check limit (max 2 sources as per existing logic)
    if len(existing_sources) >= 7:
        return await message.reply_text("⛔️ **Limit reached:** System restricted to 7 RSS sources maximum.")

    # Send processing message
    processing_msg = await message.reply_text("🔍 Analyzing URL...")

    try:
        source = await detect_and_create_source(url)
    except Exception as e:
        logger.error(f"Error in add_rss detection: {e}")
        return await processing_msg.edit_text(f"❌ Error processing URL: {e}")

    if not source:
        return await processing_msg.edit_text(
            "❌ Could not detect a valid feed or scrape content from this URL.\n"
            "Please check the URL and try again."
        )

    # Save to database
    await db.add_source(source)

    # Build response based on type
    if source["type"] == "rss":
        if source["feed_url"] == url:
            # Direct feed
            response = (
                f"✅ RSS feed added successfully.\n\n"
                f"Title: {source['title']}\n"
                f"Feed: {source['feed_url']}\n"
                f"Type: RSS"
            )
        else:
            # Auto-discovered
            response = (
                f"✅ RSS feed discovered and added.\n\n"
                f"Title: {source['title']}\n"
                f"Feed: {source['feed_url']}\n"
                f"Type: RSS (Auto-Discovered)"
            )
    else:
        # Scraper
        response = (
            f"✅ RSS feed was not available, so a background scraper was created.\n\n"
            f"Source: {url}\n"
            f"Type: Custom Scraper\n"
            f"Status: Active"
        )

    await processing_msg.edit_text(response)


@Client.on_message(filters.command("rem_rss") & admin)
async def rem_rss_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("⚠️ **sʏɴᴛᴀx ᴇʀʀᴏʀ:** ᴜsᴀɢᴇ: `/rem_rss <url>`")
    await db.rem_rss_db(message.command[1])
    await message.reply_text(f"🗑 **sᴏᴜʀᴄᴇ ᴅᴇᴛᴀᴄʜᴇᴅ:**\n`{message.command[1]}`")


@Client.on_message(filters.command("view_rss") & admin)
async def view_rss_cmd(client: Client, message: Message):
    feeds = await db.get_all_rss()
    text = (
        "📡 **ᴀᴄᴛɪᴠᴇ ʀss ꜰᴇᴇᴅs:**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        + ("\n".join(f"🟢 `{f}`" for f in feeds) if feeds else "⚠️ ɴᴏ ʀss ꜰᴇᴇᴅs ᴄᴏɴꜰɪɢᴜʀᴇᴅ.")
    )
    await message.reply_text(text)


@Client.on_message(filters.command("add_chnl") & admin)
async def add_chnl_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("⚠️ **sʏɴᴛᴀx ᴇʀʀᴏʀ:** ᴜsᴀɢᴇ: `/add_chnl <@username or ID>`")
    await db.add_channel_db(message.command[1])
    await message.reply_text(f"✅ **ʀᴏᴜᴛᴇ ᴇsᴛᴀʙʟɪsʜᴇᴅ:**\n`{message.command[1]}`")


@Client.on_message(filters.command("rem_chnl") & admin)
async def rem_chnl_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("⚠️ **sʏɴᴛᴀx ᴇʀʀᴏʀ:** ᴜsᴀɢᴇ: `/rem_chnl <@username or ID>`")
    await db.rem_channel_db(message.command[1])
    await message.reply_text(f"🗑 **ʀᴏᴜᴛᴇ sᴇᴠᴇʀᴇᴅ:**\n`{message.command[1]}`")


@Client.on_message(filters.command("view_chnl") & admin)
async def view_chnl_cmd(client: Client, message: Message):
    channels = await db.get_all_channels()
    text = (
        "📢 **ᴀᴄᴛɪᴠᴇ ᴛᴀʀɢᴇᴛ ʀᴏᴜᴛᴇs:**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        + ("\n".join(f"🟢 `{c}`" for c in channels) if channels else "⚠️ ɴᴏ ᴛᴀʀɢᴇᴛ ᴄʜᴀɴɴᴇʟs ᴄᴏɴꜰɪɢᴜʀᴇᴅ.")
    )
    await message.reply_text(text)


@Client.on_message(filters.command("status") & admin)
async def status_cmd(client: Client, message: Message):
    total = await db.get_total_posted()
    await message.reply_text(
        f"ʜᴇʀᴇ ʏᴏᴜʀ ʙᴏᴛ sᴛᴀᴛᴜs:",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("sᴛᴀᴛᴜs", callback_data="status")
            ]
        ])
            )
