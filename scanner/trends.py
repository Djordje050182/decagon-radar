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
                        "items": {"type": "string"},
                        "description": "2-4 headline titles from the input that support this theme",
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
                },
                "required": ["claim", "horizon", "confidence", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overview", "themes", "convergence", "predictions"],
    "additionalProperties": False,
}


def main():
    client = anthropic.Anthropic()
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)

    news = json.loads((DATA / "competitor-news.json").read_text())
    mentions = json.loads((DATA / "mentions.json").read_text())

    lines = []
    for i in news["items"]:
        d = (i.get("published") or i.get("found_at") or "")[:10]
        if d and d >= cutoff.strftime("%Y-%m-%d"):
            lines.append(f"{d} | {i['company']} | {i['type']} | {i.get('market','')} | {i['title']} :: {i['summary']}")
    for m in mentions["mentions"]:
        lines.append(f"{m['published'][:10]} | PODCAST {m['podcast']} | sentiment={m['sentiment']} | {m['topic']}")

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
        "SIGNALS:\n" + "\n".join(lines[:400])
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,  # adaptive thinking shares this budget; 4k truncated mid-JSON
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    result = json.loads(text)
    result["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result["window_days"] = WINDOW_DAYS
    result["signal_count"] = len(lines)

    (DATA / "trends.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(f"trends synthesised from {len(lines)} signals: {len(result['themes'])} themes, {len(result['predictions'])} predictions")


if __name__ == "__main__":
    main()
