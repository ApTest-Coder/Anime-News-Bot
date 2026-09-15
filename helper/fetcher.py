import aiohttp
import asyncio
import feedparser
import logging
import re
from bs4 import BeautifulSoup
from dataclasses import dataclass
from database.db import db
from helper.rss_detector import scrape_source_for_updates
from config import *


@dataclass
class AnimeNews:
    title: str
    link: str
    summary: str
    image_url: str | None   # None = no image, broadcaster sends text-only
    source_url: str
    guid: str | None = None  # RSS entry id/guid for global dedup


# --- 🔍 IMAGE URL VALIDATOR ---
def is_valid_img(src: str | None) -> bool:
    """
    Checks if a URL looks like a real usable image.
    Filters out tracking pixels, placeholders, SVGs, base64 blobs, etc.
    """
    if not src:
        return False
    src = src.strip()
    if not src.startswith("http"):
        return False
    if src.startswith("data:"):
        return False
    if src.endswith(".svg"):
        return False
    bad_patterns = [
        "pixel", "tracker", "analytics", "beacon",
        "1x1", "blank", "placeholder", "spacer",
        "logo", "icon", "avatar", "favicon"
    ]
    src_lower = src.lower()
    if any(p in src_lower for p in bad_patterns):
        return False
    return True


# --- 🖼 ARTICLE PAGE IMAGE SCRAPER ---
async def fetch_image_from_article(session: aiohttp.ClientSession, article_url: str) -> str | None:
    """
    Fetches the actual article page and extracts the best image from it.
    Priority:
      1. <figure> tag image (usually the main article image)
      2. og:image meta tag
      3. data-src lazy-loaded image
      4. Regular <img> src
    """
    if not article_url:
        return None

    try:
        async with session.get(
            article_url,
            timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")

        # 1. Figure tag
        figure = soup.find("figure")
        if figure:
            img_tag = figure.find("img")
            if img_tag:
                src = img_tag.get("data-src") or img_tag.get("src")
                if is_valid_img(src):
                    logging.info(f"[ArticleScraper] ✅ Figure image found at: '{article_url}'")
                    return src

        # 2. og:image meta tag
        og_img = soup.find("meta", property="og:image")
        if og_img and is_valid_img(og_img.get("content")):
            logging.info(f"[ArticleScraper] ✅ og:image found at: '{article_url}'")
            return og_img["content"]

        # 3. data-src lazy loaded images
        for img_tag in soup.find_all("img"):
            src = img_tag.get("data-src")
            if is_valid_img(src):
                logging.info(f"[ArticleScraper] ✅ data-src image found at: '{article_url}'")
                return src

        # 4. Regular img src
        for img_tag in soup.find_all("img"):
            src = img_tag.get("src")
            if is_valid_img(src):
                logging.info(f"[ArticleScraper] ✅ img src found at: '{article_url}'")
                return src

        logging.info(f"[ArticleScraper] ❌ No image found at: '{article_url}'")
        return None

    except asyncio.TimeoutError:
        logging.error(f"[ArticleScraper] ⏱️ Timeout fetching: '{article_url}'")
        return None
    except aiohttp.ClientError as e:
        logging.error(f"[ArticleScraper] 🌐 Network error for '{article_url}': {e}")
        return None
    except Exception as e:
        logging.error(f"[ArticleScraper] 💥 Unexpected error for '{article_url}': {e}")
        return None


# Whole-candidate words that are NEVER real anime titles (rejected only when
# the ENTIRE candidate is one of these — never stripped from valid titles)
GENERIC_ANIME_CANDIDATES = {
    "web", "anime", "news", "manga", "manhwa", "manhua", "donghua",
    "tv", "show", "series", "official", "update", "updates", "creator",
    "announces", "announced", "trailer", "video", "review", "episode",
    "episodes", "part", "season", "film", "movie", "ova", "special",
    "cast", "staff",
}


def is_generic_anime_candidate(name: str) -> bool:
    """True only when the WHOLE candidate is a generic word (never a title)."""
    if not name:
        return True
    cleaned = name.strip().lower().strip('"\'')
    if not cleaned:
        return True
    return cleaned in GENERIC_ANIME_CANDIDATES


# --- 🔤 REGEX-BASED ANIME NAME EXTRACTOR (MULTI-CANDIDATE) ---
def extract_anime_names(title: str) -> list[str]:
    """
    Extracts ALL possible anime names from the title using multiple patterns.
    Returns them in priority order for fallback searching.
    
    Priority:
      1. Text inside quotes ("" or '') - with and without subtitle
      2. Text before colon :
      3. Text before dash -
      4. Text before season/media words
    """
    title = title.strip()
    candidates = []
    seen = set()  # Avoid duplicates

    # --- Pattern 1: Text inside quotes "" or '' ---
    quoted_match = re.search(r'["\']([^"\']+)["\']', title)
    if quoted_match:
        result = quoted_match.group(1).strip()
        if len(result) > 2 and result not in seen:
            candidates.append(result)
            seen.add(result)
            
            # Also try without subtitle (if colon exists inside quotes)
            if ':' in result:
                base_name = result.split(':')[0].strip()
                if len(base_name) > 2 and base_name not in seen:
                    candidates.append(base_name)
                    seen.add(base_name)

    # --- Pattern 2: Text before colon : ---
    colon_match = re.match(r'^(.+?):', title)
    if colon_match:
        result = colon_match.group(1).strip().strip('"\'')
        if len(result) > 2 and result not in seen:
            candidates.append(result)
            seen.add(result)

    # --- Pattern 3: Text before dash - ---
    dash_match = re.match(r'^(.+?)\s[-–]\s', title)
    if dash_match:
        result = dash_match.group(1).strip().strip('"\'')
        # Skip if it ends with season/episode info
        if not re.search(r'\b(?:season|vol|ep|chapter)\s*\d*$', result, re.IGNORECASE):
            if len(result) > 2 and result not in seen:
                candidates.append(result)
                seen.add(result)

    # --- Pattern 4: Text before season/media-type words ---
    media_match = re.match(
        r'^(.+?)\s+(?:season|anime|manga|manhwa|manhua|donghua|vol|volume|episode|ep|ch|chapter)\b',
        title,
        re.IGNORECASE
    )
    if media_match:
        result = media_match.group(1).strip().strip('"\'')
        if len(result) > 2 and result not in seen:
            candidates.append(result)
            seen.add(result)

    # Drop whole-candidate generic words (e.g. "Web", "Anime") before logging
    # and returning, so AniList never queries them if a better one exists.
    filtered = [c for c in candidates if not is_generic_anime_candidate(c)]
    if len(filtered) != len(candidates):
        logging.info(f"[NameExtract] ⏭️ Skipped {len(candidates) - len(filtered)} generic candidate(s)")
    candidates = filtered

    if not candidates:
        logging.info(f"[NameExtract] ❌ No pattern matched: '{title}'")
    else:
        logging.info(f"[NameExtract] Extracted {len(candidates)} candidate(s) from '{title}':")
        for i, candidate in enumerate(candidates, 1):
            logging.info(f"[NameExtract]   {i}. '{candidate}'")

    return candidates


# --- 🖼 ANILIST GRAPHQL POSTER FETCHER (MULTI-SEARCH) ---
async def get_anilist_poster(session: aiohttp.ClientSession, title: str, retries: int = 3) -> str | None:
    """
    1. Extracts ALL possible anime names from the RSS title
    2. Searches AniList for each candidate in priority order
    3. Returns the FIRST successful match (cover > banner)
    4. Supports both ANIME and MANGA types
    """
    url = 'https://graphql.anilist.co'

    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    query = '''
    query ($search: String) {
      anime: Media (search: $search, type: ANIME, sort: POPULARITY_DESC) {
        bannerImage
        coverImage {
          extraLarge
        }
      }
      manga: Media (search: $search, type: MANGA, sort: POPULARITY_DESC) {
        bannerImage
        coverImage {
          extraLarge
        }
      }
    }
    '''

    # Get ALL candidate names
    search_terms = extract_anime_names(title)

    if not search_terms:
        logging.info(f"[AniList] ⏭️ Skipping — no series name in: '{title}'")
        return None

    # Try each candidate until we find an image
    for search_term in search_terms:
        # Safety net: never query AniList with a generic whole-word candidate.
        if is_generic_anime_candidate(search_term):
            logging.info(f"[AniList] ⏭️ Skipping generic candidate: '{search_term}'")
            continue
        logging.info(f"[AniList] 🔍 Trying: '{search_term}'")

        for attempt in range(1, retries + 1):
            try:
                async with session.post(
                    url,
                    json={'query': query, 'variables': {'search': search_term}},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as response:

                    if response.status == 404:
                        logging.info(f"[AniList] ❌ Not found: '{search_term}'")
                        break  # No retry — candidate not found, try next one

                    if response.status == 429:
                        retry_after = int(response.headers.get('Retry-After', 10))
                        logging.warning(f"[AniList] ⏳ Rate limited. Waiting {retry_after}s ...")
                        await asyncio.sleep(retry_after)
                        continue

                    if response.status in {500, 502, 503, 504}:
                        logging.warning(f"[AniList] ⚠️ AniList server error, retrying... (attempt {attempt}/{retries})")
                        await asyncio.sleep(2 * attempt)
                        continue

                    # Any other non-200 status — log once, do NOT retry as HTTP error
                    if response.status != 200:
                        logging.warning(f"[AniList] ⚠️ HTTP {response.status} for '{search_term}' (attempt {attempt}/{retries})")
                        await asyncio.sleep(2 * attempt)
                        continue

                    # HTTP 200 — parse response, do NOT retry 200 as an HTTP failure
                    try:
                        data = await response.json()
                    except Exception as e:
                        logging.warning(f"[AniList] ⚠️ Failed to parse JSON for '{search_term}': {e}")
                        break  # Parse failure — no retry, try next candidate

                    if 'errors' in data and data['errors']:
                        logging.warning(f"[AniList] ⚠️ AniList API errors for '{search_term}': {data.get('errors')}")

                    data_section = data.get('data', {})

                    # HTTP 200 but no data returned — not a retryable HTTP error
                    if not data_section:
                        logging.info(f"[AniList] ℹ️ No matching result for: '{search_term}'")
                        break  # No retry — move to next candidate

                    # Check anime first, then manga — cover first, banner as fallback
                    for media_type in ['anime', 'manga']:
                        media = data_section.get(media_type)
                        if not media:
                            continue

                        cover = (media.get('coverImage') or {}).get('extraLarge')
                        banner = media.get('bannerImage')

                        if cover:
                            logging.info(f"[AniList] ✅ SUCCESS! {media_type.upper()} cover found for '{search_term}'")
                            return cover
                        elif banner:
                            logging.info(f"[AniList] ✅ SUCCESS! {media_type.upper()} banner found for '{search_term}'")
                            return banner

                    # HTTP 200 but no image found for this candidate — try next candidate
                    logging.info(f"[AniList] ❌ No images for '{search_term}', trying next candidate...")
                    break  # Exit retry loop, move to next search term

            except asyncio.TimeoutError:
                logging.error(f"[AniList] ⏱️ Timeout for '{search_term}' (attempt {attempt}/{retries})")
                if attempt < retries:
                    await asyncio.sleep(2 * attempt)

            except aiohttp.ClientError as e:
                logging.error(f"[AniList] 🌐 Network error for '{search_term}' (attempt {attempt}/{retries}): {e}")
                if attempt < retries:
                    await asyncio.sleep(2 * attempt)

            except Exception as e:
                logging.error(f"[AniList] 💥 Unexpected error for '{search_term}' (attempt {attempt}/{retries}): {e}")
                if attempt < retries:
                    await asyncio.sleep(2 * attempt)

    logging.error(f"[AniList] ❌ All candidates failed for original title: '{title}'")
    return None


# --- 📰 MAIN FETCHER (RSS + Scraper) ---
async def fetch_latest_news() -> list[AnimeNews]:
    """
    Fetches latest news from all sources (RSS + scraper) in the database.

    Image priority per item:
      🥇 AniList poster  — cover first, then banner (anime before manga)
                          Tries ALL regex-extracted candidates, stops at first success
      🥈 Article page    — scraped from the actual news article URL
      🥉 None            — broadcaster sends text-only post
    """
    sources = await db.get_all_sources()
    if not sources:
        logging.info("[Fetcher] No sources configured. Nothing to fetch.")
        return []

    news_items = []

    async with aiohttp.ClientSession() as session:
        for source in sources:
            source_type = source.get("type", "rss")
            source_url = source.get("url", "")

            if source_type == "rss":
                # --- RSS Feed Processing ---
                rss_url = source.get("feed_url") or source_url
                try:
                    async with session.get(
                        rss_url,
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as response:
                        content = await response.text()

                    feed = feedparser.parse(content)

                    for entry in feed.entries[:3]:
                        title = entry.get("title", "No Title")
                        entry_link = entry.get("link", "")
                        entry_guid = entry.get("id") or entry.get("guid") or None

                        # Clean summary text
                        raw_summary = entry.get("summary", "")
                        soup = BeautifulSoup(raw_summary, "html.parser")
                        clean_summary = soup.get_text().strip()
                        if len(clean_summary) > 250:
                            clean_summary = clean_summary[:250] + "..."

                        # 🥇 Try AniList poster first (tries ALL regex candidates)
                        anilist_poster = await get_anilist_poster(session, title)

                        if anilist_poster:
                            final_image = anilist_poster
                            logging.info(f"[Fetcher] 🥇 AniList poster used for: '{title}'")

                        else:
                            # 🥈 All AniList candidates failed, scrape the article page
                            article_image = await fetch_image_from_article(session, entry_link)

                            if article_image:
                                final_image = article_image
                                logging.info(f"[Fetcher] 🥈 Article image used for: '{title}'")
                            else:
                                final_image = None
                                logging.info(f"[Fetcher] 🥉 No image — text-only for: '{title}'")

                        news_items.append(AnimeNews(
                            title=title,
                            link=entry_link,
                            summary=clean_summary,
                            image_url=final_image,
                            source_url=rss_url,
                            guid=entry_guid
                        ))

                except aiohttp.ClientError as e:
                    logging.error(f"[Fetcher] 🌐 Network error for feed '{rss_url}': {e}")
                except Exception as e:
                    logging.error(f"[Fetcher] 💥 Unexpected error for feed '{rss_url}': {e}")

            elif source_type == "scraper":
                # --- Scraper Source Processing ---
                try:
                    items = await scrape_source_for_updates(source)

                    # Update source last_item in DB if we have new items
                    if source.get("last_item"):
                        await db.update_source(source_url, {"last_item": source["last_item"]})

                    for item in items:
                        news_items.append(AnimeNews(
                            title=item.get("title", "No Title"),
                            link=item.get("link", ""),
                            summary="",
                            image_url=None,
                            source_url=source_url
                        ))

                    if items:
                        logging.info(f"[Fetcher] 📄 Scraper returned {len(items)} item(s) from '{source_url}'")

                except Exception as e:
                    logging.error(f"[Fetcher] 💥 Error scraping source '{source_url}': {e}")

    logging.info(f"[Fetcher] ✅ Done. {len(news_items)} item(s) ready to broadcast.")
    return news_items
