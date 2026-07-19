# Decagon Radar

Automated daily tracking of [Decagon](https://decagon.ai) mentions across podcasts, plus a competitor/industry watch — published as a live dashboard.

**Dashboard:** https://djordje050182.github.io/decagon-radar/

## How it works

A GitHub Actions job runs daily (20:00 UTC):

1. **Podcast mentions** (`scanner/scan.py`) — scans new uploads from ~20 tech/AI podcast YouTube channels (`scanner/channels.json`) plus a generic YouTube search, pulls captions, finds every spoken "Decagon", and uses Claude to filter false positives (the shape, other Decagons) and score sentiment + topic. Each mention deep-links to the exact second on YouTube.
2. **Competitor watch** (`scanner/competitors.py`) — sweeps competitor blogs (RSS) and Google News for launches, customer wins, funding and frontier-lab overlap (`scanner/competitors.json`), with Claude filtering relevance and drafting a positioning angle.
3. Results are committed to `data/`, the dashboard updates via GitHub Pages, and a **digest issue** is opened when there are new items (watch this repo to get it by email — formatted for pasting into WhatsApp).

## Setup

Two repository secrets (Settings → Secrets and variables → Actions):

- `YOUTUBE_API_KEY` — Google Cloud, YouTube Data API v3 (free tier is ample)
- `ANTHROPIC_API_KEY` — console.anthropic.com (Haiku usage costs pennies/month)

First run: Actions → Daily Radar Scan → Run workflow → tick **backfill** (scans 12 months of podcast uploads / 30 days of news).

## Extending

- Add a podcast: append `{ "name": "...", "handle": "@..." }` to `scanner/channels.json`
- Add a competitor: append an entry with a `news_query` (and RSS `feeds` if the blog has one) to `scanner/competitors.json`

Known limitation: coverage is podcasts published to YouTube with captions — the large majority of relevant shows, but not audio-only feeds. YouTube occasionally rate-limits caption fetches from CI runners; failed episodes retry automatically on later runs.
