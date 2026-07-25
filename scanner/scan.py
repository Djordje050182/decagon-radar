"""Decagon Radar — podcast mention scanner.

Finds mentions of Decagon (decagon.ai, the AI customer-support company) in
podcast episodes published to YouTube, by scanning captions of new uploads
from a watchlist of channels plus a generic YouTube search.

Outputs:
  data/mentions.json        accumulated confirmed mentions (committed)
  data/channel-cache.json   handle -> channel/uploads-playlist cache (committed)
  data/scanned-videos.json  videos already processed (committed)
  digest_mentions.md        new-mention digest fragment (not committed)

Usage:
  python scanner/scan.py            # daily incremental (last 3 days)
  python scanner/scan.py --backfill # first run (last 365 days of uploads)
"""

import argparse
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import anthropic
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)

try:
    from youtube_transcript_api.proxies import WebshareProxyConfig
except ImportError:
    WebshareProxyConfig = None

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
YT_API = "https://www.googleapis.com/youtube/v3"
YT_KEY = os.environ.get("YOUTUBE_API_KEY")
SEARCH_QUERIES = ['"Decagon AI"', "decagon.ai"]

# Brands tracked in podcast captions. BRANDS_VERSION bumps when this list grows;
# already-scanned videos are only re-checked when RESCAN_BRANDS=1 (archive re-scan).
BRANDS_VERSION = 2
BRANDS = [
    ("Decagon", r"decagon", "Decagon, the AI customer-support agents company (decagon.ai, founded by Jesse Zhang) — NOT the geometric shape or any other Decagon"),
    ("Sierra", r"\bsierra\b", "Sierra, the AI agents company (sierra.ai, founded by Bret Taylor and Clay Bavor) — NOT the mountain range, Sierra Nevada, Sierra Leone, High Sierra or other Sierras"),
    ("ElevenLabs", r"eleven\s?labs", "ElevenLabs, the voice AI company"),
    ("Cresta", r"\bcresta\b", "Cresta, the contact-center AI company"),
    ("Intercom (Fin)", r"\bintercom\b", "Intercom, the customer-service platform, or its Fin AI agent — NOT a door/building intercom"),
    ("Salesforce Agentforce", r"agent\s?force", "Salesforce Agentforce, the enterprise AI agent platform"),
    ("Ada", r"\bada\b", "Ada, the AI customer-service company (ada.cx) — NOT Ada Lovelace, the ADA act/compliance, Cardano's ADA token, or the programming language"),
    ("PolyAI", r"poly\s?ai", "PolyAI, the voice assistant company"),
    ("Parloa", r"parloa", "Parloa, the AI contact-center company"),
    ("Relevance AI", r"relevance\s?ai", "Relevance AI, the AI workforce/agents platform"),
    ("Forethought", r"\bforethought\b", "Forethought, the customer-support AI company (forethought.ai) — NOT the common English word"),
]
BRAND_PATTERNS = [(name, re.compile(pat, re.IGNORECASE), desc) for name, pat, desc in BRANDS]
CONTEXT_SECONDS = 60          # transcript context around a hit sent to Claude
MENTION_GAP_SECONDS = 120     # hits closer than this collapse into one mention
MAX_CAPTION_RETRIES = 3       # daily runs a video gets before we give up
MAX_RUNTIME_MIN = int(os.environ.get("MAX_RUNTIME_MIN", 300))  # stop before CI's 6h kill so data commits
FETCH_WORKERS = int(os.environ.get("FETCH_WORKERS", 6))        # parallel caption fetches (rotating proxy)
RUN_START = time.monotonic()

HAIKU = "claude-haiku-4-5"
ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "is_company": {
            "type": "boolean",
            "description": "True only if this refers to Decagon the AI customer-support company (decagon.ai)",
        },
        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
        "topic": {
            "type": "string",
            "description": "One sentence: what the conversation is about at this point",
        },
    },
    "required": ["is_company", "sentiment", "topic"],
    "additionalProperties": False,
}


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def yt_get(endpoint, **params):
    params["key"] = YT_KEY
    r = requests.get(f"{YT_API}/{endpoint}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def resolve_channel(handle, cache):
    if handle in cache:
        return cache[handle]
    resp = yt_get("channels", part="contentDetails,snippet", forHandle=handle)
    items = resp.get("items", [])
    if not items:
        print(f"  ! could not resolve channel handle {handle}")
        return None
    item = items[0]
    cache[handle] = {
        "channel_id": item["id"],
        "title": item["snippet"]["title"],
        "uploads_playlist": item["contentDetails"]["relatedPlaylists"]["uploads"],
    }
    return cache[handle]


def recent_uploads(playlist_id, cutoff, max_pages):
    """Videos in an uploads playlist published after cutoff (newest first)."""
    videos, page_token = [], None
    for _ in range(max_pages):
        params = {"part": "snippet,contentDetails", "playlistId": playlist_id, "maxResults": 50}
        if page_token:
            params["pageToken"] = page_token
        resp = yt_get("playlistItems", **params)
        stop = False
        for item in resp.get("items", []):
            published = item["contentDetails"].get("videoPublishedAt") or item["snippet"]["publishedAt"]
            if published < cutoff:
                stop = True
                continue
            videos.append({
                "video_id": item["contentDetails"]["videoId"],
                "title": item["snippet"]["title"],
                "channel": item["snippet"]["channelTitle"],
                "published": published,
            })
        page_token = resp.get("nextPageToken")
        if stop or not page_token:
            break
    return videos


def search_videos(query, cutoff):
    resp = yt_get(
        "search", part="snippet", q=query, type="video",
        publishedAfter=cutoff, maxResults=50, order="date",
        relevanceLanguage="en",
    )
    return [{
        "video_id": item["id"]["videoId"],
        "title": item["snippet"]["title"],
        "channel": item["snippet"]["channelTitle"],
        "published": item["snippet"]["publishedAt"],
    } for item in resp.get("items", [])]


_TLS = threading.local()
_PROXY_ANNOUNCED = threading.Event()


def ytt_api():
    """Per-thread caption client (sessions aren't thread-safe), proxied if configured."""
    api = getattr(_TLS, "api", None)
    if api is None:
        user, pw = os.environ.get("WEBSHARE_USER"), os.environ.get("WEBSHARE_PASS")
        if user and pw and WebshareProxyConfig:
            api = YouTubeTranscriptApi(
                proxy_config=WebshareProxyConfig(proxy_username=user, proxy_password=pw)
            )
            if not _PROXY_ANNOUNCED.is_set():
                _PROXY_ANNOUNCED.set()
                print("caption fetches routed via Webshare proxy", flush=True)
        else:
            api = YouTubeTranscriptApi()
        _TLS.api = api
    return api


def fetch_transcript(video_id):
    """Returns list of {text, start} snippets, or raises."""
    fetched = ytt_api().fetch(video_id, languages=["en", "en-US", "en-GB"])
    return [{"text": s.text, "start": s.start} for s in fetched]


def find_mentions(snippets, pattern):
    """Group keyword hits into mentions; return list of (timestamp, context_text)."""
    hits = [s["start"] for s in snippets if pattern.search(s["text"])]
    if not hits:
        return []
    groups, current = [], [hits[0]]
    for t in hits[1:]:
        if t - current[-1] <= MENTION_GAP_SECONDS:
            current.append(t)
        else:
            groups.append(current)
            current = [t]
    groups.append(current)

    mentions = []
    for group in groups:
        start, end = group[0] - CONTEXT_SECONDS, group[-1] + CONTEXT_SECONDS
        context = " ".join(s["text"] for s in snippets if start <= s["start"] <= end)
        mentions.append((int(group[0]), context))
    return mentions


def analyse(client, video, timestamp, context, brand, brand_desc):
    prompt = (
        f"A podcast transcript excerpt is below. Somewhere in it '{brand}' (or a close spoken "
        f"variant) appears. Decide whether it refers to {brand_desc}. "
        f"If it is the company, assess the sentiment of the discussion about {brand} "
        "(positive / neutral / negative) and summarise in one sentence what the conversation "
        "is about at this point.\n\n"
        f"Podcast: {video['channel']}\nEpisode: {video['title']}\n\n"
        f"Transcript excerpt:\n{context[:6000]}"
    )
    response = client.messages.create(
        model=HAIKU,
        max_tokens=512,
        output_config={"format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def push_progress(processed):
    """In CI: push data/ so the live site updates while the scan runs."""
    import subprocess
    try:
        subprocess.run(["git", "add", "data/"], cwd=ROOT, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", f"Scan progress: {processed} episodes"],
                       cwd=ROOT, check=True, capture_output=True)
        subprocess.run(["git", "push", "-q"], cwd=ROOT, check=True, capture_output=True, timeout=60)
        print(f"  ↑ pushed progress at {processed} episodes", flush=True)
    except Exception as e:
        print(f"  ! progress push failed (continuing): {e}", flush=True)


def fmt_ts(seconds):
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true", help="scan the last 365 days")
    args = parser.parse_args()

    if not YT_KEY:
        sys.exit("YOUTUBE_API_KEY is not set")
    client = anthropic.Anthropic(timeout=90.0)  # fail fast on wedged connections; SDK retries

    days = 365 if args.backfill else 3
    max_pages = 8 if args.backfill else 1
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    channels = json.loads((ROOT / "scanner" / "channels.json").read_text())
    cache = load_json(DATA / "channel-cache.json", {})
    scanned = load_json(DATA / "scanned-videos.json", {})
    store = load_json(DATA / "mentions.json", {"mentions": [], "last_scan": None})
    known = {(m["video_id"], m.get("brand", "Decagon"), m["timestamp"]) for m in store["mentions"]}
    rescan_brands = os.environ.get("RESCAN_BRANDS") == "1"

    # 1. Discover candidates
    candidates = {}
    for ch in channels:
        info = resolve_channel(ch["handle"], cache)
        if not info:
            continue
        for v in recent_uploads(info["uploads_playlist"], cutoff, max_pages):
            candidates[v["video_id"]] = v
        time.sleep(0.1)
    print(f"watchlist candidates: {len(candidates)}")

    for q in SEARCH_QUERIES:
        try:
            for v in search_videos(q, cutoff):
                candidates.setdefault(v["video_id"], v)
        except requests.HTTPError as e:
            print(f"  ! search failed for {q}: {e}")
    print(f"total candidates: {len(candidates)}")

    # 2. Scan captions
    def persist():
        save_json(DATA / "mentions.json", store)
        save_json(DATA / "channel-cache.json", cache)
        save_json(DATA / "scanned-videos.json", scanned)

    pending = []
    for vid, video in candidates.items():
        state = scanned.get(vid, {})
        if state.get("status") == "done":
            # re-scan only when the brand list grew AND an archive re-scan was requested
            if not (rescan_brands and state.get("bv", 1) < BRANDS_VERSION):
                continue
        elif state.get("status") == "no_captions":
            continue
        if state.get("retries", 0) >= MAX_CAPTION_RETRIES:
            continue
        pending.append((vid, video))
    print(f"to scan this run: {len(pending)}", flush=True)

    def fetch_one(item):
        vid, video = item
        try:
            return vid, video, fetch_transcript(vid), None
        except Exception as e:
            return vid, video, None, e

    new_mentions, caption_failures = [], 0
    consecutive_blocks, processed = 0, 0
    stop = False
    BATCH = FETCH_WORKERS * 4
    for batch_start in range(0, len(pending), BATCH):
        if stop:
            break
        batch = pending[batch_start:batch_start + BATCH]
        # fresh executor per batch: a wedged fetch can't poison later batches
        pool = ThreadPoolExecutor(max_workers=FETCH_WORKERS)
        futures = {pool.submit(fetch_one, item): item for item in batch}
        try:
            results = (f.result() for f in as_completed(futures, timeout=30 + 20 * len(batch)))
            for vid, video, snippets, err in results:
                if consecutive_blocks >= 15:
                    print("  !! aborting caption scan: YouTube is blocking us — remaining videos left for a later run", flush=True)
                    stop = True
                    break
                if time.monotonic() - RUN_START > MAX_RUNTIME_MIN * 60:
                    print(f"  !! runtime cap ({MAX_RUNTIME_MIN} min) reached — stopping gracefully, remaining videos next run", flush=True)
                    stop = True
                    break
                state = scanned.get(vid, {})
                if err is not None:
                    if isinstance(err, (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable)):
                        scanned[vid] = {"status": "no_captions"}
                    elif "ProxyError" in type(err).__name__:
                        # proxy dead (e.g. bandwidth cap) — not the video's fault, don't burn retries
                        consecutive_blocks += 5  # trip the abort fast
                        caption_failures += 1
                        print(f"  ! proxy failure on {vid}: {type(err).__name__} — check Webshare bandwidth", flush=True)
                    else:
                        if type(err).__name__ in ("IpBlocked", "RequestBlocked"):
                            consecutive_blocks += 1
                        scanned[vid] = {"status": "retry", "retries": state.get("retries", 0) + 1}
                        caption_failures += 1
                        print(f"  ! captions failed for {vid} ({video['title'][:60]}): {type(err).__name__}", flush=True)
                    continue
                consecutive_blocks = 0

                for brand, pattern, brand_desc in BRAND_PATTERNS:
                    for timestamp, context in find_mentions(snippets, pattern):
                        if (vid, brand, timestamp) in known:
                            continue
                        try:
                            verdict = analyse(client, video, timestamp, context, brand, brand_desc)
                        except Exception as e:
                            print(f"  ! analysis failed for {vid}@{timestamp} [{brand}]: {e}", flush=True)
                            continue
                        if not verdict["is_company"]:
                            continue
                        mention = {
                            "video_id": vid,
                            "brand": brand,
                            "podcast": video["channel"],
                            "episode": video["title"],
                            "published": video["published"],
                            "timestamp": timestamp,
                            "timestamp_label": fmt_ts(timestamp),
                            "url": f"https://www.youtube.com/watch?v={vid}&t={timestamp}s",
                            "sentiment": verdict["sentiment"],
                            "topic": verdict["topic"],
                            "found_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        }
                        store["mentions"].append(mention)
                        known.add((vid, brand, timestamp))
                        new_mentions.append(mention)
                        print(f"  + MENTION [{brand}] {video['channel']} @ {mention['timestamp_label']} ({verdict['sentiment']})", flush=True)
                scanned[vid] = {"status": "done", "bv": BRANDS_VERSION}
                processed += 1
                if processed % 25 == 0:
                    persist()
                    print(f"  … {processed} videos scanned this run", flush=True)
                    if os.environ.get("PUSH_PROGRESS") and processed % 100 == 0:
                        push_progress(processed)
        except FuturesTimeoutError:
            wedged = sum(1 for f in futures if not f.done())
            for f, (vid, video) in futures.items():
                if not f.done():
                    st = scanned.get(vid, {})
                    scanned[vid] = {"status": "retry", "retries": st.get("retries", 0) + 1}
                    caption_failures += 1
            print(f"  ! batch timed out with {wedged} wedged fetches — queued for retry", flush=True)
        finally:
            pool.shutdown(wait=False)

    # 3. Persist
    store["mentions"].sort(key=lambda m: m["published"], reverse=True)
    store["last_scan"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    persist()

    # 4. Digest fragment for the daily issue
    if new_mentions:
        lines = [f"## 🎙 {len(new_mentions)} new podcast mention(s)\n"]
        for m in new_mentions:
            lines.append(
                f"- **{m.get('brand', 'Decagon')}** · *{m['podcast']}* — {m['episode']}\n"
                f"  {m['sentiment'].capitalize()} · at {m['timestamp_label']} · {m['topic']}\n"
                f"  {m['url']}"
            )
        (ROOT / "digest_mentions.md").write_text("\n".join(lines) + "\n")

    print(f"done: {len(new_mentions)} new mentions, {caption_failures} caption fetches queued for retry")


if __name__ == "__main__":
    main()
