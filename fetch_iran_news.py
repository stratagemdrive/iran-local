"""
fetch_iran_news.py

Pulls RSS headlines from Iranian news sources, translates non-English content
to English, categorizes each story, and writes/updates docs/iran_news.json.

Target: 20 stories per category (Diplomacy, Military, Energy, Economy, Local Events).
- Stories older than 7 days are dropped.
- If fewer than 20 new relevant stories exist, only available stories are used.
- Oldest entries are replaced first when new stories arrive.
"""

import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import requests
from dateutil import parser as dateutil_parser
from deep_translator import GoogleTranslator
from langdetect import detect, LangDetectException

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OUTPUT_DIR = Path("docs")
OUTPUT_FILE = OUTPUT_DIR / "iran_news.json"

MAX_PER_CATEGORY = 20
MAX_AGE_DAYS = 7

CATEGORIES = ["Diplomacy", "Military", "Energy", "Economy", "Local Events"]

# RSS feed sources — all publicly available, no API keys required.
# Al-Monitor is paywalled and has been replaced with Tasnim News Agency.
RSS_SOURCES = [
    {
        "name": "Tehran Times",
        "url": "https://www.tehrantimes.com/rss",
    },
    {
        "name": "Iran Daily",
        "url": "https://irannewsdaily.com/feed",
    },
    {
        "name": "Financial Tribune",
        "url": "https://financialtribune.com/rss.xml",
    },
    {
        "name": "Press TV",
        "url": "https://www.presstv.ir/rss",
    },
    {
        "name": "Mehr News Agency",
        "url": "https://en.mehrnews.com/rss",
    },
    {
        "name": "IRNA",
        "url": "https://en.irna.ir/rss",
    },
    {
        "name": "Iran Front Page",
        "url": "https://ifpnews.com/feed",
    },
    {
        "name": "Tasnim News Agency",   # Replacement for Al-Monitor (paywalled)
        "url": "https://www.tasnimnews.com/en/rss",
    },
    {
        "name": "Radio Farda",
        "url": "https://en.radiofarda.com/api/zptmoveregmq",
    },
    {
        "name": "Iran International",
        "url": "https://www.iranintl.com/en/rss",
    },
]

# ---------------------------------------------------------------------------
# Keyword-based category classifier
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS = {
    "Military": [
        "military", "army", "irgc", "missile", "drone", "airstrike", "air strike",
        "defense", "war", "weapon", "armed forces", "navy", "air force", "general",
        "commander", "combat", "attack", "bomb", "strike", "operation", "basij",
        "revolutionary guard", "ballistic", "nuclear warhead", "troop",
    ],
    "Energy": [
        "oil", "gas", "petroleum", "energy", "opec", "barrel", "refinery",
        "pipeline", "natural gas", "electricity", "power plant", "fuel",
        "nuclear energy", "uranium", "enrichment", "reactor", "megawatt",
        "hydropower", "renewables", "solar", "wind power",
    ],
    "Diplomacy": [
        "diplomat", "diplomacy", "foreign minister", "foreign policy", "sanctions",
        "nuclear deal", "jcpoa", "negotiation", "agreement", "treaty", "envoy",
        "ambassador", "united nations", "un", "talks", "ceasefire", "relations",
        "bilateral", "multilateral", "foreign affairs", "state department",
        "summit", "meeting", "eu", "p5+1",
    ],
    "Economy": [
        "economy", "economic", "gdp", "inflation", "trade", "export", "import",
        "investment", "market", "currency", "rial", "toman", "bank", "banking",
        "budget", "fiscal", "finance", "financial", "revenue", "stock",
        "business", "commerce", "tariff", "customs",
    ],
    "Local Events": [
        "tehran", "isfahan", "mashhad", "shiraz", "tabriz", "qom", "province",
        "earthquake", "flood", "fire", "accident", "protest", "arrest",
        "festival", "election", "social", "culture", "education", "health",
        "hospital", "university", "citizen", "local", "domestic", "internal",
        "community", "municipality", "mayor",
    ],
}


def classify_story(title: str, summary: str = "") -> str:
    """Return the best-matching category for a story based on keywords."""
    text = (title + " " + summary).lower()
    scores = {cat: 0 for cat in CATEGORIES}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                scores[cat] += 1
    # Return the highest-scoring category; fall back to Local Events
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "Local Events"


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------

_translator = GoogleTranslator(source="auto", target="en")


def translate_to_english(text: str) -> str:
    """Translate text to English if it is not already English."""
    if not text or not text.strip():
        return text
    try:
        lang = detect(text)
    except LangDetectException:
        lang = "en"
    if lang == "en":
        return text
    try:
        # GoogleTranslator has a 5000-char limit; truncate for classification
        chunk = text[:4900]
        translated = _translator.translate(chunk)
        return translated if translated else text
    except Exception:
        return text


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def parse_date(entry) -> datetime | None:
    """Extract a timezone-aware datetime from a feedparser entry."""
    for attr in ("published", "updated", "created"):
        raw = getattr(entry, attr, None)
        if raw:
            try:
                dt = dateutil_parser.parse(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                continue
    # Fall back to feedparser's parsed tuple
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def is_recent(dt: datetime | None) -> bool:
    """Return True if dt is within the last MAX_AGE_DAYS days."""
    if dt is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    return dt >= cutoff


# ---------------------------------------------------------------------------
# RSS fetching
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; IranNewsBot/1.0; "
        "+https://stratagemdrive.github.io/iran-local/)"
    )
}


def fetch_feed(source: dict) -> list[dict]:
    """Fetch and parse one RSS feed; return a list of story dicts."""
    stories = []
    try:
        response = requests.get(source["url"], headers=HEADERS, timeout=20)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
    except Exception as exc:
        print(f"  [WARN] Could not fetch {source['name']}: {exc}")
        return stories

    for entry in feed.entries:
        raw_title = getattr(entry, "title", "") or ""
        raw_summary = getattr(entry, "summary", "") or ""
        url = getattr(entry, "link", "") or ""
        dt = parse_date(entry)

        if not is_recent(dt):
            continue
        if not url or not raw_title:
            continue

        # Translate if needed
        title = translate_to_english(raw_title.strip())
        summary = translate_to_english(re.sub(r"<[^>]+>", " ", raw_summary).strip())

        category = classify_story(title, summary)
        published_date = dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""

        stories.append(
            {
                "title": title,
                "source": source["name"],
                "url": url,
                "published_date": published_date,
                "category": category,
            }
        )
        time.sleep(0.1)  # be polite

    print(f"  [{source['name']}] {len(stories)} recent stories fetched")
    return stories


# ---------------------------------------------------------------------------
# JSON persistence helpers
# ---------------------------------------------------------------------------

def load_existing() -> dict[str, list[dict]]:
    """Load the current JSON file, grouped by category."""
    if not OUTPUT_FILE.exists():
        return {cat: [] for cat in CATEGORIES}
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Expect {"stories": [...]} at the top level
        flat = data.get("stories", [])
        grouped: dict[str, list[dict]] = {cat: [] for cat in CATEGORIES}
        for story in flat:
            cat = story.get("category", "Local Events")
            if cat in grouped:
                grouped[cat].append(story)
        return grouped
    except Exception as exc:
        print(f"  [WARN] Could not load existing JSON: {exc}")
        return {cat: [] for cat in CATEGORIES}


def merge_stories(existing: dict[str, list[dict]], fresh: list[dict]) -> dict[str, list[dict]]:
    """
    Merge fresh stories into existing buckets.
    - Drop stories older than MAX_AGE_DAYS from the existing set.
    - Deduplicate by URL.
    - Keep at most MAX_PER_CATEGORY per category, replacing oldest first.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)

    # Group fresh stories by category, keyed by URL for deduplication
    fresh_by_cat: dict[str, dict[str, dict]] = {cat: {} for cat in CATEGORIES}
    for story in fresh:
        cat = story.get("category", "Local Events")
        url = story.get("url", "")
        if url:
            fresh_by_cat[cat][url] = story

    merged: dict[str, list[dict]] = {}
    for cat in CATEGORIES:
        # Filter existing: drop old and build url-keyed dict
        existing_valid = {}
        for s in existing.get(cat, []):
            try:
                dt = dateutil_parser.parse(s["published_date"]).replace(tzinfo=timezone.utc)
                if dt >= cutoff:
                    existing_valid[s["url"]] = s
            except Exception:
                pass

        # Merge: fresh stories take precedence (overwrite by URL)
        combined = {**existing_valid, **fresh_by_cat[cat]}

        # Sort descending by date; newest first
        def sort_key(s):
            try:
                return dateutil_parser.parse(s["published_date"])
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)

        sorted_stories = sorted(combined.values(), key=sort_key, reverse=True)

        # Trim to MAX_PER_CATEGORY
        merged[cat] = sorted_stories[:MAX_PER_CATEGORY]

    return merged


def save_output(grouped: dict[str, list[dict]]) -> None:
    """Flatten the grouped stories and write to OUTPUT_FILE."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    flat = []
    for cat in CATEGORIES:
        flat.extend(grouped.get(cat, []))

    payload = {
        "country": "iran",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_stories": len(flat),
        "stories": flat,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n  Wrote {len(flat)} stories to {OUTPUT_FILE}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=== Iran News Fetcher ===")
    print(f"Run time (UTC): {datetime.now(timezone.utc).isoformat()}\n")

    # 1. Load what we already have
    existing = load_existing()

    # 2. Fetch all feeds
    all_fresh: list[dict] = []
    for source in RSS_SOURCES:
        print(f"Fetching: {source['name']} ...")
        stories = fetch_feed(source)
        all_fresh.extend(stories)

    print(f"\nTotal fresh stories across all feeds: {len(all_fresh)}")

    # 3. Merge with existing data
    merged = merge_stories(existing, all_fresh)

    # 4. Print per-category summary
    print("\nCategory counts after merge:")
    for cat in CATEGORIES:
        print(f"  {cat}: {len(merged[cat])}")

    # 5. Save
    save_output(merged)
    print("\nDone.")


if __name__ == "__main__":
    main()
