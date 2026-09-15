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
import hashlib
import json
import logging
import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlencode, parse_qsl, urlunparse
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

SKIP_EXTENSIONS = [".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".pdf", ".zip", ".ico", ".woff", ".ttf", ".webm", ".mp4"]
# Tracking query parameters to strip during URL normalization
TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "ref", "referrer", "source", "mc_cid")


def normalize_article_url(url: str) -> str:
    """
    Normalize a URL for cross-source duplicate comparison.
    Strips tracking params, fragments; normalizes hostname case and trailing slash.
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return url
        hostname = parsed.netloc.lower()
        filtered = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                    if not k.lower().startswith(TRACKING_PARAMS)]
        new_query = urlencode(filtered)
        path = parsed.path.rstrip("/") if parsed.path else ""
        return urlunparse((parsed.scheme, hostname, path, parsed.params, new_query, ""))
    except Exception:
        return url


def normalize_title(title: str) -> str:
    """
    Normalize a title for duplicate comparison: lowercase, trim, collapse whitespace.
    """
    if not title:
        return ""
    title = re.sub(r"\s+", " ", title.lower().strip())
    title = title.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    return title


def build_dedup_key(title: str, url: str, guid: str = None, published: str = None) -> str:
    """
    Build a stable global deduplication key for an item.
    Priority:
      1. guid / entry id
      2. normalized article URL
      3. hash(title + normalized URL)
      4. hash(normalized title + published date)
    """
    if guid:
        return f"guid:{guid.strip()}"
    norm_url = normalize_article_url(url)
    if norm_url:
        return f"url:{norm_url}"
    norm_title = normalize_title(title)
    if norm_title:
        digest = hashlib.md5(f"{norm_title}|{norm_url}".encode()).hexdigest()
        if published:
            pub_day = str(published)[:10]
            return f"title:{digest}|{pub_day}"
        return f"title:{digest}"
    return None


async def _fetch_url(
    session: aiohttp.ClientSession,
    url: str,
    retries: int = 1,
) -> tuple[int, str]:
    """
    Fetch URL and return (status_code, content).

    - follows redirects
    - sends browser-like headers to reduce blocking
    - retries soft-fail statuses (202/429/503) once with a short delay
    - returns the real HTTP status even for 403/404/429/5xx so callers can
      decide; returns (0, "") only on network/DNS/timeout errors.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
    }
    for attempt in range(retries + 1):
        try:
            async with session.get(
                url,
                timeout=REQUEST_TIMEOUT,
                headers=headers,
                allow_redirects=True
            ) as resp:
                content = await resp.text()
                status = resp.status
                # 202/429/503 may succeed on a retry — but only re-request if
                # we still have attempts left.
                if status in (202, 429, 503) and attempt < retries:
                    await asyncio.sleep(0.5)
                    continue
                return status, content
        except aiohttp.ServerDisconnectedError:
            if attempt < retries:
                await asyncio.sleep(0.5)
                continue
            logger.error(f"[RSSDetector] Server disconnected for '{url}'")
            return 0, ""
        except aiohttp.ClientError as e:
            logger.error(f"[RSSDetector] Network error for '{url}': {e}")
            return 0, ""
        except asyncio.TimeoutError:
            logger.error(f"[RSSDetector] Timeout for '{url}'")
            return 0, ""
        except Exception as e:
            logger.error(f"[RSSDetector] Unexpected error for '{url}': {e}")
            return 0, ""
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


# Titles that are never real article titles (nav, page titles, CTAs)
_GENERIC_TITLES = {
    "news", "home", "login", "log in", "sign in", "sign up", "register",
    "subscribe", "watch now", "watch", "read more", "read full", "view all",
    "load more", "crunchyroll news", "imdb", "anime news", "about us", "about",
    "contact", "contact us", "privacy", "privacy policy", "terms",
    "terms of use", "terms of service", "anime", "web", "menu", "search",
}


def _is_meaningful_title(title: str) -> bool:
    """Reject generic nav/page/CTA titles; keep real article titles."""
    if not title:
        return False
    t = title.strip().lower()
    if len(t) < 5:
        return False
    if t in _GENERIC_TITLES:
        return False
    # Reject pure CTA patterns like "Read more", "See all", "Load more"
    if re.fullmatch(r"(read|view|see|show|load|watch|listen)\s+(more|all|full|now|latest)", t):
        return False
    return True


def _extract_published_from_dict(node: dict) -> str:
    """Find a publication timestamp inside an embedded JSON object."""
    for key in (
        "datePublished", "dateModified", "published", "publishedAt",
        "publishDate", "publishedDate", "createdAt", "updatedAt", "sortDate",
        "date", "releaseDate", "postDate",
    ):
        val = node.get(key)
        if isinstance(val, str) and val:
            dt = _parse_timestamp(val)
            if dt:
                return dt.isoformat()
    # Some sites use nested {"iso": "...", "datetime": "..."}
    for key in ("dateTime", "startDate", "displayDate"):
        val = node.get(key)
        if isinstance(val, str) and val:
            dt = _parse_timestamp(val)
            if dt:
                return dt.isoformat()
    return ""


def _walk_embedded_articles(node, base_url: str):
    """
    Recursively walk an embedded JSON structure (Next.js __NEXT_DATA__,
    application/json, etc.) and yield article-like dictionaries that contain
    a title + url pair.
    """
    if isinstance(node, dict):
        title = node.get("headline") or node.get("title") or node.get("name") or node.get("label") or ""
        url = (
            node.get("url") or node.get("link") or node.get("href")
            or node.get("canonicalLink") or ""
        )
        if not url:
            # Some sites (e.g. Crunchyroll) store the article path in `slug`
            slug = node.get("slug")
            if isinstance(slug, str) and (slug.startswith("/") or slug.startswith("http")):
                url = slug
        if (isinstance(title, str) and isinstance(url, str) and url and title):
            if not str(url).startswith("http"):
                url = urljoin(base_url, str(url))
            item = {
                "title": str(title).strip(),
                "link": url,
                "published": _extract_published_from_dict(node),
            }
            if _is_meaningful_title(item["title"]) and _is_valid_article_url(item["link"], base_url):
                yield item
        for value in node.values():
            yield from _walk_embedded_articles(value, base_url)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_embedded_articles(value, base_url)


def _extract_embedded_json_items(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """
    Extract articles from common embedded JSON page-state structures:
      - <script id="__NEXT_DATA__"> (Next.js / Crunchyroll)
      - <script id="__APP_DATA__"> / other page-state / apollo-state ids
      - <script type="application/json">
      - other JSON script bodies (fallback sweep)
    Returns normalized items: {"title", "link", "published"}.
    """
    items = []
    seen = set()

    def add_unique(article):
        key = article["link"]
        if key not in seen:
            seen.add(key)
            items.append(article)

    id_hints = ("next_data", "app_data", "apollo", "page_state", "pagedata", "state")
    primary = []

    for script in soup.find_all("script"):
        script_id = (script.get("id") or "").lower()
        script_type = (script.get("type") or "").lower()
        if "ld+json" in script_type:
            continue  # handled by JSON-LD strategy
        is_id_hint = any(hint in script_id for hint in id_hints)
        in_type_json = "json" in script_type
        if not (is_id_hint or in_type_json):
            continue
        primary.append(script)

    def extract(script):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            return
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        for article in _walk_embedded_articles(data, base_url):
            add_unique(article)

    for script in primary:
        extract(script)

    # Fallback sweep: try remaining scripts whose body parses as JSON. This
    # handles Next.js pages served with a bare `<script>` containing state JSON.
    if not items:
        for script in soup.find_all("script"):
            raw = script.string or script.get_text()
            if not raw or not raw.strip():
                continue
            stripped = raw.strip()
            if not (stripped.startswith("{") or stripped.startswith("[")):
                continue
            extract(script)
            if items:
                break

    if items:
        logger.info(f"[RSSDetector] Embedded JSON found {len(items)} candidate item(s)")
    return items


def _scrape_latest_items(html: str, base_url: str) -> list[dict]:
    """
    Scrape latest items from a webpage as fallback.
    Runs ALL extraction strategies cumulatively — a failure (or low yield) in
    one strategy NEVER stops later strategies:
      JSON-LD → embedded JSON → <article>/cards → headings → article links
    Normalizes items into common structure: {"title", "link", "published"}.
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen_links = set()
    base_domain = urlparse(base_url).netloc
    is_crunchyroll = "crunchyroll" in base_domain

    def add_item(title: str, link: str, published: str = ""):
        if not title or not link:
            return
        title = title.strip()
        link = link.strip()
        if len(title) < 5 or len(link) < 10:
            return
        if link in seen_links:
            return
        if not _is_meaningful_title(title):
            return
        if not _is_valid_article_url(link, base_url):
            return
        # Crunchyroll tag/listing pages: only real /news/ or /article/ URLs.
        if is_crunchyroll and "/news/" not in link and "/article/" not in link:
            return
        seen_links.add(link)
        items.append({
            "title": title,
            "link": link,
            "published": published,
        })

    # Strategy 0: JSON-LD structured data
    for item in _extract_jsonld_items(soup, base_url):
        add_item(item["title"], item["link"], item["published"])
    if len(items) >= 10:
        return items[:10]

    # Strategy 0b: embedded JSON page-state (__NEXT_DATA__, application/json)
    for item in _extract_embedded_json_items(soup, base_url):
        add_item(item["title"], item["link"], item["published"])
    if len(items) >= 10:
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
    if len(items) >= 10:
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
    if len(items) >= 10:
        return items[:10]

    # Strategy 3: Links that look like article titles
    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "")
        title = a_tag.get_text(strip=True)

        if len(title) < 20:
            continue

        if not href.startswith("http"):
            href = urljoin(base_url, href)

        add_item(title, href)

        if len(items) >= 10:
            break

    # Strategy 3b: listing/tag page <a href> fallback (Crunchyroll /news/,
    # IMDb /news/..., tag/list/category pages). Runs only on news-like pages.
    # Uses a lower title threshold and enriches weak anchor text from the
    # nearest heading / aria-label / title attribute. Everything else still
    # goes through add_item() guards (URL validation, generic-title filter,
    # Crunchyroll /news/ preference, dedup).
    is_news_listing = (
        is_crunchyroll
        or any(seg in base_url for seg in ("/news/", "/tag/", "/list/", "/category/", "/category_listing"))
    )
    if is_news_listing and len(items) < 10:
        # --- Crunchyroll / Next.js shell detection ---
        # Crunchyroll tag pages (e.g. /news/tag/india, /news/tag/Hindi%20Dub)
        # return a Next.js shell with ZERO <a href> article links in the
        # server response. Article data is loaded client-side via JavaScript
        # and the HTML is also behind Cloudflare bot protection.
        # Do NOT treat this as a scraper failure — detect it explicitly so
        # /test_post prints the correct diagnostic instead of a generic
        # "no articles" message.
        is_nextjs_shell = False
        is_clientside_rendered = False
        is_soft_404 = False

        if is_crunchyroll:
            a_tag_count = body.count("<a")
            has_next_static = "/_next/static/" in body or "/build/_next/" in body
            is_nextjs_shell = has_next_static and a_tag_count < 5
            is_clientside_rendered = is_nextjs_shell
            is_soft_404 = "could not be found" in body.lower()

        if is_nextjs_shell:
            logger.info(
                "[RSSDetector] %s — Next.js/CSR shell, no article data in server HTML; "
                "article links are loaded client-side and/or protected by bot challenge.",
                base_url,
            )
        elif is_soft_404:
            logger.info(
                "[RSSDetector] %s — server returned 200 but page indicates content not found.",
                base_url,
            )
        else:
            for a_tag in soup.find_all("a", href=True):
                href = a_tag.get("href", "")
                if not href.startswith("http"):
                    href = urljoin(base_url, href)

                title = a_tag.get_text(" ", strip=True)
                if len(title) < 8:
                    # Enrich weak anchor text from nearby heading, then attributes
                    heading = a_tag.find_parent(["h1", "h2", "h3", "h4"])
                    if heading:
                        heading_text = heading.get_text(" ", strip=True)
                        if len(heading_text) >= 8:
                            title = heading_text
                    if len(title) < 8:
                        title = a_tag.get("aria-label") or a_tag.get("title") or title

                add_item(title, href)

                if len(items) >= 10:
                    break

    if items:
        logger.info(f"[RSSDetector] Scraper found {len(items)} item(s) on {base_url}")
    else:
        logger.warning(f"[RSSDetector] No items found on page: {base_url}")

    return items[:10]


def _normalize_feed_items(feed, source_url: str) -> list[dict]:
    """
    Convert feedparser entries into the same normalized item structure:
    {"title", "link", "published"}.
    """
    items = []
    for entry in feed.entries[:10]:
        title = entry.get("title", "")
        link = entry.get("link", "")
        if not title or not link:
            continue
        published = ""
        published_struct = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
        if published_struct:
            try:
                published_dt = datetime(*published_struct[:6], tzinfo=timezone.utc)
                published = published_dt.isoformat()
            except (TypeError, ValueError):
                published = ""
        items.append({
            "title": title.strip(),
            "link": link,
            "published": published,
        })
    return items


def _try_parse_feed_items(content: str, source_url: str) -> list[dict] | None:
    """
    Attempt to parse content as an RSS/Atom feed.
    Returns normalized items if a valid feed with entries is found, else None.
    """
    if not content or not content.strip():
        return None
    try:
        feed = feedparser.parse(content)
        if feed.entries:
            return _normalize_feed_items(feed, source_url)
    except Exception as e:
        logger.debug(f"[RSSDetector] Feed parse skipped for {source_url}: {e}")
    return None


async def _create_scraper_source(session: aiohttp.ClientSession, url: str) -> dict | None:
    """
    Create a custom scraper source for the given URL.
    Returns source dict if scraping succeeds, None otherwise.
    """
    status, content = await _fetch_url(session, url)
    if not (200 <= status < 300) or not content:
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
        if not (200 <= status < 300) or not content:
            return []

        items = _scrape_latest_items(content, url)

        # Update last_item for tracking
        if items:
            source["last_item"] = items[0]["link"]

        return items

