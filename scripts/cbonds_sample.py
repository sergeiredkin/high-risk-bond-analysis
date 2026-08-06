"""One-off sampling script for the Cbonds feasibility check (Task 4 follow-up).

NOT part of the hybond pipeline -- standalone, disk-only, no DB writes.
Purpose: log Cbonds' /news/ feed plus both existing Smart-Lab feeds once a
day for a real week, so the overlap/additive-rate report at the end of the
window is based on 5-7 days of data instead of the single-day spot check
already done. If the additive rate looks worth it, a real scraper+alias
integration (same shape as smartlab_news.py) would be a separate later task.

Self-terminating: exits immediately (no fetch, no log write) once today is
past END_DATE, so the crontab entry can be left in place after the sampling
window without silently running forever -- delete the crontab line at that
point rather than relying on this check alone.

Output: newline-delimited JSON, one line per (day, source) fetch, appended
to data/cbonds_sample/log.jsonl. Each line already carries which watchlist
aliases matched and whether that alias's issuer also appears in the *other*
source(s) fetched the same day, so the end-of-window report is just a read
+ aggregate over this file, no re-fetching needed.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hybond.scrape.smartlab_news import NEWS_ALIASES  # noqa: E402
from hybond.scrape.smartlab import INTERFAX_ISSUERS, _ITEM_RE, _clean_text  # noqa: E402

END_DATE = date(2026, 8, 13)  # ~7 days from 2026-08-06; delete the crontab entry after this

ALIASES = {**NEWS_ALIASES, **INTERFAX_ISSUERS}  # union, for matching purposes only

LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "cbonds_sample" / "log.jsonl"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="ignore")


def strip_tags(html: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def match_aliases(text: str) -> list[list[str]]:
    return [[alias, iid] for alias, iid in ALIASES.items() if alias in text]


def parse_cbonds(html: str) -> list[dict]:
    items = re.findall(
        r'<a href="(https://cbonds\.ru/news/\d+/)"\s+class="ttl[^"]*"\s+target="_blank">(.*?)</a>',
        html, re.S,
    )
    out = []
    for url, raw_title in items:
        title = strip_tags(raw_title).strip()
        out.append({"url": url, "title": title, "aliases": match_aliases(title)})
    return out


def parse_smartlab_bonds(html: str) -> list[dict]:
    # Reuses smartlab.py's real production regex (_ITEM_RE) rather than a
    # new guess -- same forum post structure, just title text extracted here
    # (matching is redone against the union alias set, see match_aliases).
    out = []
    for m in _ITEM_RE.finditer(html):
        title = _clean_text(m.group("title"))
        author = _clean_text(m.group("author"))
        if not title:
            continue
        out.append({"url": m.group("href"), "title": title, "author": author,
                    "aliases": match_aliases(title) or match_aliases(author)})
    return out


def parse_smartlab_rss(xml: str) -> list[dict]:
    titles = re.findall(r"<title>(.*?)</title>", xml, re.S)
    out = []
    for raw_title in titles[1:]:  # first <title> is the feed's own title
        title = strip_tags(raw_title).strip()
        out.append({"title": title, "aliases": match_aliases(title)})
    return out


def main() -> None:
    today = date.today()
    if today > END_DATE:
        print(f"{today} is past END_DATE ({END_DATE}) -- sampling window closed, doing nothing.")
        return

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    fetches = [
        ("cbonds_news", "https://cbonds.ru/news/", parse_cbonds),
        ("smartlab_bonds_forum", "https://smart-lab.ru/bonds/extra/", parse_smartlab_bonds),
        ("smartlab_news_rss", "https://smart-lab.ru/news/rss/", parse_smartlab_rss),
    ]

    with LOG_PATH.open("a", encoding="utf-8") as f:
        for source, url, parser in fetches:
            record = {
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "date": today.isoformat(),
                "source": source,
            }
            try:
                raw = fetch(url)
                record["items"] = parser(raw)
                record["ok"] = True
            except Exception as e:  # noqa: BLE001 -- log-and-continue script, not pipeline code
                record["ok"] = False
                record["error"] = str(e)
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"{source}: {'ok' if record['ok'] else 'FAILED: ' + record.get('error', '')} "
                  f"({len(record.get('items', []))} items)")


if __name__ == "__main__":
    main()
