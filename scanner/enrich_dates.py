"""Backfill publication dates for undated items (best effort).

For each item with no published date:
  1. Re-fetch the page and look for a visible printed date.
  2. Fall back to the Wayback Machine's first snapshot of the URL.
Estimated dates are flagged date_estimated=true (shown as ≈ on the dashboard).

This matters more than it looks: the dashboard hides undated items from every
time-window view, so a company-site page with no date is fetched, stored, and
then never seen. Runs daily in CI with --limit; give it no limit to grind
through the whole backlog by hand.

Pages that resist MAX_ATTEMPTS lookups are parked rather than retried forever —
some company blogs simply never print a date.
"""

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from competitors import _page_meta, UA  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def wayback_first_capture(url):
    try:
        r = requests.get(
            "https://web.archive.org/cdx/search/cdx",
            params={"url": url, "output": "json", "fl": "timestamp",
                    "filter": "statuscode:200", "limit": 1},
            headers=UA, timeout=25,
        )
        rows = r.json()
        if len(rows) > 1:
            ts = rows[1][0]
            return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}T00:00:00"
    except Exception:
        pass
    return None


MAX_ATTEMPTS = 3


def save(store):
    store["items"].sort(key=lambda x: x.get("published") or "0000", reverse=True)
    (DATA / "competitor-news.json").write_text(
        json.dumps(store, indent=2, ensure_ascii=False) + "\n")


def main():
    limit = None
    argv = sys.argv[1:]
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])

    store = json.loads((DATA / "competitor-news.json").read_text())
    undated = [i for i in store["items"] if not i.get("published")]
    pending = [i for i in undated if i.get("date_lookup_attempts", 0) < MAX_ATTEMPTS]

    # Newest finds first, then fewest attempts first (stable sort, so the second
    # key wins) — today's items never queue behind a backlog that has already
    # resisted a lookup or two.
    pending.sort(key=lambda x: x.get("found_at") or "", reverse=True)
    pending.sort(key=lambda x: x.get("date_lookup_attempts", 0))
    if limit:
        pending = pending[:limit]

    print(f"undated: {len(undated)} | processing {len(pending)} this run")
    fixed_text, fixed_wb = 0, 0
    for n, item in enumerate(pending, 1):
        date = None
        try:
            _, _, date = _page_meta(item["url"])
        except Exception:
            pass
        if date:
            fixed_text += 1
        else:
            date = wayback_first_capture(item["url"])
            if date:
                fixed_wb += 1
            time.sleep(0.8)  # be polite to archive.org
        if date:
            item["published"] = date
            item["date_estimated"] = True
            item.pop("date_lookup_attempts", None)
        else:
            item["date_lookup_attempts"] = item.get("date_lookup_attempts", 0) + 1
        if n % 15 == 0:
            save(store)
            print(f"  … {n}/{len(pending)} processed")
    save(store)
    still = sum(1 for i in store["items"] if not i.get("published"))
    parked = sum(1 for i in store["items"] if not i.get("published")
                 and i.get("date_lookup_attempts", 0) >= MAX_ATTEMPTS)
    print(f"done: {fixed_text} from page text, {fixed_wb} from Wayback | "
          f"{still} still undated ({parked} parked after {MAX_ATTEMPTS} tries)")


if __name__ == "__main__":
    main()
