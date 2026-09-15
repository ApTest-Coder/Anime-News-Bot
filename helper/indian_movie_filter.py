"""
Indian Movie / Anime News Filter

Filters RSS feed items to identify anime-related news from Indian sources.
Used to gate which items from feeds like animenewsindia.com become AnimeNews payloads.
"""
import logging
import re
from typing import Optional

logger = logging.getLogger("IndianMovieFilter")

# Known anime series that we want to track from Indian news sources
KNOWN_ANIME_SERIES = [
    # Major Shonen
    "dragon ball", "dragon ball super", "dragon ball gt",
    "one piece", "naruto", "naruto shippuden", "boruto",
    "bleach", "bleach thousand-year blood war",
    "my hero academia", "mha", "boku no hero",
    "attack on titan", "shingeki no kyojin",
    "demon slayer", "kimetsu no yaiba",
    "jujutsu kaisen", "chainsaw man",
    "spy x family",
    "black clover",
    "fire force", "enen no shouboutai",
    "dr stone",
    "komi can't communicate",
    "kaiju no. 8", "kaiju no8",
    "dandadan",
    "worst of evil",
    "sailormoon", "sailor moon", "pretty guardian sailormoon",
    "genshin impact",
    "honkai impact",
    "tower of god",
    "god of high school",
    "orange marmalade",
    "mushoku tensei",
    "re: zero",
    "no game no life",
    "overlord",
    "sword art online", "sao",
    "that time i got reincarnated as a slime", "tensei slime",
    "made in abyss",
    "durarara",
    "fruits basket",
    "haikyuu",
    "kindergarten war",
    "jojo", "jojo's bizarre adventure",
    "code geass",
    "death note",
    "fullmetal alchemist", "fma", "fma brotherhood",
    "pokemon",
    "digimon",
    "yu-gi-oh", "yugioh",
    "venom", "morbius",
    "slayers",
    "shaman king",
    "hunter x hunter",
    "yu yu hakusho",
    "saint seiya", "knights of the zodiac",
    "vfma", "violet evergarden",
]

# Pattern: Series name + regional dub keywords
REGIONAL_DUB_PATTERNS = [
    r"\b(?:dub(?:bed)?\s*.{0,20}(?:hindi|regional|tamil|telugu|malayalam|bengali|marathi|urdu))\b",
    r"\b(?:hindi[_\s]?dub(?:bed)?)\b",
    r"\b(?:regional[_\s]?(?:dub|language))\b",
    r"\b(?:india[/\s].{0,20}(?:dub|release|stream|now\s*playing))\b",
    r"\b(?:jio.?hotstar|hotstar|disney\+(?:hulu)?|zee5|sony[s_]?liv|mx.?player|prime.?video)\b",
    r"\b(?:now\s*streaming|now\s*playing|available\s*on|premieres?|premiere)\b",
    r"\b(?:anime.{0,20}(?:india|indian|regional|dub))\b",
    r"\b(?:indian\s*anime)\b",
    r"\b(?:anime\s*(?:news|updates|exclusive|stories))\b",
    r"\b(?:anime\s*(?:festival|con|convention|event))\b",
]

# Anti-patterns: content that looks like anime news but isn't really
FALSE_POSITIVE_PATTERNS = [
    r"\b(?:bollywood|hollywood|south\s*indian|kollywood|tollywood)\b.*(?:\banime\b)",
    r"\b cinema\s*bazaar\b",
    r"\b filmfare\b",
]

def normalize_title(title: str) -> str:
    """Normalize title for matching: lowercase, strip extra whitespace."""
    return re.sub(r'\s+', ' ', title.lower().strip())


def matches_known_series(title: str) -> Optional[str]:
    """
    Check if title contains a known anime series name.
    Returns the matched series name or None.
    """
    normalized = normalize_title(title)
    for series in KNOWN_ANIME_SERIES:
        if series in normalized:
            return series
    return None


def matches_regional_dub_patterns(title: str, summary: str = "") -> bool:
    """
    Check if title/summary contains regional dub / India release patterns.
    """
    combined = f"{title} {summary}".lower()
    for pattern in REGIONAL_DUB_PATTERNS:
        if re.search(pattern, combined):
            return True
    return False


def matches_false_positives(title: str, summary: str = "") -> bool:
    """
    Check if this looks like a false positive (anime title mentioned but not anime news).
    Returns True if it should be EXCLUDED.
    """
    combined = f"{title} {summary}".lower()
    for pattern in FALSE_POSITIVE_PATTERNS:
        if re.search(pattern, combined):
            return True
    return False


def is_anime_relevant(title: str, summary: str = "", link: str = "") -> bool:
    """
    Determine if an RSS feed item is anime-relevant for Indian news sources.
    
    Filters in items that:
    1. Mention known anime series titles
    2. Mention regional dubs / India releases / streaming availability
    3. Are explicitly about anime news
    
    Filters out:
    - False positives where anime titles appear but context is non-anime
    """
    title_norm = normalize_title(title)
    summary_norm = normalize_title(summary) if summary else ""
    combined = f"{title_norm} {summary_norm}"
    
    # Check for known anime series
    series_match = matches_known_series(title)
    if series_match:
        logger.debug(f"  ✓ Matched known series: '{series_match}' in '{title[:50]}...'")
        return True
    
    # Check regional dub patterns
    if matches_regional_dub_patterns(title, summary):
        logger.debug(f"  ✓ Matched regional dub pattern in '{title[:50]}...'")
        return True
    
    # Check for explicit anime-related keywords
    anime_keywords = [
        r"\banime\s+(?:news|updates|exclusive|review|preview|trailer|announce|release|stream)",
        r"\b(?:new\s+)?anime\s+(?:series|show|movie|film|episode|season|chapter)\b",
        r"\bdub(?:bed)?\s+(?:anime|series|show|episode)\b",
        r"\banime\s+(?:comes|coming|arrives|arriving|lands?|dropping|drops?)\b",
        r"\b(?:finally|now)\s+(?:streaming|available|released|out)\b.{0,100}\banime\b",
        r"\bwatch\s+(?:anime|online)\b",
        r"\banime\s+(?:fans|community|lovers|enthusiasts)\b",
    ]
    for pattern in anime_keywords:
        if re.search(pattern, combined):
            logger.debug(f"  ✓ Matched anime keyword pattern in '{title[:50]}...'")
            return True
    
    # Check for false positives
    if matches_false_positives(title, summary):
        logger.debug(f"  ✗ EXCLUDED: false positive in '{title[:50]}...'")
        return False
    
    logger.debug(f"  ✗ No match for '{title[:50]}...'")
    return False


def filter_indian_anime_news(
    title: str,
    summary: str = "",
    link: str = ""
) -> dict:
    """
    Filter an RSS feed item and return filtering metadata.
    
    Returns:
        {
            "kept": bool,  # Whether to keep this item
            "reason": str,  # Why it was kept or rejected
            "matched_series": Optional[str]
        }
    """
    kept = is_anime_relevant(title, summary, link)
    
    if kept:
        series = matches_known_series(title)
        return {
            "kept": True,
            "reason": f"Matched: {series or 'regional dub / anime keyword'}",
            "matched_series": series,
        }
    else:
        return {
            "kept": False,
            "reason": "No anime relevance found",
            "matched_series": None,
        }