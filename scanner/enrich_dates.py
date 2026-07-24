"""Backfill publication dates for undated items (best effort).

For each item with no published date:
  1. Re-fetch the page and look for a visible printed date.
  2. Fall back to the Wayback Machine's first snapshot of the URL.
Estimated dates are flagged date_estimated=true (shown as ≈ on the dashboard).
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


def main():
    store = json.loads((DATA / "competitor-news.json").read_text())
    undated = [i for i in store["items"] if not i.get("published")]
    print(f"undated items: {len(undated)}")
    fixed_text, fixed_wb = 0, 0
    for i, item in enumerate(undated):
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
        if (i + 1) % 15 == 0:
            store["items"].sort(key=lambda x: x["published"] or x["found_at"], reverse=True)
            (DATA / "competitor-news.json").write_text(
                json.dumps(store, indent=2, ensure_ascii=False) + "\n")
            print(f"  … {i + 1}/{len(undated)} processed")
    store["items"].sort(key=lambda x: x["published"] or x["found_at"], reverse=True)
    (DATA / "competitor-news.json").write_text(json.dumps(store, indent=2, ensure_ascii=False) + "\n")
    print(f"done: {fixed_text} from page text, {fixed_wb} from Wayback, "
          f"{len(undated) - fixed_text - fixed_wb} still undated")


if __name__ == "__main__":
    main()
