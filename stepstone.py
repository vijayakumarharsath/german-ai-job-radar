#!/usr/bin/env python3
"""
StepStone scraper — LIVE-VERIFIED working with plain requests (no browser needed).

Tested against real StepStone markup (2026-09):
  - search:  https://www.stepstone.de/jobs/{what}[-{city}]?page=N   -> 25 cards/page
  - cards:   article[data-at='job-item']
             title  a[data-at='job-item-title']        (href is absolute, has job id)
             company [data-at='job-item-company-name']
             location [data-at='job-item-location']
             age     [data-at='job-item-timeago']
             teaser  [data-at='jobcard-content']
  - detail:  JSON-LD <script type="application/ld+json"> "@type": "JobPosting"
             -> .description = full HTML JD (~10 KB text)

Be polite: personal use, delays built in, don't run hourly.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import time

import requests

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}
DELAY = 1.5  # seconds between requests


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html_mod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _slug(text: str) -> str:
    text = (text.lower()
            .replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss"))
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def get(url: str, timeout: int = 30) -> requests.Response | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code != 200:
            print(f"    ! stepstone {r.status_code} on {url[:90]}")
            return None
        return r
    except Exception as e:
        print(f"    ! stepstone error: {type(e).__name__} {str(e)[:80]}")
        return None


def parse_search(page_html: str) -> list[dict]:
    """Parse a StepStone search-results page. Works on live pages AND saved HTML."""
    from urllib.parse import urljoin
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page_html, "lxml")
    out = []
    for card in soup.select("article[data-at='job-item']"):
        t = card.select_one("a[data-at='job-item-title']")
        if not t or not t.get("href"):
            continue
        comp = card.select_one("[data-at='job-item-company-name']")
        loc = card.select_one("[data-at='job-item-location']")
        ago = card.select_one("[data-at='job-item-timeago']")
        teaser = card.select_one("[data-at='jobcard-content']")
        href = t["href"].split("?")[0]
        out.append({
            "source": "stepstone",
            "title": t.get_text(strip=True),
            "company": comp.get_text(strip=True) if comp else "",
            "location": loc.get_text(strip=True) if loc else "",
            "url": urljoin("https://www.stepstone.de", href),
            "description": _clean(teaser.get_text(" ", strip=True)) if teaser else "",
            "date_posted": ago.get_text(strip=True) if ago else "",
        })
    return out


def fetch_detail_jd(url: str) -> str:
    """Fetch one StepStone job page, return full JD text via JSON-LD JobPosting."""
    r = get(url)
    time.sleep(DELAY)
    if r is None:
        return ""
    for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>',
                        r.text, re.S):
        try:
            d = json.loads(m)
        except json.JSONDecodeError:
            continue
        if d.get("@type") == "JobPosting" and d.get("description"):
            return _clean(d["description"])
    return ""


def search(query: str, city: str = "", page: int = 1) -> list[dict]:
    slug = _slug(query + ("-" + city if city else ""))
    url = f"https://www.stepstone.de/jobs/{slug}"
    if page > 1:
        url += f"?page={page}"
    r = get(url)
    time.sleep(DELAY)
    return parse_search(r.text) if r else []


ENRICH_TITLE_RE = re.compile(
    r"werkstudent|working\s*student|studentische|praktikant|praktikum|\bintern\b|thesis|abschlussarbeit"
    r"|\bjunior\b|graduate|absolvent|einstieg|berufseinsteiger|\btrainee\b", re.I)
SENIOR_RE = re.compile(r"\bsenior\b|\bsr\.?\b|\blead\b|principal|head\s+of|manager\b|director", re.I)


def harvest(queries: list[str], cities: list[str], pages: int = 1,
            detail_limit: int = 20) -> list[dict]:
    """Search StepStone and detail-enrich the most relevant teaser-only hits."""
    relevant_re = re.compile(
        r"machine\s*learning|maschinelles|\b(ai|ki|ml)\b|deep\s*learning|data\s*science|datenanaly"
        r"|\brobot|robotik|computer\s*vision|bildverarbeitung|\bllm\b|\bnlp\b|informatik|software|perception|autonom|vision|python|daten|analytics", re.I)

    rows, seen = [], set()
    for q in queries:
        for city in cities:
            for p in range(1, pages + 1):
                found = search(q, city, p)
                print(f"  + stepstone: {q}" + (f" @ {city}" if city else "") +
                      (f" p{p}" if pages > 1 else "") + f" -> {len(found)}")
                for rec in found:
                    key = rec["url"]
                    if key not in seen:
                        seen.add(key)
                        rows.append(rec)
    # enrich: student+relevant jobs with only a teaser get the full JD
    candidates = [r for r in rows
                  if ENRICH_TITLE_RE.search(r["title"]) and not SENIOR_RE.search(r["title"])
                  and len(r["description"]) < 800
                  and relevant_re.search(r["title"] + " " + r["description"])]
    print(f"  + stepstone: fetching {min(len(candidates), detail_limit)} full JDs…")
    for rec in candidates[:detail_limit]:
        jd = fetch_detail_jd(rec["url"])
        if len(jd) > len(rec["description"]):
            rec["description"] = jd
    return rows


def enrich_stored(con, limit: int = 20) -> int:
    """Fetch full JDs for stored StepStone rows that only have a teaser.
    Returns how many rows were upgraded."""
    relevant_re = re.compile(
        r"machine\s*learning|maschinelles|\b(ai|ki|ml)\b|deep\s*learning|data\s*science|datenanaly"
        r"|\brobot|robotik|computer\s*vision|bildverarbeitung|\bllm\b|\bnlp\b|informatik|software|perception|autonom|vision|python", re.I)
    rows = con.execute(
        "SELECT url_hash, title, url FROM jobs WHERE source='stepstone' "
        "AND LENGTH(description) < 800").fetchall()
    cands = [r for r in rows
             if ENRICH_TITLE_RE.search(r[1]) and not SENIOR_RE.search(r[1])
             and relevant_re.search(r[1])]
    print(f"  + stepstone enrich: {len(cands)} candidates, fetching up to {limit}…")
    n = 0
    for url_hash, title, url in cands[:limit]:
        jd = fetch_detail_jd(url)
        if len(jd) > 800:
            con.execute("UPDATE jobs SET description=? WHERE url_hash=?",
                        (jd[:15000], url_hash))
            n += 1
    print(f"  + stepstone enrich: {n} JDs upgraded")
    return n


if __name__ == "__main__":
    import pprint
    rows = harvest(["werkstudent machine learning"], ["berlin"], pages=1, detail_limit=3)
    pprint.pp(rows[:2])
