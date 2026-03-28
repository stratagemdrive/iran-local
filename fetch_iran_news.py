"""
fetch_iran_news.py - Fixed version with fallback URLs, retry logic,
UA rotation, verbose logging, and no-date tolerance.
"""

import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import requests
from dateutil import parser as dateutil_parser
from deep_translator import GoogleTranslator
from langdetect import detect, LangDetectException

OUTPUT_DIR = Path("docs")
OUTPUT_FILE = OUTPUT_DIR / "iran_news.json"
MAX_PER_CATEGORY = 20
MAX_AGE_DAYS = 7
CATEGORIES = ["Diplomacy", "Military", "Energy", "Economy", "Local Events"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

RSS_SOURCES = [
    {"name": "Tehran Times",       "urls": ["https://www.tehrantimes.com/rss", "https://tehrantimes.com/rss"]},
    {"name": "Iran Daily",         "urls": ["https://irannewsdaily.com/feed", "https://irannewsdaily.com/rss"]},
    {"name": "Financial Tribune",  "urls": ["https://financialtribune.com/rss.xml", "https://financialtribune.com/feed"]},
    {"name": "Press TV",           "urls": ["https://www.presstv.ir/rss", "https://presstv.ir/rss"]},
    {"name": "Mehr News Agency",   "urls": ["https://en.mehrnews.com/rss", "https://mehrnews.com/en/rss"]},
    {"name": "IRNA",               "urls": ["https://en.irna.ir/rss", "https://irna.ir/en/rss"]},
    {"name": "Iran Front Page",    "urls": ["https://ifpnews.com/feed", "https://ifpnews.com/rss"]},
    {"name": "Tasnim News Agency", "urls": ["https://www.tasnimnews.com/en/rss", "https://tasnimnews.com/en/rss"]},
    {"name": "Radio Farda",        "urls": ["https://www.rferl.org/api/epiqegurkiuz", "https://en.radiofarda.com/api/zptmoveregmq"]},
    {"name": "Iran International", "urls": ["https://www.iranintl.com/en/rss", "https://iranintl.com/en/rss"]},
]

CATEGORY_KEYWORDS = {
    "Military": [
        "military","army","irgc","missile","drone","airstrike","air strike","defense","war",
        "weapon","armed forces","navy","air force","general","commander","combat","attack",
        "bomb","strike","operation","basij","revolutionary guard","ballistic","troop","soldier",
        "artillery","radar","intercept","brigade","battalion",
    ],
    "Energy": [
        "oil","gas","petroleum","energy","opec","barrel","refinery","pipeline","natural gas",
        "electricity","power plant","fuel","nuclear energy","uranium","enrichment","reactor",
        "megawatt","hydropower","renewables","solar","wind power","lng","tanker",
    ],
    "Diplomacy": [
        "diplomat","diplomacy","foreign minister","foreign policy","sanctions","nuclear deal",
        "jcpoa","negotiation","agreement","treaty","envoy","ambassador","united nations","un ",
        "talks","ceasefire","relations","bilateral","multilateral","foreign affairs",
        "state department","summit","meeting","eu ","p5+1","security council","araghchi",
        "foreign ministry",
    ],
    "Economy": [
        "economy","economic","gdp","inflation","trade","export","import","investment","market",
        "currency","rial","toman","bank","banking","budget","fiscal","finance","financial",
        "revenue","stock exchange","business","commerce","tariff","customs","growth",
        "recession","unemployment","subsidy","privatization",
    ],
    "Local Events": [
        "tehran","isfahan","mashhad","shiraz","tabriz","qom","province","earthquake","flood",
        "fire","accident","protest","arrest","festival","election","social","culture",
        "education","health","hospital","university","citizen","local","domestic","internal",
        "community","municipality","mayor","judiciary","court","iranian",
    ],
}

def classify_story(title, summary=""):
    text = (title + " " + summary).lower()
    scores = {cat: 0 for cat in CATEGORIES}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                scores[cat] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "Local Events"

_translator = GoogleTranslator(source="auto", target="en")

def translate_to_english(text):
    if not text or not text.strip():
        return text
    try:
        lang = detect(text)
    except LangDetectException:
        lang = "en"
    if lang == "en":
        return text
    try:
        translated = _translator.translate(text[:4900])
        return translated if translated else text
    except Exception:
        return text

def parse_date(entry):
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
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except Exception:
                continue
    return None

def is_recent(dt):
    if dt is None:
        return True  # accept undated stories instead of silently dropping them
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    return dt >= cutoff

def _get_with_retry(url, ua, retries=2):
    headers = {
        "User-Agent": ua,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            if resp.status_code == 200:
                return resp
            print(f"    HTTP {resp.status_code} on attempt {attempt+1} for {url}")
        except requests.RequestException as exc:
            print(f"    Request error on attempt {attempt+1} for {url}: {exc}")
        if attempt < retries:
            time.sleep(2 ** attempt)
    return None

def fetch_feed(source, ua_index=0):
    ua = USER_AGENTS[ua_index % len(USER_AGENTS)]
    raw_content = None

    for url in source["urls"]:
        resp = _get_with_retry(url, ua)
        if resp is not None:
            raw_content = resp.content
            print(f"  OK   [{source['name']}] fetched {url}")
            break
        else:
            print(f"  FAIL [{source['name']}] {url}")

    if raw_content is None:
        print(f"  SKIP [{source['name']}] all URLs failed")
        return []

    feed = feedparser.parse(raw_content)
    total = len(feed.entries)
    stories = []

    for entry in feed.entries:
        raw_title   = (getattr(entry, "title",   "") or "").strip()
        raw_summary = (getattr(entry, "summary", "") or "").strip()
        url         = (getattr(entry, "link",    "") or "").strip()
        dt = parse_date(entry)

        if not url or not raw_title:
            continue
        if not is_recent(dt):
            continue

        title   = translate_to_english(raw_title)
        summary = translate_to_english(re.sub(r"<[^>]+>", " ", raw_summary))
        category = classify_story(title, summary)

        published_date = (
            dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            if dt
            else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )

        stories.append({
            "title":          title,
            "source":         source["name"],
            "url":            url,
            "published_date": published_date,
            "category":       category,
        })
        time.sleep(0.05)

    print(f"  [{source['name']}] {total} feed entries -> {len(stories)} kept")
    return stories

def load_existing():
    if not OUTPUT_FILE.exists():
        print("  No existing JSON — starting fresh.")
        return {cat: [] for cat in CATEGORIES}
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        flat = data.get("stories", [])
        grouped = {cat: [] for cat in CATEGORIES}
        for story in flat:
            cat = story.get("category", "Local Events")
            if cat in grouped:
                grouped[cat].append(story)
        print(f"  Loaded {len(flat)} existing stories.")
        return grouped
    except Exception as exc:
        print(f"  WARN: Could not load existing JSON ({exc}) — starting fresh.")
        return {cat: [] for cat in CATEGORIES}

def merge_stories(existing, fresh):
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    fresh_by_cat = {cat: {} for cat in CATEGORIES}
    for story in fresh:
        cat = story.get("category", "Local Events")
        url = story.get("url", "")
        if url:
            fresh_by_cat[cat][url] = story

    merged = {}
    for cat in CATEGORIES:
        existing_valid = {}
        for s in existing.get(cat, []):
            try:
                dt = dateutil_parser.parse(s["published_date"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt >= cutoff:
                    existing_valid[s["url"]] = s
            except Exception:
                existing_valid[s.get("url", "")] = s

        combined = {**existing_valid, **fresh_by_cat[cat]}

        def sort_key(s):
            try:
                dt = dateutil_parser.parse(s["published_date"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)

        merged[cat] = sorted(combined.values(), key=sort_key, reverse=True)[:MAX_PER_CATEGORY]

    return merged

def save_output(grouped):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    flat = []
    for cat in CATEGORIES:
        flat.extend(grouped.get(cat, []))

    payload = {
        "country":        "iran",
        "generated_at":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_stories":  len(flat),
        "stories":        flat,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n  Wrote {len(flat)} stories to {OUTPUT_FILE}")

def main():
    print("=" * 55)
    print("Iran News Fetcher")
    print(f"Run time (UTC): {datetime.now(timezone.utc).isoformat()}")
    print("=" * 55)

    existing = load_existing()
    print()

    all_fresh = []
    for i, source in enumerate(RSS_SOURCES):
        print(f"Fetching [{i+1}/{len(RSS_SOURCES)}]: {source['name']}")
        stories = fetch_feed(source, ua_index=i)
        all_fresh.extend(stories)
        time.sleep(0.5)

    print(f"\nTotal fresh stories: {len(all_fresh)}")

    merged = merge_stories(existing, all_fresh)

    print("\nCategory counts after merge:")
    for cat in CATEGORIES:
        print(f"  {cat:20s}: {len(merged[cat])}")

    save_output(merged)
    print("\nDone.")

if __name__ == "__main__":
    main()
