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
import json
import logging
import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Common CSS selectors for article/news cards (generic, works across many sites)
ARTICLE_SELECTORS = [
    "article",
    "[class*='article']",
    "[class*='post-card']",
    "[class*='article-card']",
    "[class*='news-card']",
    "[class*='news-item']",
    "[class*='story']",
    "[class*='entry']",
    "[class*='item']",
    "[class*='card']",
    "[class*='content-item']",
]

# Patterns to reject (navigation, login, static assets, etc.)
SKIP_PATTERNS = [
    "login", "register", "signin", "signup", "account", "profile",
    "wp-content", "wp-includes", "wp-admin", "admin",
    "category", "tag", "author", "page", "feed", "rss",
    "privacy", "terms", "about", "contact", "search",
    "javascript:", "mailto:", "#", "facebook", "twitter",
    "instagram", "youtube", "tiktok", "linkedin",
]

SKIP_EXTENSIONS = [".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".zip", ".ico", ".woff", ".ttf"]


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


def _parse_timestamp(ts_str: str) -> datetime | None:
    """
    Parse a timestamp string into a timezone-aware datetime.
    Supports ISO 8601, RFC 2822, and common formats.
    Returns None if parsing fails.
    """
    if not ts_str or not isinstance(ts_str, str):
        return None

    ts_str = ts_str.strip()

    # Try ISO 8601 formats
    iso_formats = [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in iso_formats:
        try:
            dt = datetime.strptime(ts_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue

    # Try RFC 2822 (email format)
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        pass

    return None


def _is_valid_article_url(url: str, base_url: str) -> bool:
    """
    Check if a URL looks like a valid article link.
    Rejects navigation, login, static assets, etc.
    """
    if not url:
        return False

    parsed = urlparse(url)
    path = parsed.path.lower()

    if parsed.scheme not in ("http", "https"):
        return False

    if any(path.endswith(ext) for ext in SKIP_EXTENSIONS):
        return False

    path_parts = path.strip("/").split("/")
    if any(part in SKIP_PATTERNS for part in path_parts):
        return False

    if len(path) < 2:
        return False

    return True


def _extract_jsonld_items(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """
    Extract articles from JSON-LD structured data.
    Supports Article, NewsArticle, BlogPosting, and ItemList schemas.
    """
    items = []

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
        except (json.JSONDecodeError, TypeError):
            continue

        if not isinstance(data, dict):
            continue

        # Single article
        if data.get("@type") in ("Article", "NewsArticle", "BlogPosting"):
            item = _parse_jsonld_article(data, base_url)
            if item:
                items.append(item)

        # ItemList with itemListElement
        if data.get("@type") == "ItemList" and "itemListElement" in data:
            for element in data["itemListElement"]:
                if isinstance(element, dict):
                    if element.get("@type") in ("Article", "NewsArticle", "BlogPosting"):
                        item = _parse_jsonld_article(element, base_url)
                        if item:
                            items.append(item)
                    elif "url" in element:
                        url = element.get("url", "")
                        name = element.get("name", "")
                        if url and name:
                            if not url.startswith("http"):
                                url = urljoin(base_url, url)
                            items.append({
                                "title": name,
                                "link": url,
                                "published": "",
                            })

        # Graph format (multiple entities)
        if "@graph" in data:
            for entity in data["@graph"]:
                if isinstance(entity, dict) and entity.get("@type") in ("Article", "NewsArticle", "BlogPosting"):
                    item = _parse_jsonld_article(entity, base_url)
                    if item:
                        items.append(item)

    return items


def _parse_jsonld_article(data: dict, base_url: str) -> dict | None:
    """Parse a single JSON-LD article object into our normalized format."""
    title = data.get("headline") or data.get("name", "")
    url = data.get("url", "")

    if not title or not url:
        return None

    if not url.startswith("http"):
        url = urljoin(base_url, url)

    date_published = data.get("datePublished", "")
    date_modified = data.get("dateModified", "")
    published_str = date_published or date_modified
    published_dt = _parse_timestamp(published_str) if published_str else None
    published = published_dt.isoformat() if published_dt else ""

    return {
        "title": title.strip(),
        "link": url,
        "published": published,
    }


def _scrape_latest_items(html: str, base_url: str) -> list[dict]:
    """
    Scrape latest items from a webpage as fallback.
    Uses multiple strategies: JSON-LD, article elements, headings, links.
    Normalizes items into common structure: {"title", "link", "published"}.
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen_links = set()

    def add_item(title: str, link: str, published: str = ""):
        if not title or not link:
            return
        title = title.strip()
        link = link.strip()
        if len(title) < 5 or len(link) < 10:
            return
        if link in seen_links:
            return
        if not _is_valid_article_url(link, base_url):
            return
        seen_links.add(link)
        items.append({
            "title": title,
            "link": link,
            "published": published,
        })

    # Strategy 0: JSON-LD structured data
    jsonld_items = _extract_jsonld_items(soup, base_url)
    for item in jsonld_items:
        add_item(item["title"], item["link"], item["published"])
    if items:
        logger.info(f"[RSSDetector] JSON-LD found {len(items)} item(s)")
        return items[:10]

    # Strategy 1: Article elements with headings
    for selector in ARTICLE_SELECTORS:
        elements = soup.select(selector)
        if len(elements) >= 2:
            for elem in elements[:10]:
                title_tag = elem.find(["h1", "h2", "h3", "h4"])
                title = title_tag.get_text(strip=True) if title_tag else ""

                link_tag = elem.find("a", href=True)
                link = link_tag.get("href", "") if link_tag else ""

                if link and not link.startswith("http"):
                    link = urljoin(base_url, link)

                time_tag = elem.find("time")
                published = ""
                if time_tag:
                    published = time_tag.get("datetime", "") or time_tag.get_text(strip=True)

                add_item(title, link, published)
            if items:
                logger.info(f"[RSSDetector] Article elements found {len(items)} item(s)")
                return items[:10]

    # Strategy 2: Headings with adjacent links
    for heading_tag in soup.find_all(["h1", "h2", "h3"]):
        title = heading_tag.get_text(strip=True)
        if len(title) < 15:
            continue

        link_tag = heading_tag.find("a", href=True)
        if not link_tag:
            parent = heading_tag.parent
            if parent:
                link_tag = parent.find("a", href=True)

        if link_tag:
            link = link_tag.get("href", "")
            if link and not link.startswith("http"):
                link = urljoin(base_url, link)
            add_item(title, link)

    if items:
        logger.info(f"[RSSDetector] Headings found {len(items)} item(s)")
        return items[:10]

    # Strategy 3: Links that look like article titles
    base_domain = urlparse(base_url).netloc
    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "")
        title = a_tag.get_text(strip=True)

        if len(title) < 20:
            continue

        if not href.startswith("http"):
            href = urljoin(base_url, href)

        if not _is_valid_article_url(href, base_url):
            continue

        # For Crunchyroll, prefer /news/ URLs
        if "crunchyroll" in base_domain:
            if "/news/" not in href and "/article/" not in href:
                continue

        add_item(title, href)

        if len(items) >= 10:
            break

    if items:
        logger.info(f"[RSSDetector] Links found {len(items)} item(s)")
    else:
        logger.warning(f"[RSSDetector] No items found on page: {base_url}")

    return items[:10]


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


def _validate_url(url: str) -> bool:
    """
    Validate URL format and block private/internal networks.
    Prevents SSRF attacks.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        # Block private/internal IP ranges
        import ipaddress
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_reserved:
                return False
        except ValueError:
            pass  # Not an IP, it's a domain name
        # Block localhost
        if hostname in ("localhost", "127.0.0.1", "::1"):
            return False
        return True
    except Exception:
        return False


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

    # Validate URL
    if not _validate_url(url):
        logger.warning(f"[RSSDetector] Invalid or blocked URL: {url}")
        return None

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

