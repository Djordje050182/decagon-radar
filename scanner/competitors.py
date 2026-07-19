"""Decagon Radar — competitor watch.

Sweeps competitor first-party blogs (RSS) and Google News for launches,
customer wins, funding, and frontier-lab feature overlap relevant to
Decagon's market (AI customer-support / voice agents). Claude Haiku filters
irrelevant items and drafts a suggested positioning angle.

Outputs:
  data/competitor-news.json   accumulated items (committed)
  digest_competitors.md       new-item digest fragment (not committed)

Usage:
  python scanner/competitors.py            # daily incremental (last 3 days)
  python scanner/competitors.py --backfill # first run (last 30 days)
"""

import argparse
import calendar
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import anthropic

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HAIKU = "claude-haiku-4-5"
MAX_ITEMS_PER_RUN = 60

SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {
            "type": "boolean",
            "description": "True only if this matters to Decagon's market: AI customer-support/voice agents, enterprise agent platforms, or a direct competitor move",
        },
        "type": {
            "type": "string",
            "enum": ["product_launch", "customer_win", "funding", "partnership", "frontier_overlap", "other"],
        },
        "summary": {"type": "string", "description": "Two sentences max, factual"},
        "positioning": {
            "type": "string",
            "description": "One or two sentences: how Decagon could position against this. Empty string if not applicable.",
        },
    },
    "required": ["relevant", "type", "summary", "positioning"],
    "additionalProperties": False,
}


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def entry_date(entry):
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
    return None


def clean(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def collect(competitor, cutoff):
    """Yield candidate items from first-party feeds + Google News."""
    urls = list(competitor["feeds"])
    q = urllib.parse.quote(competitor["news_query"])
    urls.append(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en")

    for url in urls:
        first_party = "news.google.com" not in url
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"  ! feed failed {url}: {e}")
            continue
        for entry in feed.entries[:30]:
            date = entry_date(entry)
            if date and date < cutoff:
                continue
            yield {
                "company": competitor["name"],
                "title": clean(entry.get("title", "")),
                "url": entry.get("link", ""),
                "source": "blog" if first_party else clean(getattr(entry, "source", {}).get("title", "") if hasattr(entry, "source") else "") or "news",
                "published": date.isoformat(timespec="seconds") if date else None,
                "snippet": clean(entry.get("summary", ""))[:1500],
            }


def analyse(client, item):
    prompt = (
        "You are a competitive-intelligence analyst for Decagon (decagon.ai), which builds "
        "AI agents for enterprise customer support (chat and voice). Assess the news item "
        "below about a competitor or frontier AI lab.\n\n"
        "Only mark it relevant if it genuinely matters to Decagon's market: AI customer-support "
        "or voice-agent products, enterprise agent platforms, competitor customer wins, funding, "
        "or frontier-lab releases that overlap Decagon's space. General model news, consumer "
        "features, music/media tools, and unrelated corporate news are NOT relevant.\n\n"
        "If relevant, classify it, summarise it factually, and suggest a positioning angle — "
        "how Decagon might credibly position against it (this is an AI-suggested angle, "
        "not official messaging; keep it grounded, no spin).\n\n"
        f"Company: {item['company']}\nHeadline: {item['title']}\nSource: {item['source']}\n"
        f"Snippet: {item['snippet']}"
    )
    response = client.messages.create(
        model=HAIKU,
        max_tokens=512,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


TYPE_LABELS = {
    "product_launch": "Launch",
    "customer_win": "Customer win",
    "funding": "Funding",
    "partnership": "Partnership",
    "frontier_overlap": "Frontier overlap",
    "other": "News",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true", help="scan the last 30 days")
    args = parser.parse_args()

    client = anthropic.Anthropic()  # needs ANTHROPIC_API_KEY
    days = 30 if args.backfill else 3
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    competitors = json.loads((ROOT / "scanner" / "competitors.json").read_text())
    store = load_json(DATA / "competitor-news.json", {"items": [], "seen": [], "last_scan": None})
    seen = set(store["seen"])

    candidates = []
    for comp in competitors:
        for item in collect(comp, cutoff):
            key = item["url"] or f"{item['company']}|{item['title']}"
            if key in seen or not item["title"]:
                continue
            seen.add(key)
            candidates.append(item)
        time.sleep(0.2)

    dropped = max(0, len(candidates) - MAX_ITEMS_PER_RUN)
    if dropped:
        print(f"  ! capping run at {MAX_ITEMS_PER_RUN} items ({dropped} deferred to future runs)")
        for item in candidates[MAX_ITEMS_PER_RUN:]:
            seen.discard(item["url"] or f"{item['company']}|{item['title']}")
        candidates = candidates[:MAX_ITEMS_PER_RUN]
    print(f"candidates: {len(candidates)}")

    new_items = []
    for item in candidates:
        try:
            verdict = analyse(client, item)
        except Exception as e:
            print(f"  ! analysis failed for {item['title'][:60]}: {e}")
            seen.discard(item["url"] or f"{item['company']}|{item['title']}")
            continue
        if not verdict["relevant"]:
            continue
        record = {
            "company": item["company"],
            "title": item["title"],
            "url": item["url"],
            "source": item["source"],
            "published": item["published"],
            "type": verdict["type"],
            "summary": verdict["summary"],
            "positioning": verdict["positioning"],
            "found_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        store["items"].append(record)
        new_items.append(record)
        print(f"  + {item['company']}: [{verdict['type']}] {item['title'][:70]}")

    store["items"].sort(key=lambda x: x["published"] or x["found_at"], reverse=True)
    store["seen"] = sorted(seen)
    store["last_scan"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_json(DATA / "competitor-news.json", store)

    if new_items:
        lines = [f"## 📡 {len(new_items)} competitor update(s)\n"]
        for it in new_items:
            lines.append(
                f"- **{it['company']}** · {TYPE_LABELS.get(it['type'], 'News')} — {it['title']}\n"
                f"  {it['summary']}\n"
                f"  {it['url']}"
            )
        (ROOT / "digest_competitors.md").write_text("\n".join(lines) + "\n")

    print(f"done: {len(new_items)} relevant items kept")


if __name__ == "__main__":
    main()
