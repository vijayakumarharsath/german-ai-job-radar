#!/usr/bin/env python3
"""
Indeed-direct scraper.

Two modes:
1. parse_saved_html(text) — parses a search-results page you saved from your
   browser (Ctrl+S → "Webpage, Complete" or just the .html). VERIFIED against
   real de.indeed.com markup:
     card container  div.slider_item
     title + jk      a[data-jk]  (inner span[id^=jobTitle-])
     company         [data-testid='company-name']
     location        [data-testid='text-location']
   The jk gives the canonical detail URL: https://de.indeed.com/viewjob?jk={jk}
   Full JDs: open viewjob URLs locally (or let jobspy fetch them on your laptop).

2. try_live_search() — direct requests attempt. Indeed sits behind Cloudflare
   and 403-blocks datacenter IPs almost always; from a home-IP laptop it often
   works. Raises BlockedError with instructions when blocked.
"""

from __future__ import annotations

import html as html_mod
import re
import time

import requests

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


class BlockedError(Exception):
    pass


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html_mod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_saved_html(page_html: str) -> list[dict]:
    """Parse an Indeed search results page (live-fetched OR saved from browser)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page_html, "lxml")
    out = []
    for card in soup.select("div.slider_item"):
        a = card.select_one("a[data-jk]")
        if not a:
            continue
        jk = a.get("data-jk", "")
        comp = card.select_one("[data-testid='company-name']")
        loc = card.select_one("[data-testid='text-location']")
        snippet = card.select_one("[data-testid='attribute_snippet'], div.job-snippet")
        out.append({
            "source": "indeed-direct",
            "title": a.get_text(strip=True),
            "company": _clean(comp.get_text(" ", strip=True)) if comp else "",
            "location": _clean(loc.get_text(" ", strip=True)) if loc else "",
            "url": f"https://de.indeed.com/viewjob?jk={jk}",
            "description": _clean(snippet.get_text(" ", strip=True)) if snippet else "",
            "date_posted": "",
        })
    return out


def fetch_detail_jd(viewjob_url: str) -> str:
    """Fetch one Indeed viewjob page and extract the JD text. Works only from
    non-blocked IPs (i.e. usually your laptop, not a cloud box)."""
    try:
        r = requests.get(viewjob_url, headers=HEADERS, timeout=30)
    except Exception as e:
        raise BlockedError(f"network error: {e}") from e
    if r.status_code != 200 or "cf-chl" in r.text or "Attention Required" in r.text:
        raise BlockedError(f"Indeed blocked this IP (HTTP {r.status_code})")
    m = re.search(r'<div id="jobDescriptionText"[^>]*>(.*?)</div>\s*</div>',
                  r.text, re.S)
    if m:
        return _clean(m.group(1))
    # fallback: strip whole page
    txt = _clean(re.sub(r"<script.*?</script>", " ", r.text, flags=re.S))
    i = txt.find("Jobbeschreibung")
    j = txt.find("Job Description")
    k = min(x for x in (i, j, 10 ** 9) if x >= 0)
    return txt[k:k + 8000] if k < 10 ** 9 else ""


def try_live_search(query: str, city: str = "", start: int = 0) -> list[dict]:
    """Direct Indeed search. Raises BlockedError on Cloudflare challenge."""
    params = {"q": query, "l": city, "start": str(start)}
    try:
        r = requests.get("https://de.indeed.com/jobs", params=params,
                         headers=HEADERS, timeout=30)
    except Exception as e:
        raise BlockedError(f"network error: {e}") from e
    if r.status_code != 200 or "cf-chl" in r.text or "Attention Required" in r.text:
        raise BlockedError(
            f"Indeed blocked this IP (HTTP {r.status_code}). "
            "Fix: run from your home laptop (usually passes), use jobspy locally, "
            "or save the search page in your browser and load it with --from-html.")
    cards = parse_saved_html(r.text)
    if not cards:
        raise BlockedError("Indeed returned a page without job cards (likely a JS "
                           "challenge). Use --from-html with a saved page instead.")
    return cards


if __name__ == "__main__":
    import sys
    path = sys.argv[1]
    rows = parse_saved_html(open(path, encoding="utf-8", errors="ignore").read())
    print(f"{len(rows)} jobs from saved page")
    for r in rows[:3]:
        print(" ", r["title"][:60], "|", r["company"][:25], "|", r["url"][:60])
