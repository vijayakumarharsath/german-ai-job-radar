#!/usr/bin/env python3
"""
German Job Radar — two tracks
=============================
TRACK 1 "student"    : Werkstudent / Working Student / Praktikum / HiWi (during M.Sc.)
TRACK 2 "fulltime"   : Junior / Graduate / Trainee / entry-level roles (after M.Sc., 2027)

Sources: Indeed DE + LinkedIn (python-jobspy), StepStone (live, verified),
Arbeitnow API, browser-saved HTML fallback for Indeed.
Every JD is scored against the ACTIVE profile — edit `profile.json` (or pass
`--profile`) to score against YOUR resume, search terms and cities.

Usage:
    pip install python-jobspy requests beautifulsoup4 lxml pandas
    python job_radar.py                              # all sources, both tracks
    python job_radar.py --sources stepstone,fromhtml # only StepStone + saved pages
    python job_radar.py --no-scrape --enrich-stepstone  # upgrade teaser JDs, rescore
    python job_radar.py --no-scrape                  # rescore jobs.db only

Outputs (output/):
    jobs_ranked.csv + market_report.md            (student track)
    jobs_ranked_fulltime.csv + market_report_fulltime.md (junior/graduate track)
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "jobs.db"
OUT = BASE / "output"
OUT.mkdir(exist_ok=True)

# ----------------------------- config ---------------------------------

SEARCH_TERMS = [          # student track
    "Werkstudent Machine Learning",
    "Werkstudent AI",
    "Werkstudent Data Science",
    "Werkstudent Robotics",
    "Working Student Machine Learning",
    "Werkstudent Computer Vision",
]
FULLTIME_TERMS = [        # junior / post-graduation track
    "Junior Machine Learning Engineer",
    "Junior Data Scientist",
    "Junior AI Engineer",
    "Junior Software Engineer",
    "Junior Computer Vision",
    "Junior Robotics",
    "Junior Data Engineer",
    "Trainee Data Science",
    "Graduate Program IT",
]
CITIES = ["Berlin", "München", "Stuttgart"]
HOURS_OLD = 24 * 14
RESULTS_WANTED = 12
ARBEITNOW_PAGES = 10

# --------------------- track classification ----------------------------

STUDENT_RE = re.compile(
    r"werk[s]?student|studierende|working\s*student|studentische[rs]?\s+hilfskraft"
    r"|praktikant|praktikum|\bintern\b|student\s+trainee|abschlussarbeit|thesis"
    r"|\bhiwi\b",
    re.I,
)
FT_TITLE_RE = re.compile(
    r"\bjunior\b|\bjun\.|graduate|absolvent(?:in|en)?\b|einstieg|berufseinsteiger"
    r"|\btrainee\b|entry[\s-]?level|\bassociate\b|\bgraduate program\b",
    re.I,
)
SENIOR_TITLE_RE = re.compile(
    r"\bsenior\b|\bsr\.?\b|\blead\b|principal|\bstaff\b|head\s+of|manager\b"
    r"|director|\bexperte?\b|spezialist|\bteamleiter\b|\bchapter lead\b",
    re.I,
)
ENTRY_DESC_RE = re.compile(
    r"\b0\s*[-–to]+\s*[12]\s*(jahre|years)|\b1\s*[-–to]+\s*2\s*(jahre|years)"
    r"|erste\s+(?:beruf)?erfahrung|berufseinsteig|recent(?:ly)?\s+graduat"
    r"|einstiegs(?:position|rolle|m)",
    re.I,
)


def classify_track(title: str, description: str = "") -> str | None:
    """'student' | 'fulltime' | None (irrelevant for either track)."""
    if STUDENT_RE.search(title):
        return "student"
    if SENIOR_TITLE_RE.search(title):
        return None
    if FT_TITLE_RE.search(title):
        return "fulltime"
    if description and ENTRY_DESC_RE.search(description):
        return "fulltime"
    return None


RELEVANT_TITLE_RE = re.compile(
    r"machine\s*learning|maschinelles|deep\s*learning|\b(ai|ki|ml)\b|künstliche"
    r"|artificial\s*intelligence|data\s*science|datenanaly|data\s*engineer"
    r"|\brobot|robotik|computer\s*vision|bildverarbeitung|\bllm\b|\bnlp\b"
    r"|informatik|software|perception|autonom|vision",
    re.I,
)
# used to keep non-title matches (German JDs often have generic titles)
CORE_ML_RE = [
    re.compile(p, re.I)
    for p in (
        r"machine\s*learning|maschinelles?\s*lernen",
        r"deep\s*learning",
        r"pytorch|tensorflow",
        r"\bros\b|\bros2\b",
        r"computer\s*vision|bildverarbeitung",
        r"\bllms?\b|neuronale\s*netze|neural",
    )
]

# ----------------- resume vs. market keyword profile -------------------
# The ACTIVE profile (./profile.json at runtime, or --profile) drives scoring:
# keyword weights (3=High/2=Med/1=Low), YOUR level per skill (strong/partial/
# novice/none), the search terms and the cities. The dict below is the built-in
# starter profile, used only when no profile file is present or loadable.
_DEFAULT_KEYWORDS = {
    "Python":                    (3, [r"python"], 1.0),
    "Machine Learning":          (3, [r"machine\s*learning", r"maschinelles?\s*lernen"], 1.0),
    "Deep Learning":             (3, [r"deep\s*learning"], 1.0),
    "PyTorch":                   (3, [r"pytorch"], 0.0),
    "TensorFlow/Keras":          (3, [r"tensorflow", r"\bkeras\b"], 1.0),
    "Computer Vision":           (3, [r"computer\s*vision", r"bildverarbeitung"], 1.0),
    "OpenCV":                    (2, [r"opencv"], 1.0),
    "Object detect/segmentation":(2, [r"object\s*detection", r"objekt(?:er)?kennung", r"objektdetektion", r"\byolo\b", r"segmentier", r"image\s*classification", r"pose\s*estimation", r"posenschätzung"], 0.5),
    "NLP":                       (2, [r"\bnlp\b", r"natural\s*language"], 1.0),
    "LLM/GenAI":                 (3, [r"\bllms?\b", r"large\s*language", r"sprachmodell", r"\bgpt\b", r"genai", r"generative\s*(ai|ki|intelligenz)"], 1.0),
    "HuggingFace/transformers":  (2, [r"hugging[\s-]?face", r"\btransformers\b"], 0.5),
    "LangChain/AI agents":       (2, [r"langchain", r"langgraph", r"agentic", r"multi-?agent"], 0.5),
    "ROS/ROS2":                  (3, [r"\bros\s*2?\b"], 1.0),
    "MoveIt/motion planning":    (2, [r"moveit", r"motion\s*planning", r"bewegungsplanung"], 0.0),
    "SLAM":                      (2, [r"\bslam\b"], 0.0),
    "Nav2/path planning":        (2, [r"\bnav2\b", r"navigation\s*stack", r"path\s*planning", r"pfadplanung"], 0.0),
    "Gazebo/Isaac/robot sim":    (2, [r"gazebo", r"isaac\s*sim", r"mujoco", r"pybullet", r"webots", r"coppeliasim"], 0.25),
    "Kinematics/control theory": (2, [r"kinematik", r"kinematic", r"regelungstechnik", r"control\s*theory", r"kalman", r"koordinatentransformation", r"coordinate\s*transformation"], 0.0),
    "C++":                       (3, [r"\bc\+\+"], 0.5),
    "Java":                      (1, [r"\bjava\b(?!script)"], 1.0),
    "Docker":                    (3, [r"docker", r"containeris", r"containeriz"], 1.0),
    "Kubernetes":                (2, [r"kubernetes", r"\bk8s\b"], 0.0),
    "CI/CD":                     (2, [r"ci/?cd", r"continuous\s*(integration|delivery)", r"github\s*actions", r"gitlab\s*ci"], 0.25),
    "Git":                       (3, [r"\bgit\b", r"github", r"gitlab"], 1.0),
    "Linux/Bash":                (3, [r"\blinux\b", r"ubuntu", r"\bbash\b", r"\bunix\b"], 1.0),
    "SQL":                       (3, [r"\bsql\b", r"mysql", r"postgres", r"\bsqlite\b"], 1.0),
    "Pandas/NumPy":              (3, [r"pandas", r"numpy"], 1.0),
    "scikit-learn":              (3, [r"scikit-?learn", r"sklearn"], 1.0),
    "APIs (FastAPI/Flask/REST)": (2, [r"fastapi", r"flask", r"django", r"rest[\s-]?api", r"\bapi\b"], 0.5),
    "Cloud (AWS/Azure/GCP)":     (2, [r"\baws\b", r"amazon\s*web\s*services", r"azure", r"google\s*cloud", r"\bgcp\b"], 0.0),
    "Spark/Kafka/Airflow":       (1, [r"\bspark\b", r"hadoop", r"kafka", r"airflow"], 0.0),
    "Power BI/Tableau":          (2, [r"power\s*bi", r"tableau", r"looker"], 0.0),
    "Statistics/math":           (2, [r"statisti", r"wahrscheinlichkeits", r"probability"], 1.0),
    "Data pipelines/analysis":   (2, [r"data[\s-]*pipeline", r"datenpipeline", r"\betl\b", r"datenanalyse", r"data\s*analysis", r"datenverarbeitung"], 0.5),
    "Embedded/RPi/sensors":      (2, [r"embedded", r"mikrocontroller", r"microcontroller", r"raspberry", r"arduino", r"sensorik", r"esp32", r"stm32"], 1.0),
    "CUDA/Jetson/edge AI":       (2, [r"\bcuda\b", r"jetson", r"tensorrt", r"edge[\s-]*(ai|computing|deployment)"], 0.25),
    "Reinforcement/imitation":   (2, [r"reinforcement\s*learning", r"bestärkendes?\s*lernen", r"imitation\s*learning", r"diffusion\s*polic"], 0.0),
    "German (B1+)":              (3, [r"\bdeutsch\b(?!land)",
                                      r"deutsch(?:kenntnisse?|kenntnis|sprachig)",
                                      r"deutsch[a-z]*\s+sprache",
                                      r"sprachkenntnisse",
                                      r"\bgerman\b", r"\bb[12]\b",
                                      r"verhandlungssicher"], 0.5),
    "Agile/Scrum":               (2, [r"agile", r"scrum", r"kanban"], 1.0),
    "MATLAB/Simulink":           (2, [r"\bmatlab\b", r"simulink"], 0.0),
    "Digital twin":              (1, [r"digital\s*twin", r"digitalen?\s*zwilling"], 1.0),
}
KEYWORDS = dict(_DEFAULT_KEYWORDS)
COMPILED = {k: re.compile("(?:%s)" % "|".join(f"(?:{v})" for v in variants), re.I)
            for k, (_, variants, _) in KEYWORDS.items()}
PROFILE_FILE = BASE / "profile.json"
_ACTIVE_PROFILE: dict = {"name": "built-in starter profile"}
LEVEL_TO_CREDIT = {"strong": 1.0, "partial": 0.5, "novice": 0.25, "none": 0.0}


def load_profile(path: str | Path | None = None, warn: bool = True) -> dict:
    """Load a profile.json; on success rebuild KEYWORDS/COMPILED plus the search
    config (terms + cities) from it. Falls back to the built-in starter when the
    file is missing or invalid, so the pipeline always runs."""
    global KEYWORDS, COMPILED, SEARCH_TERMS, FULLTIME_TERMS, CITIES, _ACTIVE_PROFILE
    p = Path(path) if path else PROFILE_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as e:
        if warn:
            print(f"! can't load profile {p} ({type(e).__name__}: {e})\n"
                  f"  using the built-in starter profile")
        return _ACTIVE_PROFILE
    kw = {}
    for name, spec in sorted(data.get("keywords", {}).items()):
        patterns = [str(x) for x in spec.get("patterns", [])]
        if not patterns:
            continue
        kw[name] = (int(spec.get("weight", 2)), patterns,
                    LEVEL_TO_CREDIT.get(str(spec.get("level", "none")).lower(), 0.0))
    if kw:
        KEYWORDS = kw
        COMPILED = {k: re.compile("(?:%s)" % "|".join(f"(?:{v})" for v in variants), re.I)
                    for k, (_, variants, _) in kw.items()}
    tracks = data.get("tracks", {})
    if tracks.get("student"):
        SEARCH_TERMS = [str(x) for x in tracks["student"]]
    if tracks.get("fulltime"):
        FULLTIME_TERMS = [str(x) for x in tracks["fulltime"]]
    if isinstance(data.get("cities"), list):
        CITIES = [str(x) for x in data["cities"]]
    _ACTIVE_PROFILE = data
    return data


load_profile(warn=False)


def active_profile() -> dict:
    """The currently loaded profile (dict as read from profile.json)."""
    return _ACTIVE_PROFILE


def clear_console() -> None:
    """Clear the terminal so each run starts with a fresh screen."""
    try:
        os.system("cls" if os.name == "nt" else "clear")
    except Exception:
        pass

# ----------------------------- helpers ---------------------------------

def clean_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def norm_url(u: str) -> str:
    if not u:
        return ""
    return u.split("?")[0].split("#")[0].rstrip("/")


def init_db() -> sqlite3.Connection:
    try:
        con = sqlite3.connect(DB, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.execute(
            """CREATE TABLE IF NOT EXISTS jobs(
                url_hash TEXT PRIMARY KEY, source TEXT, title TEXT, company TEXT,
                location TEXT, url TEXT, description TEXT, date_posted TEXT,
                first_seen TEXT)"""
        )
        con.execute("SELECT count(*) FROM jobs").fetchone()
    except sqlite3.DatabaseError:
        try:
            con.close()
        except Exception:
            pass
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bad = DB.with_name(f"{DB.name}.corrupt-{stamp}")
        DB.replace(bad)
        for ext in ("-wal", "-shm"):
            side = DB.with_name(DB.name + ext)
            if side.exists():
                side.replace(DB.with_name(bad.name + ext))
        print(f"⚠ jobs.db unreadable — quarantined as {bad.name}, rebuilding empty DB")
        con = sqlite3.connect(DB, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.execute(
            """CREATE TABLE IF NOT EXISTS jobs(
                url_hash TEXT PRIMARY KEY, source TEXT, title TEXT, company TEXT,
                location TEXT, url TEXT, description TEXT, date_posted TEXT,
                first_seen TEXT)"""
        )
    try:
        con.execute("ALTER TABLE jobs ADD COLUMN track TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    return con


def upsert(con: sqlite3.Connection, rec: dict) -> bool:
    """Insert-or-refresh one listing. Cross-source dedup key = normalized URL
    (same job re-listed on any site collapses to one row); jobs without a URL
    fall back to title+company."""
    tc = hashlib.sha1(
        (re.sub(r"\W+", "", rec["title"].lower()) + "|" +
         re.sub(r"\W+", "", rec["company"].lower())).encode()).hexdigest()
    url = norm_url(rec.get("url", ""))
    key = hashlib.sha1(url.encode()).hexdigest() if url else tc
    row = con.execute("SELECT title, company, location, description, date_posted"
                      " FROM jobs WHERE url_hash=?", (key,)).fetchone()
    if row:
        _refresh_if_stale(con, key, row, rec)
        return False
    track = classify_track(rec["title"], rec["description"]) or ""
    con.execute(
        "INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
        (key, rec["source"], rec["title"], rec["company"], rec["location"],
         rec["url"], rec["description"][:15000], rec["date_posted"],
         datetime.now(timezone.utc).isoformat(timespec="seconds"), track),
    )
    return True


def _refresh_if_stale(con: sqlite3.Connection, key: str,
                      row: tuple, rec: dict) -> None:
    """A re-seen listing may carry a fuller JD, a better company/location or a
    real date than the row we already have — upgrade only what actually helps."""
    cur_title, cur_comp, cur_loc, cur_desc, cur_date = row
    new_desc = rec.get("description", "")
    if new_desc and len(new_desc) > len(cur_desc):
        con.execute("UPDATE jobs SET description=? WHERE url_hash=?",
                    (new_desc[:15000], key))
        cur_desc = new_desc
    for field, new, old in (("company", rec.get("company", ""), cur_comp),
                            ("location", rec.get("location", ""), cur_loc),
                            ("title", rec.get("title", ""), cur_title)):
        new = str(new or "").strip()
        old = str(old or "").strip()
        if new and (len(new) > len(old) or not old):
            con.execute(f"UPDATE jobs SET {field}=? WHERE url_hash=?", (new, key))
    new_date = str(rec.get("date_posted") or "").strip()
    if new_date and not cur_date:
        con.execute("UPDATE jobs SET date_posted=? WHERE url_hash=?",
                    (new_date, key))

# ----------------------------- scrapers --------------------------------

def scrape_jobspy(terms: list[str]) -> list[dict]:
    import requests
    import pandas as pd
    from jobspy import scrape_jobs

    rows = []
    for term in terms:
        for city in CITIES:
            df = pd.DataFrame()
            for attempt in range(2):
                try:
                    df = scrape_jobs(
                        site_name=["indeed", "linkedin"],
                        search_term=term,
                        location=f"{city}, Germany",
                        results_wanted=RESULTS_WANTED,
                        hours_old=HOURS_OLD,
                        country_indeed="germany",
                    )
                    break
                except Exception as e:
                    if attempt == 1:
                        print(f"  ! {term} @ {city}: {type(e).__name__}: {str(e)[:110]}")
                    else:
                        time.sleep(2)
            for r in df.to_dict("records"):
                comp = r.get("company")
                comp = "" if comp is None or str(comp) in ("nan", "None") else str(comp)
                loc = r.get("location")
                loc = "" if loc is None or str(loc) in ("nan", "None") else str(loc)
                desc_raw = r.get("description")
                if desc_raw is None or (pd is not None and pd.isna(desc_raw)):
                    desc_raw = ""
                dp = r.get("date_posted")
                try:
                    dp = str(pd.Timestamp(dp).date()) if dp is not None and not pd.isna(dp) else ""
                except Exception:
                    dp = str(dp or "")
                rows.append({
                    "source": f"indeed/linkedin:{r.get('site', '')}",
                    "title": str(r.get("title") or "").strip(),
                    "company": comp,
                    "location": loc,
                    "url": str(r.get("job_url") or ""),
                    "description": clean_html(str(desc_raw)),
                    "date_posted": dp,
                })
            print(f"  + {term} @ {city}: {len(df)} listings")
            time.sleep(0.8)
    return rows


def scrape_arbeitnow() -> list[dict]:
    import requests
    rows = []
    for page in range(1, ARBEITNOW_PAGES + 1):
        try:
            r = requests.get(
                "https://www.arbeitnow.com/api/job-board-api",
                params={"page": page},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json().get("data", [])
        except Exception as e:
            print(f"  ! arbeitnow page {page}: {e}")
            break
        for j in data:
            created = j.get("created_at")
            rows.append({
                "source": "arbeitnow",
                "title": str(j.get("title") or "").strip(),
                "company": str(j.get("company_name") or ""),
                "location": str(j.get("location") or ""),
                "url": str(j.get("url") or f"https://www.arbeitnow.com/jobs/{j.get('slug', '')}"),
                "description": clean_html(j.get("description") or ""),
                "date_posted": str(datetime.fromtimestamp(created, tz=timezone.utc).date()) if created else "",
            })
    print(f"  + arbeitnow: {len(rows)} listings ({page} pages)")
    return rows

# ----------------------------- scoring ---------------------------------

def score_all(con: sqlite3.Connection, track: str) -> list[dict]:
    rows = con.execute(
        "SELECT source,title,company,location,url,description,date_posted,"
        "COALESCE(track,''),url_hash FROM jobs"
    ).fetchall()
    scored, kept_total = [], 0
    for src, title, comp, loc, url, desc, dp, stored_track, url_hash in rows:
        if stored_track:
            t = stored_track
        else:
            t = classify_track(title, desc) or ""
            con.execute("UPDATE jobs SET track=? WHERE url_hash=?"
                        " AND COALESCE(track,'')=''", (t, url_hash))
        if t != track:
            continue
        kept_total += 1
        desc = "" if desc.strip().lower() in ("nan", "none") else desc
        text = f"{title} {desc}"
        relevant = bool(RELEVANT_TITLE_RE.search(title)) or \
            sum(1 for p in CORE_ML_RE if p.search(desc)) >= 3
        if not relevant:
            continue
        demanded = [k for k in KEYWORDS if COMPILED[k].search(text)]
        sufficient = len(demanded) >= 3   # enough JD text to trust the score
        wsum = sum(KEYWORDS[k][0] for k in demanded)
        fit = round(100 * sum(KEYWORDS[k][0] * KEYWORDS[k][2] for k in demanded) / wsum) if (wsum and sufficient) else None
        missing = sorted(
            [k for k in demanded if KEYWORDS[k][2] < 0.75],
            key=lambda k: (-KEYWORDS[k][0], KEYWORDS[k][2]),
        )
        scored.append({
            "fit_score": fit,
            "title": title,
            "company": comp or "n/a",
            "location": loc or "n/a",
            "source": src,
            "german_demanded": "yes" if "German (B1+)" in demanded else "",
            "missing_skills": "; ".join(
                f"{k}{'*' if KEYWORDS[k][2] else ''}" for k in missing[:6]
            ),
            "posted": dp or "n/a",
            "url": url,
            "_demanded": demanded,
            "_sufficient": sufficient,
        })
    scored.sort(key=lambda d: (0 if d["_sufficient"] else 1, -(d["fit_score"] or 0)))
    solid = sum(1 for s in scored if s["_sufficient"])
    print(f"  [{track}] {kept_total} track jobs -> {len(scored)} relevant "
          f"({solid} with full JD text, {len(scored) - solid} low-signal flagged)")
    return scored


def write_outputs(scored: list[dict], track: str) -> Path:
    suffix = "" if track == "student" else "_fulltime"
    out_rows = []
    for s in scored:
        r = {k: v for k, v in s.items() if not k.startswith("_")}
        r["fit_score"] = s["fit_score"] if s["fit_score"] is not None else ""
        r["jd_quality"] = "" if s["_sufficient"] else "short JD — open link to verify"
        out_rows.append(r)
    csv_path = OUT / f"jobs_ranked{suffix}.csv"
    import csv as _csv
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=list(out_rows[0].keys()) if out_rows
                            else ["fit_score", "title"])
        w.writeheader()
        w.writerows(out_rows)

    solid = [s for s in scored if s["_sufficient"]]
    n = len(solid)
    title = ("German Werkstudent Job Market Report" if track == "student"
             else "German Junior / Graduate (Post-Graduation) Market Report")
    lines = [
        f"# {title}",
        f"_Run: {datetime.now(timezone.utc).date()} · {n} relevant jobs scored_",
        "",
        "## What the market demands vs. what you offer",
        "",
        "| # | Skill | Asked in | Priority | Your level |",
        "|---|-------|----------|----------|------------|",
    ]
    demand = []
    for k, (w, _, credit) in KEYWORDS.items():
        cnt = sum(1 for s in solid if k in s["_demanded"])
        if cnt:
            demand.append((cnt, k, w, credit))
    demand.sort(key=lambda t: (-t[0], -t[2]))
    for i, (cnt, k, w, credit) in enumerate(demand[:20], 1):
        level = "Strong" if credit >= 0.75 else ("Partial" if credit >= 0.25 else "MISSING")
        pct = round(100 * cnt / n) if n else 0
        lines.append(f"| {i} | {k} | {cnt}/{n} jobs ({pct}%) | {['Low','Medium','High'][w-1]} | {level} |")
    lines += ["", "## Top 15 best-fit jobs", "",
              "| Fit | Title | Company | Location | Missing for you | Link |",
              "|-----|-------|---------|----------|-----------------|------|"]
    for s in solid[:15]:
        t = (s["title"][:60] + "…") if len(s["title"]) > 60 else s["title"]
        c = s["company"][:28]
        l = s["location"][:24]
        m = (s["missing_skills"][:70] + "…") if len(s["missing_skills"]) > 70 else s["missing_skills"]
        lines.append(f"| {s['fit_score']}% | {t} | {c} | {l} | {m} | [open]({s['url']}) |")
    ger = sum(1 for s in solid if s["german_demanded"])
    flagged = len(scored) - n
    lines += [
        "",
        f"**German demanded:** {ger}/{n} jobs explicitly ask for German.",
        "",
        "\\* = partial coverage in your profile. Full data: `output/jobs_ranked"
        f"{suffix}.csv`",
    ]
    if flagged:
        lines.append(f"{flagged} additional listings had too little JD text to score reliably — "
                     "flagged in the CSV, open the links to judge manually.")
    report = OUT / f"market_report{suffix}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return csv_path


def dual_track_companies(con: sqlite3.Connection) -> list[tuple[str, int, int]]:
    """Companies hiring BOTH students and juniors = Werkstudent→Übernahme pipeline."""
    q = """
        SELECT company,
               SUM(track='student'), SUM(track='fulltime')
        FROM jobs
        WHERE track IN ('student','fulltime') AND company != ''
        GROUP BY company
        HAVING SUM(track='student') > 0 AND SUM(track='fulltime') > 0
        ORDER BY SUM(track='student') + SUM(track='fulltime') DESC
    """
    return [(c, s, f) for c, s, f in con.execute(q).fetchall()]

# ------------------------------- main ----------------------------------

def import_saved_html(con: sqlite3.Connection, folder: Path) -> None:
    """Parse browser-saved search-result pages (Indeed/StepStone) into the DB.
    This always works — even when a site blocks direct scraping."""
    files = sorted(folder.glob("*.html")) + sorted(folder.glob("*.htm"))
    if not files:
        print(f"  ! no .html files in {folder}/")
        return
    from stepstone import parse_search as ss_parse
    from indeed_direct import parse_saved_html as in_parse
    for f in files:
        text = f.read_text(encoding="utf-8", errors="ignore")
        if "stepstone.de" in text[:4000] or "data-at='job-item'" in text or 'data-at="job-item"' in text:
            rows, kind = ss_parse(text), "StepStone"
        elif "indeed." in text[:4000] or "slider_item" in text:
            rows, kind = in_parse(text), "Indeed"
        else:
            print(f"  ? {f.name}: unknown site, skipped")
            continue
        added = sum(1 for rec in rows if upsert(con, rec))
        print(f"  + {kind} page {f.name[:48]}: {len(rows)} jobs ({added} new)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-scrape", action="store_true", help="rescore jobs.db only")
    ap.add_argument("--sources", default="jobspy,arbeitnow,stepstone,indeed",
                    help="comma list: jobspy,arbeitnow,stepstone,indeed,fromhtml")
    ap.add_argument("--from-html", dest="from_html", default="saved_pages",
                    help="folder with browser-saved search pages (for fromhtml)")
    ap.add_argument("--stepstone-details", type=int, default=20,
                    help="max full JDs to fetch from StepStone per run")
    ap.add_argument("--enrich-stepstone", action="store_true",
                    help="fetch full JDs for stored teaser rows, then rescore")
    ap.add_argument("--tracks", default="student,fulltime",
                    help="which tracks to run: student,fulltime")
    ap.add_argument("--profile", default=None,
                    help="path to your profile.json (default: ./profile.json)")
    args = ap.parse_args()
    sources = {s.strip().lower() for s in args.sources.split(",") if s.strip()}
    tracks = {t.strip().lower() for t in args.tracks.split(",") if t.strip()}
    if args.profile:
        load_profile(args.profile)

    clear_console()

    con = init_db()
    con.execute("UPDATE jobs SET description='' WHERE LOWER(COALESCE(description,'')) IN ('nan','none')")

    if args.enrich_stepstone:
        from stepstone import enrich_stored
        enrich_stored(con, limit=args.stepstone_details)
        con.commit()

    if not args.no_scrape:
        terms = ([t for t in SEARCH_TERMS if "student" in tracks] +
                 [t for t in FULLTIME_TERMS if "fulltime" in tracks])
        if "jobspy" in sources and terms:
            print("Scraping (jobspy: Indeed DE + LinkedIn)…")
            for rec in scrape_jobspy(terms):
                upsert(con, rec)
        if "arbeitnow" in sources:
            print("Fetching Arbeitnow API…")
            for rec in scrape_arbeitnow():
                upsert(con, rec)
        if "stepstone" in sources and terms:
            print("Scraping StepStone (live)…")
            from stepstone import harvest as ss_harvest
            for rec in ss_harvest(terms, CITIES, detail_limit=args.stepstone_details):
                upsert(con, rec)
        if "indeed" in sources and terms:
            print("Trying Indeed direct (may be blocked outside home IPs)…")
            from indeed_direct import try_live_search, BlockedError
            warned = False
            for term in terms:
                for city in CITIES:
                    try:
                        rows = try_live_search(term, city)
                        for rec in rows:
                            upsert(con, rec)
                        print(f"  + indeed: {term} @ {city} -> {len(rows)}")
                        time.sleep(1.0)
                    except BlockedError as e:
                        if not warned:
                            print(f"  ! {e}")
                            warned = True
                        break
                if warned:
                    break
        if "fromhtml" in sources:
            print(f"Importing saved HTML pages from {args.from_html}/ …")
            import_saved_html(con, Path(args.from_html))
        con.commit()

    print("\nScoring against resume profile…")
    for track in ("student", "fulltime"):
        if track not in tracks:
            continue
        scored = score_all(con, track)
        if not scored:
            print(f"  [{track}] no relevant jobs found — widen terms/cities.")
            continue
        csv_path = write_outputs(scored, track)
        print(f"  [{track}] TOP 8:")
        for s in [x for x in scored if x["_sufficient"]][:8]:
            print(f"    {s['fit_score']:>3}%  {s['title'][:58]}  —  {s['company'][:30]}")
            print(f"         missing: {s['missing_skills'][:88]}")
            print(f"         {s['url']}")

    if "fulltime" in tracks:
        both = dual_track_companies(con)
        if both:
            ft_report = OUT / "market_report_fulltime.md"
            body = ft_report.read_text(encoding="utf-8")
            body = body.split("\n## Companies hiring BOTH")[0].rstrip() + "\n"
            lines = ["", "## Companies hiring BOTH Werkstudents and Juniors",
                     "_(= realistic Werkstudent → Übernahme pipelines — prioritize these)_", ""]
            for c, s, f in both[:15]:
                lines.append(f"- **{c}** — {s} student + {f} junior listings in DB")
            ft_report.write_text(body + "\n".join(lines) + "\n", encoding="utf-8")
            print(f"\nDual-track employers: {len(both)} (appended to fulltime report)")
    con.commit()   # persist track labels computed during scoring
    print(f"\nWrote reports + CSVs to {OUT}/ · DB: {DB}")
