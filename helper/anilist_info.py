"""
AniList anime metadata fetcher for the /info command (Phase 3).

Uses the public AniList GraphQL API (https://graphql.anilist.co).
Only verified data is returned: missing values stay None/empty and the
plugin renders them as "-". No metadata is ever invented.

Includes a small in-memory TTL cache so repeated /info lookups for the
same anime do not hit the API again. No new database/collection.
"""

import asyncio
import logging
import re
import time

import aiohttp

logger = logging.getLogger(__name__)

ANILIST_URL = "https://graphql.anilist.co"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
MAX_RETRIES = 2

_MEDIA_FIELDS = """
      id
      title { romaji english native }
      synonyms
      format
      episodes
      status
      seasonYear
      genres
      countryOfOrigin
      coverImage { extraLarge large }
      studios(isMain: true) { nodes { name } }
      externalLinks { site url }
      description
"""

SEARCH_QUERY = f"""
query SearchAnime($search: String, $perPage: Int) {{
  Page(perPage: $perPage) {{
    media(search: $search, type: ANIME, sort: SEARCH_MATCH, isAdult: false) {{
{_MEDIA_FIELDS}
    }}
  }}
}}
"""

BY_ID_QUERY = f"""
query AnimeById($id: Int) {{
  Media(id: $id, type: ANIME) {{
{_MEDIA_FIELDS}
  }}
}}
"""

# ------------------------------------------------------------------
# In-memory TTL cache (bounded). Reused across requests in one process.
# ------------------------------------------------------------------
_CACHE: dict = {}
_CACHE_TTL = 3600          # 1 hour
_CACHE_MAX = 128           # bounded size


def _cache_get(key):
    entry = _CACHE.get(key)
    if not entry:
        return None
    ts, value = entry
    if time.time() - ts > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return value


def _cache_put(key, value):
    if len(_CACHE) >= _CACHE_MAX:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)
    _CACHE[key] = (time.time(), value)


# ------------------------------------------------------------------
# GraphQL transport with 429 / 5xx handling and bounded retries
# ------------------------------------------------------------------
async def _post(query: str, variables: dict):
    """POST one GraphQL query. Returns data dict or None. Never raises."""
    body = {"query": query, "variables": variables}
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with session.post(ANILIST_URL, json=body) as resp:
                    if resp.status == 429:
                        retry_after = resp.headers.get("Retry-After", "5")
                        try:
                            wait = min(int(retry_after), 15)
                        except ValueError:
                            wait = 5
                        if attempt < MAX_RETRIES:
                            await asyncio.sleep(wait)
                            continue
                        logger.warning("[AniListInfo] rate limited, giving up")
                        return None
                    if resp.status >= 500 and attempt < MAX_RETRIES:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    if resp.status != 200:
                        logger.warning(
                            "[AniListInfo] HTTP %s from AniList", resp.status
                        )
                        return None
                    data = await resp.json(content_type=None)
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                logger.warning("[AniListInfo] request failed: %s", exc)
                return None
            except ValueError:
                logger.warning("[AniListInfo] malformed JSON response")
                return None
        else:
            return None
    if not isinstance(data, dict):
        return None
    errors = data.get("errors")
    if errors:
        logger.warning("[AniListInfo] GraphQL errors: %s", errors[0])
        return None
    return data.get("data")



# ------------------------------------------------------------------
# Response cleaning: normalize a Media node into a flat dict.
# Missing values stay None — never invented.
# ------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text):
    if not text:
        return None
    cleaned = _TAG_RE.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _clean_media(m: dict) -> dict:
    studios = m.get("studios") or {}
    nodes = studios.get("nodes") or []
    links = m.get("externalLinks") or []
    streaming = []
    for link in links:
        site = link.get("site")
        if site and site not in streaming:
            streaming.append(site)
    return {
        "id": m.get("id"),
        "title_english": (m.get("title") or {}).get("english"),
        "title_romaji": (m.get("title") or {}).get("romaji"),
        "title_native": (m.get("title") or {}).get("native"),
        "synonyms": m.get("synonyms") or [],
        "format": m.get("format"),
        "episodes": m.get("episodes"),
        "status": m.get("status"),
        "year": m.get("seasonYear"),
        "genres": m.get("genres") or [],
        "country": m.get("countryOfOrigin"),
        "cover": (m.get("coverImage") or {}).get("extraLarge")
                 or (m.get("coverImage") or {}).get("large"),
        "studio": nodes[0].get("name") if nodes else None,
        "streaming": streaming,
        "description": _strip_html(m.get("description")),
    }


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------
async def search_anime(query: str, per_page: int = 5):
    """
    Search AniList for anime matching `query`.
    Returns a list of cleaned media dicts, [] when nothing matched,
    or None on network/API failure (so callers can distinguish
    "not found" from "service error").
    """
    q = (query or "").strip()
    if not q:
        return []
    key = f"search:{q.lower()}:{per_page}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    data = await _post(SEARCH_QUERY, {"search": q, "perPage": per_page})
    if data is None:
        return None
    page = data.get("Page") or {}
    media = page.get("media") or []
    results = [_clean_media(m) for m in media if m.get("id")]
    _cache_put(key, results)
    return results


async def get_anime_by_id(anilist_id: int):
    """Fetch one anime by AniList ID. Returns dict, None on failure."""
    key = f"id:{anilist_id}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    data = await _post(BY_ID_QUERY, {"id": int(anilist_id)})
    if data is None:
        return None
    m = data.get("Media")
    if not m or not m.get("id"):
        return None
    cleaned = _clean_media(m)
    _cache_put(key, cleaned)
    return cleaned
