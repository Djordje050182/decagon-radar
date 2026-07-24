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
import time
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
KEYWORD = re.compile(r"decagon", re.IGNORECASE)
SEARCH_QUERIES = ['"Decagon AI"', "decagon.ai"]
CONTEXT_SECONDS = 60          # transcript context around a hit sent to Claude
MENTION_GAP_SECONDS = 120     # hits closer than this collapse into one mention
MAX_CAPTION_RETRIES = 3       # daily runs a video gets before we give up

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


_YTT_API = None


def ytt_api():
    """Caption client, with Webshare rotating-proxy support if configured."""
    global _YTT_API
    if _YTT_API is None:
        user, pw = os.environ.get("WEBSHARE_USER"), os.environ.get("WEBSHARE_PASS")
        if user and pw and WebshareProxyConfig:
            _YTT_API = YouTubeTranscriptApi(
                proxy_config=WebshareProxyConfig(proxy_username=user, proxy_password=pw)
            )
            print("caption fetches routed via Webshare proxy")
        else:
            _YTT_API = YouTubeTranscriptApi()
    return _YTT_API


def fetch_transcript(video_id):
    """Returns list of {text, start} snippets, or raises."""
    fetched = ytt_api().fetch(video_id, languages=["en", "en-US", "en-GB"])
    return [{"text": s.text, "start": s.start} for s in fetched]


def find_mentions(snippets):
    """Group keyword hits into mentions; return list of (timestamp, context_text)."""
    hits = [s["start"] for s in snippets if KEYWORD.search(s["text"])]
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


def analyse(client, video, timestamp, context):
    prompt = (
        "A podcast transcript excerpt is below. Somewhere in it the word 'decagon' is spoken. "
        "Decide whether it refers to Decagon, the AI customer-support agents company (decagon.ai, "
        "founded by Jesse Zhang and Ashwin Sreenivas), as opposed to the geometric shape, "
        "Decagon Devices, a fantasy/game reference, or any other Decagon. "
        "If it is the company, assess the sentiment of the discussion about Decagon "
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
    known = {(m["video_id"], m["timestamp"]) for m in store["mentions"]}

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

    new_mentions, caption_failures = [], 0
    consecutive_blocks, processed = 0, 0
    for vid, video in candidates.items():
        state = scanned.get(vid, {})
        if state.get("status") == "done" or state.get("status") == "no_captions":
            continue
        if state.get("retries", 0) >= MAX_CAPTION_RETRIES:
            continue
        if consecutive_blocks >= 15:
            print("  !! aborting caption scan: YouTube is blocking this IP — remaining videos left for a later run")
            break
        try:
            snippets = fetch_transcript(vid)
            consecutive_blocks = 0
        except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
            scanned[vid] = {"status": "no_captions"}
            continue
        except Exception as e:  # rate limit / IP block — retry next run
            if type(e).__name__ in ("IpBlocked", "RequestBlocked"):
                consecutive_blocks += 1
            scanned[vid] = {"status": "retry", "retries": state.get("retries", 0) + 1}
            caption_failures += 1
            print(f"  ! captions failed for {vid} ({video['title'][:60]}): {type(e).__name__}")
            time.sleep(2)
            continue

        for timestamp, context in find_mentions(snippets):
            if (vid, timestamp) in known:
                continue
            try:
                verdict = analyse(client, video, timestamp, context)
            except Exception as e:
                print(f"  ! analysis failed for {vid}@{timestamp}: {e}")
                continue
            if not verdict["is_company"]:
                continue
            mention = {
                "video_id": vid,
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
            known.add((vid, timestamp))
            new_mentions.append(mention)
            print(f"  + MENTION {video['channel']} @ {mention['timestamp_label']} ({verdict['sentiment']})")
        scanned[vid] = {"status": "done"}
        processed += 1
        if processed % 25 == 0:
            persist()
            print(f"  … {processed} videos scanned this run")
        time.sleep(1.5 + random.random() * 1.5)

    # 3. Persist
    store["mentions"].sort(key=lambda m: m["published"], reverse=True)
    store["last_scan"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    persist()

    # 4. Digest fragment for the daily issue
    if new_mentions:
        lines = [f"## 🎙 {len(new_mentions)} new podcast mention(s)\n"]
        for m in new_mentions:
            lines.append(
                f"- *{m['podcast']}* — {m['episode']}\n"
                f"  {m['sentiment'].capitalize()} · at {m['timestamp_label']} · {m['topic']}\n"
                f"  {m['url']}"
            )
        (ROOT / "digest_mentions.md").write_text("\n".join(lines) + "\n")

    print(f"done: {len(new_mentions)} new mentions, {caption_failures} caption fetches queued for retry")


if __name__ == "__main__":
    main()
