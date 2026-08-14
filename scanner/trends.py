"""Decagon Radar — market trends & analysis synthesis.

Reads the accumulated competitor items and podcast mentions, and asks Claude
for a market-level read: what's going on, the themes and their momentum,
convergence signals, and predictions. Runs daily after the collectors.

Output: data/trends.json (committed; rendered by the Trends tab).
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MODEL = "claude-sonnet-5"  # one call/day; synthesis quality matters more than pennies
WINDOW_DAYS = 90

SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {
            "type": "string",
            "description": "2-3 short paragraphs: what is going on in the AI customer-support/agent market right now, based on the evidence. Written for a sales team at Decagon.",
        },
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string", "description": "2-3 sentences"},
                    "momentum": {"type": "string", "enum": ["rising", "steady", "cooling"]},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "Short headline or fact"},
                                "url": {"type": "string", "description": "Source URL — from the signals list or a web search result. Empty string if none."},
                            },
                            "required": ["label", "url"],
                            "additionalProperties": False,
                        },
                        "description": "2-4 supporting facts, each with its source URL",
                    },
                },
                "required": ["name", "description", "momentum", "evidence"],
                "additionalProperties": False,
            },
        },
        "convergence": {
            "type": "string",
            "description": "One paragraph: where players are converging or colliding (frontier labs moving down, incumbents bolting on AI, startups specialising, pricing models, voice vs chat, etc.)",
        },
        "predictions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "horizon": {"type": "string", "description": "e.g. 'next quarter', 'by end of 2026'"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "rationale": {"type": "string", "description": "One sentence grounded in the evidence"},
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "URLs supporting this prediction (from signals or web search)",
                    },
                },
                "required": ["claim", "horizon", "confidence", "rationale", "sources"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overview", "themes", "convergence", "predictions"],
    "additionalProperties": False,
}


def main():
    client = anthropic.Anthropic(timeout=180.0)  # synthesis is one long call; still bound it
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)

    news = json.loads((DATA / "competitor-news.json").read_text())
    mentions = json.loads((DATA / "mentions.json").read_text())

    lines = []
    for i in news["items"]:
        d = (i.get("published") or i.get("found_at") or "")[:10]
        if d and d >= cutoff.strftime("%Y-%m-%d"):
            lines.append(f"{d} | {i['company']} | {i['type']} | {i.get('market','')} | {i['title']} :: {i['summary']} [{i['url']}]")

    # podcasts: every Decagon mention in the window individually; other brands as aggregates
    from collections import Counter
    agg = Counter()
    for m in mentions["mentions"]:
        brand = m.get("brand", "Decagon")
        if brand == "Decagon" and m["published"][:10] >= cutoff.strftime("%Y-%m-%d"):
            lines.append(f"{m['published'][:10]} | PODCAST {m['podcast']} | Decagon | sentiment={m['sentiment']} | {m['topic']} [{m['url']}]")
        else:
            agg[(brand, m["sentiment"])] += 1
    for (brand, sentiment), n in agg.most_common():
        lines.append(f"PODCAST AGGREGATE (12 months) | {brand} | {n} mentions | sentiment={sentiment}")

    if len(lines) < 5:
        print("not enough data for trends synthesis yet")
        return

    prompt = (
        "You are a sharp market analyst covering AI customer-support and enterprise agent "
        "platforms, writing for the team at Decagon (decagon.ai). Below is every tracked "
        f"signal from the last {WINDOW_DAYS} days: competitor/industry news items and podcast "
        "mentions of Decagon.\n\n"
        "Produce a market read: an overview of what's going on, 4-6 named themes with momentum "
        "(rising/steady/cooling) and supporting evidence, a paragraph on convergence (who is "
        "moving into whose territory), and 3-5 concrete predictions with horizon and confidence. "
        "Be specific and evidence-grounded — no filler, no hedging boilerplate. It is fine to be "
        "opinionated; these are analyst views, not official positions.\n\n"
        "Use web search (a few queries at most) to verify or broaden the picture where the "
        "signals are thin — e.g. recent funding numbers, analyst commentary, or a development "
        "the signals hint at but don't cover. Every evidence item and prediction must cite "
        "source URLs — taken from the signal list (each signal ends with its URL in brackets) "
        "or from your search results. Never cite a URL you haven't seen.\n\n"
        "ACCURACY RULES (hard requirements):\n"
        "- Get M&A direction right. Verify who acquired whom with a web search before asserting "
        "any acquisition. Known correction: Intercom renamed itself 'Fin' in 2026 and was "
        "acquired by SALESFORCE for ~$3.6B — Intercom did not acquire anything.\n"
        "- Never merge separate stories into one claim; if a signal summary conflicts with what "
        "your search finds, trust the search and say so.\n"
        "- The PODCAST signals are share-of-voice data from a 12-month scan of 24 tech/AI shows "
        "— use them (volumes, sentiment mix, what hosts/guests actually discussed) as first-class "
        "evidence about mindshare and narrative, and weave at least one podcast-grounded insight "
        "into the overview and themes.\n\n"
        "SIGNALS:\n" + "\n".join(lines[:400])
    )

    def synthesise(use_search):
        messages = [{"role": "user", "content": prompt}]
        kwargs = {}
        if use_search:
            kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}]
        while True:
            response = client.messages.create(
                model=MODEL,
                max_tokens=16000,  # adaptive thinking shares this budget; 4k truncated mid-JSON
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                messages=messages,
                **kwargs,
            )
            if response.stop_reason == "pause_turn":
                messages = messages[:1] + [{"role": "assistant", "content": response.content}]
                continue
            break
        text = "".join(b.text for b in response.content if b.type == "text")
        return json.loads(text)

    # occasionally the search+schema combination yields an empty shell — retry, then drop search
    result = None
    for attempt, use_search in enumerate([True, True, False]):
        result = synthesise(use_search)
        if result["themes"] and result["overview"].strip():
            break
        print(f"  ! empty synthesis on attempt {attempt + 1} (search={use_search}), retrying")
    result["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result["window_days"] = WINDOW_DAYS
    result["signal_count"] = len(lines)

    (DATA / "trends.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(f"trends synthesised from {len(lines)} signals: {len(result['themes'])} themes, {len(result['predictions'])} predictions")


if __name__ == "__main__":
    main()
