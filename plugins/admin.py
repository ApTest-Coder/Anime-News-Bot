from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from pyrogram.enums import ParseMode
from database.db import db
from config import *
from helper.news_job import *
from helper.rss_detector import (
    detect_and_create_source, _scrape_latest_items, _fetch_url, _validate_url,
    normalize_article_url, normalize_title, _parse_timestamp, _try_parse_feed_items,
)
from datetime import datetime, timezone, timedelta

import logging
import aiohttp

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


# --- Small Caps Font Helper ---
_SMALL_CAPS_MAP = {
    'a': 'ᴀ', 'b': 'ʙ', 'c': 'ᴄ', 'd': 'ᴅ', 'e': 'ᴇ', 'f': 'ғ', 'g': 'ɢ',
    'h': 'ʜ', 'i': 'ɪ', 'j': 'ᴊ', 'k': 'ᴋ', 'l': 'ʟ', 'm': 'ᴍ', 'n': 'ɴ',
    'o': 'ᴏ', 'p': 'ᴘ', 'q': 'ǫ', 'r': 'ʀ', 's': 's', 't': 'ᴛ', 'u': 'ᴜ',
    'v': 'ᴠ', 'w': 'ᴡ', 'x': 'x', 'y': 'ʏ', 'z': 'ᴢ',
    'A': 'ᴀ', 'B': 'ʙ', 'C': 'ᴄ', 'D': 'ᴅ', 'E': 'ᴇ', 'F': 'ғ', 'G': 'ɢ',
    'H': 'ʜ', 'I': 'ɪ', 'J': 'ᴊ', 'K': 'ᴋ', 'L': 'ʟ', 'M': 'ᴍ', 'N': 'ɴ',
    'O': 'ᴏ', 'P': 'ᴘ', 'Q': 'ǫ', 'R': 'ʀ', 'S': 's', 'T': 'ᴛ', 'U': 'ᴜ',
    'V': 'ᴠ', 'W': 'ᴡ', 'X': 'x', 'Y': 'ʏ', 'Z': 'ᴢ',
}


def _sm(text: str) -> str:
    """Convert text to small caps Unicode style."""
    result = []
    for char in text:
        result.append(_SMALL_CAPS_MAP.get(char, char))
    return ''.join(result)


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
        return await message.reply_text(f"⚠️ **{_sm('syntax error')}:** ᴜsᴀɢᴇ: `/add_rss <url>`")

    url = message.command[1].strip()

    # Check for duplicate
    existing_sources = await db.get_all_sources()
    if any(s.get("url") == url for s in existing_sources):
        return await message.reply_text(f"⚠️ **{_sm('warning')}:** ᴛʜɪs sᴏᴜʀᴄᴇ ʜᴀs ᴀʟʀᴇᴀᴅʏ ʙᴇᴇɴ ᴀᴅᴅᴇᴅ.\n`{url}`")

    # Check limit (from config — MAX_RSS_SOURCES, configurable by admin)
    if len(existing_sources) >= MAX_RSS_SOURCES:
        return await message.reply_text(
            f"⛔️ **{_sm('limit reached')}:** sʏsᴛᴇᴍ sᴇᴛ ᴛᴏ {MAX_RSS_SOURCES} sᴏᴜʀᴄᴇs ᴍᴀxɪᴍᴜᴍ."
        )

    # Send processing message
    processing_msg = await message.reply_text(f"🔍 **{_sm('analyzing')}** {url}...")

    try:
        source = await detect_and_create_source(url)
    except Exception as e:
        logger.error(f"Error in add_rss detection: {e}")
        return await processing_msg.edit_text(f"❌ **{_sm('error')}:** {e}")

    if not source:
        return await processing_msg.edit_text(
            f"❌ **{_sm('error')}:** ᴄᴏᴜʟᴅ ɴᴏᴛ ᴅᴇᴛᴇᴄᴛ ᴀ ᴠᴀʟɪᴅ ғᴇᴇᴅ ᴏʀ sᴄʀᴀᴘᴇ ᴀʀᴛɪᴄʟᴇs.\n"
            f"**{_sm('please check the url and try again.')}"
        )

    # Save to database
    await db.add_source(source)

    # Build response based on type
    if source["type"] == "rss":
        if source["feed_url"] == url:
            # Direct feed
            response = (
                f"✅ **{_sm('rss feed added')}**\n\n"
                f"**{_sm('title')}:** {source['title']}\n"
                f"**{_sm('feed')}:** {source['feed_url']}\n"
                f"**{_sm('type')}:** RSS"
            )
        else:
            # Auto-discovered
            response = (
                f"✅ **{_sm('rss feed discovered and added')}**\n\n"
                f"**{_sm('title')}:** {source['title']}\n"
                f"**{_sm('feed')}:** {source['feed_url']}\n"
                f"**{_sm('type')}:** RSS (Auto-Discovered)"
            )
    else:
        # Scraper
        response = (
            f"✅ **{_sm('scraper source added')}**\n\n"
            f"**{_sm('source')}:** {url}\n"
            f"**{_sm('type')}:** Custom Scraper\n"
            f"**{_sm('status')}:** Active"
        )

    await processing_msg.edit_text(response)


@Client.on_message(filters.command("test_post") & admin)
async def test_post_cmd(client: Client, message: Message):
    """
    Test command: fetch a URL and show posts from the last 30 minutes.
    Does NOT add the URL to RSS sources.
    """
    if len(message.command) < 2:
        return await message.reply_text(f"⚠️ **{_sm('syntax error')}:** ᴜsᴀɢᴇ: `/test_post <url>`")

    url = message.command[1].strip()

    # Normalize URL
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Validate URL (prevent SSRF)
    if not _validate_url(url):
        return await message.reply_text(f"❌ **{_sm('error')}:** ɪɴᴠᴀʟɪᴅ ᴏʀ ʙʟᴏᴄᴋᴇᴅ ᴜʀʟ.")

    processing_msg = await message.reply_text(f"🔍 **{_sm('testing')}** {url}...")

    try:
        # Fetch the page
        async with aiohttp.ClientSession() as session:
            status, content = await _fetch_url(session, url)

            # --- Differentiated diagnostics instead of a single generic failure ---
            if status == 0:
                return await processing_msg.edit_text(
                    f"❌ **{_sm('fetch failed')}:** ᴄᴏᴜʟᴅ ɴᴏᴛ ʀᴇᴀᴄʜ `{url}` (ᴅɴs/ᴛɪᴍᴇᴏᴜᴛ/ɴᴇᴛᴡᴏʀᴋ ᴇʀʀᴏʀ)."
                )
            if status in (401, 403):
                return await processing_msg.edit_text(
                    f"🚫 **{_sm('blocked')}:** ᴛʜᴇ ᴡᴇʙsɪᴛᴇ ʀᴇᴛᴜʀɴᴇᴅ sᴛᴀᴛᴜs {status} (ᴀᴄᴄᴇss/ʙᴏᴛ ʙʟᴏᴄᴋᴇᴅ)."
                )
            if status == 429:
                return await processing_msg.edit_text(
                    f"⏳ **{_sm('rate limited')}:** sᴛᴀᴛᴜs 429 (ᴛᴏᴏ ᴍᴀɴʏ ʀᴇǫᴜᴇsᴛs). ᴛʀʏ ᴀɢᴀɪɴ ʟᴀᴛᴇʀ."
                )
            if status >= 500:
                return await processing_msg.edit_text(
                    f"⚠️ **{_sm('server error')}:** ᴛʜᴇ ᴡᴇʙsɪᴛᴇ ʀᴇᴛᴜʀɴᴇᴅ sᴛᴀᴛᴜs {status}."
                )
            if not (200 <= status < 300):
                return await processing_msg.edit_text(
                    f"❌ **{_sm('fetch failed')}:** ᴜɴʜᴀɴᴅʟᴇᴅ sᴛᴀᴛᴜs {status}."
                )
            if not content:
                return await processing_msg.edit_text(
                    f"⚠️ **{_sm('html fetched, but empty')}:** sᴛᴀᴛᴜs {status} ʙᴜᴛ ɴᴏ ᴛᴇxᴛ ʀᴇᴄᴇɪᴠᴇᴅ."
                )

            # Strategy 1: try RSS/Atom feed first
            items = _try_parse_feed_items(content, url)
            if items is not None:
                logger.info(f"[test_post] RSS/Atom feed parsed for {url}: {len(items)} item(s)")

            # Strategy 2+: fall back to HTML scraping (JSON-LD → embedded JSON
            # → article/card containers → headings → article links)
            if not items:
                items = _scrape_latest_items(content, url)

            if not items:
                return await processing_msg.edit_text(
                    f"⚠️ **{_sm('html fetched, but no articles were extracted')}.**\n"
                    f"🔗 {url}\n"
                    f"**{_sm('status')}:** {status}"
                )

            # In-execution dedup: remove duplicate links AND duplicate titles
            seen_urls = set()
            seen_titles = set()
            unique_items = []
            for item in items:
                norm_url = normalize_article_url(item.get("link", ""))
                norm_title = normalize_title(item.get("title", ""))
                if not norm_url or norm_url in seen_urls:
                    continue
                if norm_title and norm_title in seen_titles:
                    continue
                seen_urls.add(norm_url)
                seen_titles.add(norm_title)
                unique_items.append(item)
            items = unique_items

            # Filter for posts from last 30 minutes
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(minutes=30)

            recent_items = []
            had_timestamp = False
            for item in items:
                published_str = item.get("published", "")
                if published_str:
                    pub_dt = _parse_timestamp(published_str)
                    if pub_dt:
                        had_timestamp = True
                        if pub_dt >= cutoff:
                            recent_items.append(item)

            newest = items[0] if items else None
            newest_time = newest.get("published", "") if newest else ""

            if not recent_items:
                if not had_timestamp:
                    # Case C: articles found, but no publication time available
                    text = (
                        f"⚠️ **{_sm('articles detected, but publish time is unavailable')}.**\n\n"
                        f"📰 {newest['title'] if newest else 'N/A'}\n"
                        f"🔗 {newest['link'] if newest else ''}\n\n"
                        f"**{_sm('source')}:** {url}"
                    )
                else:
                    # Case D: articles found, timestamps exist, none within 30 min
                    text = (
                        f"ℹ️ **{_sm('no new posts in the last 30 minutes')}.**\n\n"
                        f"**{_sm('last detected')}:**\n"
                        f"📰 {newest['title'] if newest else 'N/A'}\n"
                    )
                    if newest_time:
                        text += f"📅 **{_sm('published')}:** `{newest_time}`\n"
                    if newest:
                        text += f"🔗 {newest['link']}\n"
                    text += f"\n**{_sm('source')}:** {url}"
                return await processing_msg.edit_text(text)

            # Send recent items as test posts (max 5)
            await processing_msg.edit_text(
                f"🧪 **{_sm('test post')}** — {len(recent_items)} {_sm('recent post(s) found')}\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )

            sent_count = 0
            for item in recent_items[:5]:
                title = item.get("title", "No Title")
                link = item.get("link", "")
                published = item.get("published", "")

                # Try to get image from article page
                image_url = None
                try:
                    from helper.fetcher import fetch_image_from_article
                    image_url = await fetch_image_from_article(session, link)
                except Exception:
                    pass

                caption = (
                    f"🧪 **{_sm('test post')}**\n\n"
                    f"📰 **{title}**\n"
                    f"📅 **{_sm('published')}:** `{published}`\n"
                    f"🔗 {link}\n\n"
                    f"**{_sm('source')}:** {url}"
                )

                try:
                    if image_url:
                        await client.send_photo(
                            chat_id=message.chat.id,
                            photo=image_url,
                            caption=caption,
                            parse_mode=ParseMode.HTML
                        )
                    else:
                        await client.send_message(
                            chat_id=message.chat.id,
                            text=caption,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=False
                        )
                    sent_count += 1
                except Exception as e:
                    logger.error(f"Error sending test post: {e}")
                    await client.send_message(
                        chat_id=message.chat.id,
                        text=f"⚠️ **{_sm('error')}:** ғᴀɪʟᴇᴅ ᴛᴏ sᴇɴᴅ ᴛᴇsᴛ ᴘᴏsᴛ: {e}"
                    )

            if sent_count == 0:
                await client.send_message(
                    chat_id=message.chat.id,
                    text=f"❌ **{_sm('error')}:** ᴄᴏᴜʟᴅ ɴᴏᴛ sᴇɴᴅ ᴀɴʏ ᴛᴇsᴛ ᴘᴏsᴛs."
                )

    except Exception as e:
        logger.error(f"Error in test_post: {e}")
        await processing_msg.edit_text(f"❌ **{_sm('error')}:** {e}")


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
