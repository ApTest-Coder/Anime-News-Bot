"""
Smart RSS/Feed Detection Module

Provides intelligent feed detection for the /add_rss command:
1. Direct RSS/Atom feed validation
2. RSS auto-discovery from HTML pages
3. Custom scraper fallback for non-RSS websites
"""

import aiohttp
import asyncio
import feedparser
import logging
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
USER_AGENT = "Mozilla/5.0 (compatible; AnimeNewsBot/1.0)"


async def _fetch_url(session: aiohttp.ClientSession, url: str) -> tuple[int, str]:
    """
    Fetch URL and return (status_code, content).
    Returns (0, "") on any failure.
    """
    try:
        async with session.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT}
        ) as resp:
            content = await resp.text()
            return resp.status, content
    except aiohttp.ClientError as e:
        logger.error(f"[RSSDetector] Network error for '{url}': {e}")
        return 0, ""
    except asyncio.TimeoutError:
        logger.error(f"[RSSDetector] Timeout for '{url}'")
        return 0, ""
    except Exception as e:
        logger.error(f"[RSSDetector] Unexpected error for '{url}': {e}")
        return 0, ""


def _is_valid_feed(feed) -> bool:
    """
    Check if a parsed feed is valid.
    A valid feed must have a title and at least one entry with both title and link.
    """
    if feed.bozo and not feed.entries:
        return False
    if not feed.feed.get("title"):
        return False
    if not feed.entries:
        return False
    for entry in feed.entries[:3]:
        if entry.get("link") and entry.get("title"):
            return True
    return False


async def _validate_direct_feed(session: aiohttp.ClientSession, url: str) -> dict | None:
    """
    Try to validate URL as a direct RSS/Atom feed.
    Returns source dict if valid, None otherwise.
    """
    status, content = await _fetch_url(session, url)
    if status != 200 or not content:
        return None

    feed = feedparser.parse(content)

    if _is_valid_feed(feed):
        return {
            "type": "rss",
            "feed_url": url,
            "title": feed.feed.get("title", "Unknown Feed"),
        }
    return None


def _discover_feed_links(html: str, base_url: str) -> list[str]:
    """
    Parse HTML and discover RSS/Atom feed links from <link rel="alternate"> tags.
    Returns list of absolute URLs.
    """
    soup = BeautifulSoup(html, "html.parser")
    feed_links = []

    for link in soup.find_all("link", rel="alternate"):
        link_type = link.get("type", "")
        if "rss" in link_type or "atom" in link_type:
            href = link.get("href")
            if href:
                absolute_url = urljoin(base_url, href)
                feed_links.append(absolute_url)

    return feed_links


async def _discover_feed_from_html(session: aiohttp.ClientSession, url: str) -> dict | None:
    """
    Fetch webpage and try to discover RSS/Atom feeds from auto-discovery tags.
    Each discovered feed is validated before returning.
    """
    status, content = await _fetch_url(session, url)
    if status != 200 or not content:
        return None

    feed_links = _discover_feed_links(content, url)

    for feed_url in feed_links:
        result = await _validate_direct_feed(session, feed_url)
        if result:
            return result

def _scrape_latest_items(html: str, base_url: str) -> list[dict]:
    """
    Scrape latest items from a webpage as fallback.
    Normalizes items into common structure: {"title", "link", "published"}.
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen_links = set()

    # Strategy 1: Look for article elements with headings
    article_selectors = [
        "article",
        ".post",
        ".entry",
        ".article",
    ]

    articles = []
    for selector in article_selectors:
        articles = soup.select(selector)
        if len(articles) >= 2:
            break

    if articles:
        for article in articles[:10]:
            title_tag = article.find(["h1", "h2", "h3", "h4"])
            title = title_tag.get_text(strip=True) if title_tag else ""

            link_tag = article.find("a", href=True)
            link = link_tag.get("href", "") if link_tag else ""

            if link and not link.startswith("http"):
                link = urljoin(base_url, link)

            time_tag = article.find("time")
            published = time_tag.get("datetime", "") if time_tag else ""

            if title and link and link not in seen_links:
                seen_links.add(link)
                items.append({
                    "title": title,
                    "link": link,
                    "published": published,
                })

    # Strategy 2: Look for links that look like article titles
    if not items:
        for a_tag in soup.find_all("a", href=True):
            href = a_tag.get("href", "")
            title = a_tag.get_text(strip=True)

            # Filter out short text (likely navigation)
            if len(title) < 20:
                continue

            # Make absolute URL
            if not href.startswith("http"):
                href = urljoin(base_url, href)

            # Skip non-content URLs
            parsed = urlparse(href)
            skip_extensions = [".css", ".js", ".png", ".jpg", ".gif", ".svg", ".pdf", ".zip"]
            if any(ext in parsed.path.lower() for ext in skip_extensions):
                continue

            # Skip common non-article paths
            skip_patterns = [
                "login", "register", "about", "contact", "privacy", "terms",
                "category", "tag", "author", "page", "wp-content", "wp-includes"
            ]
            if any(p in parsed.path.lower() for p in skip_patterns):
                continue

            if href not in seen_links:
                seen_links.add(href)
                items.append({
                    "title": title,
                    "link": href,
                    "published": "",
                })

            if len(items) >= 10:
                break

    return items


async def _create_scraper_source(session: aiohttp.ClientSession, url: str) -> dict | None:
    """
    Create a custom scraper source for the given URL.
    Returns source dict if scraping succeeds, None otherwise.
    """
    status, content = await _fetch_url(session, url)
    if status != 200 or not content:
        return None

    items = _scrape_latest_items(content, url)

    if items:
        soup = BeautifulSoup(content, "html.parser")
        page_title = soup.title.get_text(strip=True) if soup.title else url

        return {
            "type": "scraper",
            "feed_url": None,
            "title": page_title,
            "scraper_items": items,
        }

    return None


async def detect_and_create_source(url: str) -> dict | None:
    """
    Main detection function. Tries three strategies in order:
    1. Direct RSS/Atom feed validation
    2. RSS auto-discovery from HTML
    3. Custom scraper fallback

    Returns a complete source dict or None if all strategies fail.
    """
    # Normalize URL
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    async with aiohttp.ClientSession() as session:
        # Step 1: Try direct feed
        result = await _validate_direct_feed(session, url)
        if result:
            result["url"] = url
            result["enabled"] = True
            result["created_at"] = datetime.now(timezone.utc)
            result["last_item"] = None
            logger.info(f"[RSSDetector] Direct feed found: {result['title']}")
            return result

        # Step 2: Try auto-discovery
        result = await _discover_feed_from_html(session, url)
        if result:
            result["url"] = url
            result["enabled"] = True
            result["created_at"] = datetime.now(timezone.utc)
            result["last_item"] = None
            logger.info(f"[RSSDetector] Auto-discovered feed: {result['title']}")
            return result

        # Step 3: Fallback to scraper
        result = await _create_scraper_source(session, url)
        if result:
            result["url"] = url
            result["enabled"] = True
            result["created_at"] = datetime.now(timezone.utc)
            result["last_item"] = None
            logger.info(f"[RSSDetector] Scraper source created: {result['title']}")
            return result

    return None


async def scrape_source_for_updates(source: dict) -> list[dict]:
    """
    Scrape a scraper source for current items.
    Returns all current items (deduplication is handled by the broadcaster via posted_news).
    Updates source["last_item"] with the most recent item link.
    """
    url = source.get("url")
    if not url:
        return []

    async with aiohttp.ClientSession() as session:
        status, content = await _fetch_url(session, url)
        if status != 200 or not content:
            return []

        items = _scrape_latest_items(content, url)

        # Update last_item for tracking
        if items:
            source["last_item"] = items[0]["link"]

        return items

