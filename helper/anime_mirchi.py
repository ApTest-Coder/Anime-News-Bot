"""
Anime Mirchi scraper - Phase 1 (new article detection + data extraction).

Detects NEW articles on https://animemirchi.com/news/ and extracts structured
article data (title, subtitle, url, dates, author, categories, tags, summary,
body text, images) for the existing news pipeline.

The website is treated as the authoritative source - no RSS dependency.

VERIFIED live HTML structure (ColorMag WordPress theme, "cm-" prefixed classes).
Every selector below was confirmed against real fetched pages:

  Listing page (/news/):
    container   : <article class="post post-<id> category-... tag-...">
    url/title   : header.cm-entry-header h2.cm-entry-title > a[href]
    subtitle    : <br> + <span><i> inside the same <a> (grey italic line)
    date        : time[datetime]
    author      : span.cm-author / a[href*="/author/"]
    categories  : a[href*="/category/"]   (fallback: category-* in class attr)
    tags        : a[href*="/tag/"]        (fallback: tag-* in class attr)
    excerpt     : .cm-entry-summary p
    thumbnail   : img[src] / img[data-src]

  Article page:
    title       : h1.cm-entry-title  (same <br>/<span> subtitle split)
    canonical   : link[rel="canonical"]
    og:image    : meta[property="og:image"]
    published   : meta[property="article:published_time"]
    modified    : meta[property="article:modified_time"]
    author      : [rel="author"] / .cm-author / a[href*="/author/"]
    body        : the .cm-post-content block with the most text

Every selector has attribute/text fallbacks and the article url is the stable
identifier, so normal theme changes degrade gracefully instead of crashing the
scheduler.
"""

import asyncio
import logging
import re
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup, NavigableString

logger = logging.getLogger(__name__)

BASE_URL = "https://animemirchi.com"
NEWS_URL = "https://animemirchi.com/news/"
SOURCE_NAME = "Anime Mirchi"
SOURCE_TYPE = "anime_mirchi"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}

# Lightweight safety/performance bounds (Phase 1 only).
HTTP_TIMEOUT = 15          # seconds per HTTP request
MAX_ATTEMPTS = 2           # 1 initial try + 1 retry (429/503/network only)
RETRY_BACKOFF = 2          # seconds to wait before the single retry
MAX_SUMMARY_CHARS = 500    # cap for summary/excerpt
MAX_BODY_CHARS = 4000      # cap for article body text
MAX_ARTICLES_PER_RUN = 5   # max new articles processed per scheduler cycle


def _abs_url(href: str | None) -> str | None:
    """Resolve a possibly-relative href against BASE_URL. Returns None if empty."""
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith(("#", "javascript:", "mailto:")):
        return None
    return urljoin(BASE_URL + "/", href)


def _split_title_subtitle(anchor) -> tuple:
    """Split '<a>Title<br><span><i>subtitle</i></span></a>' into (title, subtitle)."""
    if anchor is None:
        return "", None
    parts = [s for s in anchor.stripped_strings]
    if not parts:
        return "", None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


async def _get_text(session: aiohttp.ClientSession, url: str) -> tuple:
    """GET url with timeout + 1 retry. Returns (status, text); (0, '') on failure."""
    last_status = 0
    for attempt in range(MAX_ATTEMPTS):
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
                headers=_HEADERS,
                allow_redirects=True,
            ) as resp:
                body = await resp.text()
                if resp.status in (429, 503) and attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(RETRY_BACKOFF)
                    continue
                return resp.status, body
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"[AnimeMirchi] fetch failed ({url}): {e}")
            last_status = 0
            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_BACKOFF)
        except Exception as e:
            logger.error(f"[AnimeMirchi] unexpected fetch error ({url}): {e}")
            return 0, ""
    return last_status, ""


def parse_news_listing(html: str) -> list:
    """Parse /news/ listing HTML into article dicts (no network, newest first)."""
    soup = BeautifulSoup(html, "html.parser")
    items: list = []
    seen: set = set()
    for art in soup.find_all("article"):
        try:
            anchor = art.select_one("h2.cm-entry-title a[href]") or art.find("a", href=True)
            url = _abs_url(anchor.get("href") if anchor else None)
            if not url or url in seen:
                continue
            if urlparse(url).netloc != urlparse(BASE_URL).netloc:
                continue
            title, subtitle = _split_title_subtitle(anchor)
            if not title:
                continue
            time_tag = art.find("time")
            date_iso = (time_tag.get("datetime") if time_tag else None) or None
            author_tag = art.select_one(".cm-author") or art.find("a", href=re.compile(r"/author/"))
            author = author_tag.get_text(strip=True) if author_tag else None
            categories = [a.get_text(strip=True) for a in art.find_all("a", href=re.compile(r"/category/"))]
            tags = [a.get_text(strip=True) for a in art.find_all("a", href=re.compile(r"/tag/"))]
            excerpt_tag = art.select_one(".cm-entry-summary p") or art.select_one(".cm-entry-summary")
            excerpt = excerpt_tag.get_text(" ", strip=True) if excerpt_tag else ""
            img = art.find("img")
            thumb = None
            if img:
                thumb = img.get("data-src") or img.get("src")
                if thumb and thumb.startswith("data:"):
                    thumb = None
            seen.add(url)
            items.append({
                "url": url, "title": title, "subtitle": subtitle,
                "date_iso": date_iso, "author": author,
                "categories": categories, "tags": tags,
                "excerpt": excerpt[:MAX_SUMMARY_CHARS], "thumb": thumb,
            })
        except Exception as e:
            logger.debug(f"[AnimeMirchi] listing card skipped: {e}")
            continue
    return items


def parse_article_page(html: str, page_url: str) -> dict | None:
    """Parse one article page HTML into a full metadata dict (no network)."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        logger.warning(f"[AnimeMirchi] article parse failed ({page_url}): {e}")
        return None
    try:
        h1 = soup.select_one("h1.cm-entry-title") or soup.find("h1")
        title, subtitle = _split_title_subtitle(h1)
        if not title and soup.title:
            title = soup.title.get_text(strip=True)
        if not title:
            return None
        canon = soup.find("link", rel="canonical")
        canonical = canon.get("href").strip() if canon and canon.get("href") else page_url
        og = soup.find("meta", property="og:image")
        og_image = og.get("content").strip() if og and og.get("content") else None
        feat = soup.select_one("img.wp-post-image") or soup.select_one("article img")
        featured = None
        if feat:
            featured = feat.get("data-src") or feat.get("src")
            if featured and featured.startswith("data:"):
                featured = None
        pub = soup.find("meta", property="article:published_time")
        mod = soup.find("meta", property="article:modified_time")
        published_iso = pub.get("content").strip() if pub and pub.get("content") else None
        modified_iso = mod.get("content").strip() if mod and mod.get("content") else None
        if not published_iso:
            t = soup.find("time")
            published_iso = t.get("datetime") if t and t.get("datetime") else None
        author_tag = (soup.find(attrs={"rel": "author"})
                      or soup.select_one(".cm-author")
                      or soup.find("a", href=re.compile(r"/author/")))
        author = author_tag.get_text(strip=True) if author_tag else None
        categories = [a.get_text(strip=True) for a in soup.find_all("a", href=re.compile(r"/category/"))]
        candidates = soup.select(".cm-post-content") or soup.select("article .entry-content")
        best = max((c.get_text(" ", strip=True) for c in candidates), key=len, default="")
        body_text = best[:MAX_BODY_CHARS]
        summ_tag = soup.select_one(".cm-entry-summary")
        summary = summ_tag.get_text(" ", strip=True) if summ_tag else ""
        if not summary:
            summary = body_text[:MAX_SUMMARY_CHARS]
        else:
            summary = summary[:MAX_SUMMARY_CHARS]
        return {
            "url": page_url, "canonical": canonical, "title": title,
            "subtitle": subtitle, "author": author, "categories": categories,
            "published_iso": published_iso, "modified_iso": modified_iso,
            "summary": summary, "body": body_text,
            "image": featured or og_image,
        }
    except Exception as e:
        logger.warning(f"[AnimeMirchi] article extract failed ({page_url}): {e}")
        return None


async def fetch_news_listing(session: aiohttp.ClientSession,
                             page_url: str = NEWS_URL) -> list:
    """Fetch + parse one news listing page. Returns [] on any failure."""
    status, html = await _get_text(session, page_url)
    if status != 200 or not html:
        logger.warning(f"[AnimeMirchi] listing fetch status={status} ({page_url})")
        return []
    return parse_news_listing(html)


async def fetch_article(session: aiohttp.ClientSession,
                        article_url: str) -> dict | None:
    """Fetch + parse one article page. Returns None on any failure."""
    status, html = await _get_text(session, article_url)
    if status != 200 or not html:
        logger.warning(f"[AnimeMirchi] article fetch status={status} ({article_url})")
        return None
    return parse_article_page(html, article_url)


async def get_new_articles(session: aiohttp.ClientSession,
                           last_item: str | None) -> tuple:
    """Detect new articles vs stored last_item (stable article URL).

    Listing is newest-first; everything before last_item is new.
    Unknown last_item (first run): caps to MAX_ARTICLES_PER_RUN.
    Fetches full pages ONLY for new URLs. One failure never stops batch.
    Returns (new_article_dicts, newest_url_seen).
    """
    listing = await fetch_news_listing(session)
    if not listing:
        return [], last_item
    newest_seen = listing[0]["url"]
    if last_item:
        fresh = []
        for card in listing:
            if card["url"] == last_item:
                break
            fresh.append(card)
    else:
        fresh = listing[:MAX_ARTICLES_PER_RUN]
    fresh = fresh[:MAX_ARTICLES_PER_RUN]
    results: list = []
    for card in fresh:
        try:
            full = await fetch_article(session, card["url"])
            if full:
                if card.get("excerpt") and (not full.get("summary") or len(card["excerpt"]) > len(full["summary"])):
                    full["summary"] = card["excerpt"]
                if card.get("thumb") and not full.get("image"):
                    full["image"] = card["thumb"]
                if not full.get("published_iso"):
                    full["published_iso"] = card.get("date_iso")
                results.append(full)
            else:
                results.append({
                    "url": card["url"], "canonical": card["url"],
                    "title": card["title"], "subtitle": card.get("subtitle"),
                    "author": card.get("author"),
                    "categories": card.get("categories", []),
                    "published_iso": card.get("date_iso"),
                    "modified_iso": None,
                    "summary": card.get("excerpt", ""), "body": "",
                    "image": card.get("thumb"),
                })
        except Exception as e:
            logger.warning(f"[AnimeMirchi] skipped article ({card.get('url')}): {e}")
            continue
    return results, newest_seen