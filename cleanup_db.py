"""One-time DB cleanup: rebuild url_hash to the canonical URL-based scheme,
collapse duplicate listings (same canonical URL stored under both old and new
hash schemes), and migrate into a clean table. Idempotent only insofar as it
collapses to the canonical scheme; run once.

Usage: python cleanup_db.py [path_to_jobs.db]
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def norm_url(u: str) -> str:
    if not u:
        return ""
    return u.split("?")[0].split("#")[0].rstrip("/")


def h1(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()


def best(rows: list[tuple], idx: int) -> str:
    vals = [r[idx] or "" for r in rows]
    nonempty = [v for v in vals if v.strip()]
    if not nonempty:
        return ""
    return max(nonempty, key=len)


def main(path: Path) -> None:
    con = sqlite3.connect(path)
    rows = con.execute(
        "SELECT url_hash, source, title, company, location, url, description, "
        "date_posted, first_seen, track FROM jobs").fetchall()

    groups: dict[str, list[tuple]] = {}
    for r in rows:
        url = r[5] or ""
        key = h1(norm_url(url)) if url else h1(
            re.sub(r"\W+", "", (r[2] or "").lower()) + "|" +
            re.sub(r"\W+", "", (r[3] or "").lower()))
        groups.setdefault(key, []).append(r)

    merged = []
    for key, grp in groups.items():
        title = best(grp, 2)
        company = best(grp, 3)
        location = best(grp, 4)
        url = best(grp, 5)
        desc = max((r[6] or "") for r in grp) or ""
        dates = [r[7] or "" for r in grp if r[7]]
        date_posted = dates and max(dates) or ""
        first_seen = min(r[8] or datetime.now(timezone.utc).isoformat(timespec="seconds")
                         for r in grp)
        source = max((r[1] or "") for r in grp)
        track = max((r[9] or "") for r in grp)
        merged.append((key, source, title, company, location, url,
                       desc[:15000], date_posted, first_seen, track))

    old, new = len(rows), len(merged)
    con.execute("DROP TABLE IF EXISTS jobs_new")
    con.execute(
        """CREATE TABLE jobs_new(
            url_hash TEXT PRIMARY KEY, source TEXT, title TEXT, company TEXT,
            location TEXT, url TEXT, description TEXT, date_posted TEXT,
            first_seen TEXT, track TEXT)""")
    con.executemany("INSERT INTO jobs_new VALUES (?,?,?,?,?,?,?,?,?,?)", merged)
    con.execute("DROP TABLE jobs")
    con.execute("ALTER TABLE jobs_new RENAME TO jobs")
    con.commit()
    con.close()
    print(f"cleanup done: {old} rows -> {new} unique jobs "
          f"(-{old - new} duplicates removed)")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1
         else Path(__file__).resolve().parent / "jobs.db")