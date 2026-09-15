"""
/info <anime name> — public anime information command (Phase 3).

Works in private chats, groups and supergroups: the handler is registered
with filters.command("info") only — no chat-type filter, no hardcoded chat
ID, no group-specific state. message.chat.id is used purely as the reply
destination, wherever the command comes from.

Metadata: AniList GraphQL API via helper.anilist_info (verified data only;
missing fields render as "-"). Hindi dub is shown as "-" unless a verified
source exists (none does yet) — never inferred.
"""

import html
import logging

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
try:
    from pyrogram.enums import ParseMode
except ImportError:  # older pyrogram/PYROFORK layout
    from pyrogram import ParseMode

from helper.anilist_info import get_anime_by_id, search_anime

logger = logging.getLogger(__name__)

SEPARATOR = "✦━━━━━━━━━━━━━━━━━━━✦"
BRAND = (
    '<b>⌬ Pᴏᴡᴇʀᴇᴅ ʙʏ:- <a href="https://t.me/Animerulz_Pro">'
    "𝘼𝙣𝙞𝙢𝙚𝙧𝙪𝙡𝙯</a></b>"
)
USAGE = (
    "ℹ️ <b>Usage:</b> <code>/info &lt;anime name&gt;</code>\n"
    "Example: <code>/info Demon Slayer</code>"
)
PHOTO_CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096

_STATUS_LABELS = {
    "RELEASING": "Ongoing",
    "FINISHED": "Completed",
    "NOT_YET_RELEASED": "Upcoming",
    "CANCELLED": "Cancelled",
    "HIATUS": "Hiatus",
}


def _fmt(value) -> str:
    """Render a verified value, or the em dash when missing."""
    return str(value) if value else "—"


def _esc(value) -> str:
    """HTML-escape any text before inserting into Telegram HTML."""
    return html.escape(str(value), quote=False) if value else ""

def _title_of(anime: dict) -> str:
    return anime.get("title_english") or anime.get("title_romaji") or "Unknown"


def _build_caption(anime: dict) -> str:
    """Build the /info caption. All external text is HTML-escaped."""
    title = _esc(_title_of(anime).upper())
    episodes = anime.get("episodes")
    eps_txt = f"{episodes} Eps" if episodes else "—"
    # Seasons: AniList has no verified season count -> never guessed.
    languages = "Japanese" if anime.get("country") == "JP" else "—"
    streaming = anime.get("streaming") or []
    streaming_txt = ", ".join(_esc(s) for s in streaming[:3]) if streaming else "—"
    genres = anime.get("genres") or []
    studio = anime.get("studio")
    g = _esc(genres[0]) if genres else None
    s = _esc(studio) if studio else None
    gs_txt = " · ".join(p for p in (g, s) if p) or "—"
    year = anime.get("year")
    status = _STATUS_LABELS.get(anime.get("status") or "")
    release_parts = [str(year) if year else None, status]
    release_txt = " · ".join(p for p in release_parts if p) or "—"
    # Hindi dub: no verified source exists yet -> always "—", never inferred.
    lines = [
        "<b>◆ {} ◆</b>".format(title),
        SEPARATOR,
        "<b><blockquote expandable>",
        "🎬  Aɴɪᴍᴇ Pᴏsᴛᴇʀ      ✓",
        "🗣  Hɪɴᴅɪ Dᴜʙ         -  —",
        "📺  Sᴇᴀsᴏɴs & Eᴘs    -  {}".format(_esc(eps_txt)),
        "🌐  Lᴀɴɢᴜᴀɢᴇs        -  {}".format(_esc(languages)),
        "📡  Sᴛʀᴇᴀᴍɪɴɢ        -  {}".format(streaming_txt),
        "🎭  Gᴇɴʀᴇs & Sᴛᴜᴅɪᴏ  -  {}".format(gs_txt),
        "📅  Rᴇʟᴇᴀsᴇ Iɴғᴏ     -  {}".format(_esc(release_txt)),
        "</blockquote></b>",
    ]
    desc = anime.get("description")
    if desc:
        short = desc[:300].rsplit(" ", 1)[0] + "…" if len(desc) > 300 else desc
        lines.append("<i><blockquote expandable>{}</blockquote></i>".format(_esc(short)))
    lines.append(SEPARATOR)
    lines.append(BRAND)
    return "\n".join(lines)


def _match_keyboard(results: list) -> InlineKeyboardMarkup | None:
    """Inline keyboard when multiple strong (TV) matches exist. None otherwise."""
    strong = [r for r in results if r.get("format") == "TV"]
    if len(strong) < 2:
        return None
    buttons = []
    for r in strong[:5]:
        year = r.get("year") or ""
        label = "{}{}".format(_title_of(r), f" ({year})" if year else "")
        buttons.append([InlineKeyboardButton(label[:64], callback_data=f"ainfo:{r['id']}")])
    return InlineKeyboardMarkup(buttons)


async def _deliver(client: Client, chat_id: int, anime: dict):
    """Send poster+caption. Falls back gracefully; never raises."""
    caption = _build_caption(anime)
    cover = anime.get("cover")
    if cover:
        try:
            if len(caption) <= PHOTO_CAPTION_LIMIT:
                await client.send_photo(chat_id, cover, caption=caption,
                                        parse_mode=ParseMode.HTML)
                return
            await client.send_photo(chat_id, cover)
            await client.send_message(chat_id, caption, parse_mode=ParseMode.HTML,
                                      disable_web_page_preview=True)
            return
        except Exception as exc:
            logger.warning("[Info] photo send failed (%s); falling back to text", exc)
    if len(caption) > TEXT_LIMIT:
        caption = caption[:TEXT_LIMIT - 1]
    await client.send_message(chat_id, caption, parse_mode=ParseMode.HTML,
                              disable_web_page_preview=True)


@Client.on_message(filters.command("info"))
async def info_command(client: Client, message: Message):
    """/info <anime name> — public command, valid in private/groups/supergroups."""
    query = " ".join(message.command[1:]).strip() if len(message.command) > 1 else ""
    if not query:
        await message.reply(USAGE, parse_mode=ParseMode.HTML, quote=True)
        return
    status_msg = await message.reply("🔍 <i>Searching AniList…</i>",
                                     parse_mode=ParseMode.HTML, quote=True)
    try:
        results = await search_anime(query)
    except Exception as exc:
        logger.warning("[Info] search error: %s", exc)
        results = None
    if results is None:
        await status_msg.edit("⚠️ <i>AniList is unreachable right now. Try again later.</i>",
                              parse_mode=ParseMode.HTML)
        return
    if not results:
        await status_msg.edit(
            "❌ <i>No anime found for</i> <code>{}</code>".format(_esc(query)),
            parse_mode=ParseMode.HTML)
        return
    keyboard = _match_keyboard(results)
    if keyboard is not None:
        await status_msg.edit(
            "❓ <b>Multiple matches for</b> <code>{}</code>\n<i>Select the correct one:</i>"
            .format(_esc(query)),
            parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return
    await _deliver(client, message.chat.id, results[0])
    try:
        await status_msg.delete()
    except Exception:
        pass


@Client.on_callback_query(filters.regex(r"^ainfo:(\d+)$"))
async def info_callback(client: Client, callback_query: CallbackQuery):
    """Disambiguation tap: fetch by AniList ID (stateless — no storage)."""
    anilist_id = int(callback_query.matches[0].group(1))
    await callback_query.answer()
    anime = await get_anime_by_id(anilist_id)
    if not anime:
        await callback_query.message.reply(
            "⚠️ <i>Could not load that anime. Try again later.</i>",
            parse_mode=ParseMode.HTML)
        return
    chat_id = callback_query.message.chat.id  # reply destination from live context
    try:
        await callback_query.message.edit_text(
            "✅ <i>Loading</i> <b>{}</b>…".format(_esc(_title_of(anime))),
            parse_mode=ParseMode.HTML)
    except Exception:
        pass
    await _deliver(client, chat_id, anime)

