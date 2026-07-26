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
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import anthropic

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HAIKU = "claude-haiku-4-5"
MAX_ITEMS_PER_RUN = int(os.environ.get("MAX_ITEMS_PER_RUN", 60))

SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {
            "type": "boolean",
            "description": "True only if this matters to Decagon's market: AI customer-support/voice agents, enterprise agent platforms, or a direct competitor move",
        },
        "type": {
            "type": "string",
            "enum": ["product_launch", "customer_win", "funding", "partnership", "frontier_overlap", "community", "other"],
        },
        "summary": {"type": "string", "description": "Two sentences max, factual"},
        "positioning": {
            "type": "string",
            "description": "One or two sentences: how Decagon could position against this. Empty string if not applicable (e.g. for Decagon's own coverage).",
        },
        "market": {
            "type": "string",
            "description": "Country or market the story concerns, if identifiable: e.g. 'US', 'UK', 'Australia', 'EMEA', 'APAC', 'Global'. Empty string if unclear.",
        },
    },
    "required": ["relevant", "type", "summary", "positioning", "market"],
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
    """Yield candidate items from first-party feeds + Google News (all queries)."""
    urls = list(competitor["feeds"])
    for query in competitor.get("news_queries") or [competitor["news_query"]]:
        q = urllib.parse.quote(query)
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


UA = {"User-Agent": "Mozilla/5.0 (decagon-radar research bot)"}
MAX_SITEMAP_FETCHES = int(os.environ.get("MAX_SITEMAP_FETCHES", 40))  # page fetches per competitor per run


def _sitemap_locs(url, depth=0):
    """All <loc> page URLs from a sitemap, recursing one level into indexes."""
    try:
        xml = requests.get(url, headers=UA, timeout=25).text
    except Exception as e:
        print(f"  ! sitemap failed {url}: {e}")
        return []
    child_maps = re.findall(r"<sitemap>\s*<loc>(.*?)</loc>", xml, re.S)
    if child_maps and depth == 0:
        locs = []
        for child in child_maps:
            if re.search(r"blog|news|press|page", child, re.I):
                locs += _sitemap_locs(child.strip(), depth=1)
        return locs
    return [m.strip() for m in re.findall(r"<loc>(.*?)</loc>", xml)]


def _page_meta(url):
    """Fetch a page and extract og:title / description / published date."""
    html = requests.get(url, headers=UA, timeout=25).text[:200000]

    def meta(*names):
        for n in names:
            m = re.search(
                r'<meta[^>]+(?:property|name)=["\']' + re.escape(n) + r'["\'][^>]+content=["\']([^"\']+)',
                html, re.I,
            ) or re.search(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']' + re.escape(n) + r'["\']',
                html, re.I,
            )
            if m:
                return m.group(1)
        return ""

    title = meta("og:title") or (re.search(r"<title>([^<]+)</title>", html, re.I) or [None, ""])[1]
    desc = meta("og:description", "description")
    published = meta("article:published_time", "og:article:published_time", "datePublished")
    if not published:
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html)
        published = m.group(1) if m else ""
    if not published:
        published = visible_text_date(html) or ""
    return clean(title), clean(desc), published[:19] or None


MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"


def visible_text_date(html):
    """Best-effort: find a publication date printed in the page body."""
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)[:60000]
    m = re.search(rf"({MONTHS})\s+(\d{{1,2}}),?\s+(20\d\d)", body) or \
        re.search(rf"(\d{{1,2}})\s+({MONTHS})\s+(20\d\d)", body)
    if not m:
        return None
    try:
        from datetime import datetime as _dt
        text = " ".join(m.groups())
        for fmt in ("%B %d %Y", "%d %B %Y"):
            try:
                return _dt.strptime(text, fmt).strftime("%Y-%m-%dT00:00:00")
            except ValueError:
                continue
    except Exception:
        pass
    return None


def collect_sitemap(competitor, seen):
    """Yield items for pages newly appeared in the company's own sitemap."""
    cfg = competitor.get("sitemap")
    if not cfg:
        return
    include = re.compile(cfg["include"], re.I)
    fetched = 0
    for loc in _sitemap_locs(cfg["url"]):
        path = re.sub(r"^https?://[^/]+", "", loc)
        if not include.search(path) or loc in seen:
            continue
        if fetched >= MAX_SITEMAP_FETCHES:
            break
        fetched += 1
        try:
            title, desc, published = _page_meta(loc)
        except Exception as e:
            print(f"  ! page fetch failed {loc}: {e}")
            continue
        if not title:
            continue
        time.sleep(0.3)
        yield {
            "company": competitor["name"],
            "title": title,
            "url": loc,
            "source": "Company site",
            "published": published,
            "snippet": desc[:1500],
        }


def social_query(competitor):
    """Query for Reddit/HN: explicit social_query, else the quoted primary name."""
    if competitor.get("social_query"):
        return competitor["social_query"]
    primary = re.sub(r"\s*\(.*\)$", "", competitor["name"]).strip()
    return f'"{primary}"'


def collect_reddit(competitor, cutoff):
    q = social_query(competitor)
    try:
        r = requests.get(
            "https://www.reddit.com/search.json",
            params={"q": q, "sort": "new", "limit": 25, "t": "month"},
            headers={"User-Agent": "decagon-radar/1.0 (research bot)"},
            timeout=20,
        )
        r.raise_for_status()
        posts = r.json().get("data", {}).get("children", [])
    except Exception as e:
        print(f"  ! reddit failed for {q}: {e}")
        return
    for p in posts:
        d = p.get("data", {})
        created = datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc)
        if created < cutoff:
            continue
        yield {
            "company": competitor["name"],
            "title": clean(d.get("title", "")),
            "url": "https://www.reddit.com" + d.get("permalink", ""),
            "source": f"Reddit r/{d.get('subreddit', '')}",
            "published": created.isoformat(timespec="seconds"),
            "snippet": clean(d.get("selftext", ""))[:1500],
        }


def collect_hackernews(competitor, cutoff):
    q = social_query(competitor).strip('"')
    try:
        r = requests.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={
                "query": q, "tags": "story",
                "numericFilters": f"created_at_i>{int(cutoff.timestamp())}",
                "hitsPerPage": 20,
            },
            timeout=20,
        )
        r.raise_for_status()
        hits = r.json().get("hits", [])
    except Exception as e:
        print(f"  ! hackernews failed for {q}: {e}")
        return
    for h in hits:
        yield {
            "company": competitor["name"],
            "title": clean(h.get("title", "")),
            "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
            "source": f"Hacker News ({h.get('points', 0)} pts)",
            "published": h.get("created_at", "")[:19],
            "snippet": clean(h.get("story_text", "") or "")[:1500],
        }


def analyse(client, item):
    prompt = (
        "You are a competitive-intelligence analyst for Decagon (decagon.ai), which builds "
        "AI agents for enterprise customer support (chat and voice). Assess the news item "
        "below about a competitor or frontier AI lab.\n\n"
        "Only mark it relevant if it genuinely matters to Decagon's market: AI customer-support "
        "or voice-agent products, enterprise agent platforms, competitor customer wins, funding, "
        "or frontier-lab releases that overlap Decagon's space. Consumer features, music/media "
        "tools, and unrelated corporate news are NOT relevant. Exceptions: (a) any genuine news "
        "coverage of Decagon itself IS relevant; (b) for frontier labs and open-source model "
        "providers, major new model releases or agent-capability launches ARE relevant "
        "(type: frontier_overlap) even without an explicit customer-support angle, because they "
        "shift the cost/capability stack Decagon builds on; (c) substantive community "
        "discussion (Reddit / Hacker News) IS relevant when it contains real user or buyer "
        "feedback, comparisons, or complaints about these products (type: community) — but "
        "memes, job posts, and low-effort threads are NOT. For items whose source is "
        "'Company site': keep product launches, case studies / customer stories, and major "
        "announcements; SEO explainer articles, glossary-style content and generic how-to "
        "posts are NOT relevant. A customer case study on a company's own site is a "
        "customer_win.\n\n"
        "If relevant, classify it, summarise it factually, note the country/market it concerns "
        "if identifiable, and suggest a positioning angle — how Decagon might credibly position "
        "against it (AI-suggested angle, not official messaging; grounded, no spin). For "
        "Decagon's own coverage, leave positioning empty.\n\n"
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
    "community": "Community",
    "other": "News",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true", help="scan the last 30 days")
    args = parser.parse_args()

    client = anthropic.Anthropic(timeout=90.0)  # fail fast on wedged connections; SDK retries
    days = 30 if args.backfill else 3
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    competitors = json.loads((ROOT / "scanner" / "competitors.json").read_text())
    store = load_json(DATA / "competitor-news.json", {"items": [], "seen": [], "last_scan": None})
    seen = set(store["seen"])

    candidates = []
    for comp in competitors:
        sources = [collect(comp, cutoff), collect_reddit(comp, cutoff),
                   collect_hackernews(comp, cutoff), collect_sitemap(comp, seen)]
        for source in sources:
            for item in source:
                key = item["url"] or f"{item['company']}|{item['title']}"
                if key in seen or not item["title"]:
                    continue
                seen.add(key)
                candidates.append(item)
        time.sleep(0.5)

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
            "market": verdict.get("market", ""),
            "found_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        store["items"].append(record)
        new_items.append(record)
        print(f"  + {item['company']}: [{verdict['type']}] {item['title'][:70]}")

    store["items"].sort(key=lambda x: x.get("published") or "0000", reverse=True)
    store["seen"] = sorted(seen)
    store["last_scan"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_json(DATA / "competitor-news.json", store)

    if new_items:
        lines = [f"## 📡 {len(new_items)} competitor update(s)\n"]
        for it in new_items[:15]:
            lines.append(
                f"- **{it['company']}** · {TYPE_LABELS.get(it['type'], 'News')} — {it['title']}\n"
                f"  {it['summary']}\n"
                f"  {it['url']}"
            )
        if len(new_items) > 15:
            lines.append(f"\n…and {len(new_items) - 15} more — full list on the dashboard.")
        (ROOT / "digest_competitors.md").write_text("\n".join(lines) + "\n")

    print(f"done: {len(new_items)} relevant items kept")


if __name__ == "__main__":
    main()
