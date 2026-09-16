#!/usr/bin/env python3
"""
German AI Job Radar — Web App
=============================
Browser UI for the job radar pipeline: configure sources → live scrape console
→ resume scoring → explore ranked jobs → market insights.

Run:    python app.py          (then open http://localhost:8000)
Stdlib server only — no extra dependencies beyond the radar's own.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

LOCK = threading.Lock()
STATE: dict = {
    "status": "idle",          # idle | scraping | scoring | ready | error
    "log": [],
    "stats": {},
    "config": {
        "sources": ["arbeitnow", "stepstone"],       # + "jobspy", "fromhtml"
        "tracks": ["student", "fulltime"],
        "terms_student": [
            "Werkstudent Machine Learning", "Werkstudent AI",
            "Werkstudent Data Science", "Werkstudent Robotics",
            "Werkstudent Computer Vision",
        ],
        "terms_fulltime": [
            "Junior Machine Learning Engineer", "Junior Data Scientist",
            "Junior AI Engineer", "Junior Data Engineer",
        ],
        "cities": ["Berlin", "München", "Stuttgart"],
        "results_wanted": 12,
        "arbeitnow_pages": 8,
        "stepstone_details": 12,
    },
    "jobs": {"student": [], "fulltime": []},   # scored, in-memory
}

DEFAULT_TERMS = json.loads(json.dumps(STATE["config"]["terms_student"]))


def log(msg: str) -> None:
    with LOCK:
        STATE["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        STATE["log"] = STATE["log"][-400:]


def clear_log() -> None:
    """Wipe the console panel so each run starts clean."""
    with LOCK:
        STATE["log"] = []


def set_status(s: str) -> None:
    with LOCK:
        STATE["status"] = s


# ------------------------------ pipeline -------------------------------

def apply_config(cfg: dict) -> None:
    import job_radar as jr
    for key, val in cfg.items():
        if key not in STATE["config"]:
            continue
        STATE["config"][key] = val
    jr.RESULTS_WANTED = int(STATE["config"]["results_wanted"])
    jr.ARBEITNOW_PAGES = int(STATE["config"]["arbeitnow_pages"])
    jr.CITIES = list(STATE["config"]["cities"])


def run_pipeline() -> None:
    import sqlite3
    import job_radar as jr

    cfg = STATE["config"]
    terms = ([t for t in cfg["terms_student"] if "student" in cfg["tracks"]] +
             [t for t in cfg["terms_fulltime"] if "fulltime" in cfg["tracks"]])
    con = jr.init_db()

    try:
        set_status("scraping")
        clear_log()
        log("— run started —")

        if "arbeitnow" in cfg["sources"]:
            log("Arbeitnow API…")
            rows = jr.scrape_arbeitnow()
            n = sum(1 for r in rows if jr.upsert(con, r))
            log(f"arbeitnow: {len(rows)} listings, {n} new")

        if "jobspy" in cfg["sources"] and terms:
            try:
                for term in terms:
                    rows = jr.scrape_jobspy([term])
                    n = sum(1 for r in rows if jr.upsert(con, r))
                    log(f"jobspy [{term}]: {len(rows)} listings, {n} new")
            except ImportError:
                log("! python-jobspy not installed — skipping (pip install python-jobspy)")

        if "stepstone" in cfg["sources"] and terms:
            from stepstone import harvest as ss_harvest
            rows = ss_harvest(terms, list(cfg["cities"]),
                              detail_limit=int(cfg["stepstone_details"]))
            n = sum(1 for r in rows if jr.upsert(con, r))
            log(f"stepstone: {len(rows)} listings, {n} new")

        if "fromhtml" in cfg["sources"]:
            folder = BASE / "saved_pages"
            if folder.exists():
                from job_radar import import_saved_html
                import_saved_html(con, folder)
            else:
                log("! saved_pages/ folder not found — skipping saved-HTML import")

        con.commit()
        total = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        with LOCK:
            STATE["stats"]["total_jobs"] = total
        log(f"database now holds {total} jobs")

        score_now(con)
        log("✔ run complete — explore your ranked jobs")
        set_status("ready")
    except Exception as e:
        log(f"✖ ERROR: {type(e).__name__}: {str(e)[:200]}")
        set_status("error")
    finally:
        con.close()


def score_now(con=None) -> None:
    import sqlite3
    import job_radar as jr

    own = con is None
    con = con or jr.init_db()
    if own:
        clear_log()
    set_status("scoring")
    log("scoring against resume profile…")
    try:
        with LOCK:
            STATE["jobs"] = {"student": [], "fulltime": []}
        for track in ("student", "fulltime"):
            scored = jr.score_all(con, track)
            rows = []
            for s in scored:
                rows.append({
                    "fit": s["fit_score"],
                    "title": s["title"], "company": s["company"],
                    "location": s["location"], "source": s["source"],
                    "german": s["german_demanded"] == "yes",
                    "missing": s["missing_skills"],
                    "posted": s["posted"], "url": s["url"],
                    "solid": s["_sufficient"],
                    "n_skills": len(s["_demanded"]),
                })
            with LOCK:
                STATE["jobs"][track] = rows
            try:
                jr.write_outputs(scored, track)
            except Exception as e:
                log(f"! report write failed: {e}")
        with LOCK:
            STATE["stats"].update({
                "student": len(STATE["jobs"]["student"]),
                "fulltime": len(STATE["jobs"]["fulltime"]),
            })
        log(f"scored: {STATE['stats']['student']} student + "
            f"{STATE['stats']['fulltime']} junior roles")
        con.commit()          # persist track labels (needed for dual-track query)
    except Exception as e:
        log(f"✖ scoring error: {type(e).__name__}: {str(e)[:200]}")
        set_status("error")
    finally:
        if own:
            con.close()
            if STATE["status"] == "scoring":
                set_status("ready")


def auto_load() -> None:
    """On server start: if the DB already has data, score it so Explore works."""
    import sqlite3
    db = BASE / "jobs.db"
    if not db.exists():
        return
    con = sqlite3.connect(db)
    try:
        n = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    except sqlite3.OperationalError:
        n = 0
    con.close()
    if n:
        with LOCK:
            STATE["stats"]["total_jobs"] = n
        log(f"loaded existing database: {n} jobs")
        score_now()
        log("ready — Explore tab has your ranked jobs")


def insights_payload() -> dict:
    import re as _re
    import job_radar as jr
    import topic_demand as td

    jobs = td.load_relevant_jobs()
    n = len(jobs)
    seg_count, topics = {}, {}
    for name, (stack, rxs, _credit) in td.TOPICS.items():
        topics[name] = {"stack": stack, "count": 0, "credit": td.topic_credit(name)}
    compiled = td.COMPILED
    for j in jobs:
        text = f"{j['title']} {j['desc']}"
        seg = td.seg_of(j["title"], j["desc"])
        seg_count[seg] = seg_count.get(seg, 0) + 1
        for name in td.TOPICS:
            if any(rx.search(text) for rx in compiled[name]):
                topics[name]["count"] += 1
    con = jr.init_db()
    dual = jr.dual_track_companies(con)
    con.close()
    return {"n": n, "segments": seg_count, "topics": topics, "dual": dual[:12],
            "n_student": sum(1 for j in jobs if j["track"] == "student"),
            "n_ft": sum(1 for j in jobs if j["track"] == "fulltime")}


# ------------------------------- server --------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            f = (BASE / "web" / "index.html").read_bytes()
            self._send(200, f, "text/html; charset=utf-8")
        elif u.path.startswith("/docs/") and u.path != "/docs/":
            allowed = (BASE / "docs").resolve()
            target = (allowed / u.path.removeprefix("/docs/")).resolve()
            if allowed in target.parents and target.is_file():
                ct = ("image/png" if target.suffix == ".png"
                      else "text/markdown; charset=utf-8" if target.suffix == ".md"
                      else "application/octet-stream")
                self._send(200, target.read_bytes(), ct)
            else:
                self._json({"error": "not found"}, 404)
        elif u.path == "/api/state":
            with LOCK:
                self._json({"status": STATE["status"], "log": STATE["log"][-120:],
                            "stats": STATE["stats"], "config": STATE["config"]})
        elif u.path == "/api/config":
            with LOCK:
                self._json(STATE["config"])
        elif u.path == "/api/jobs":
            track = q.get("track", ["student"])[0]
            try:
                min_fit = int(q.get("min_fit", ["0"])[0])
            except ValueError:
                self._json({"error": "min_fit must be an integer"}, 400)
                return
            only_ger = q.get("german", ["0"])[0] == "1"
            solid_only = q.get("solid", ["1"])[0] == "1"
            text = unquote(q.get("q", [""])[0]).lower()
            with LOCK:
                rows = list(STATE["jobs"].get(track, []))
            out = []
            for r in rows:
                if r["fit"] is None or r["fit"] < min_fit:
                    continue
                if only_ger and not r["german"]:
                    continue
                if solid_only and not r["solid"]:
                    continue
                if text and text not in (r["title"] + " " + r["company"]).lower():
                    continue
                out.append(r)
            out.sort(key=lambda r: -(r["fit"] or 0))
            self._json({"count": len(out), "jobs": out[:200]})
        elif u.path == "/api/job":
            url = unquote(q.get("u", [""])[0])
            import sqlite3
            import job_radar as jr
            con = jr.init_db()
            row = con.execute(
                "SELECT title, company, location, source, url, description, date_posted"
                " FROM jobs WHERE url=?", (url,)).fetchone()
            con.close()
            if not row:
                self._json({"error": "not found"}, 404)
                return
            self._json({"title": row[0], "company": row[1], "location": row[2],
                        "source": row[3], "url": row[4], "description": row[5],
                        "posted": row[6]})
        elif u.path == "/api/insights":
            try:
                self._json(insights_payload())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif u.path == "/api/profile":
            import job_radar as jr
            self._json({"ok": True, "name": jr.active_profile().get("name", ""),
                        "path": str(jr.PROFILE_FILE), "profile": jr.active_profile()})
        else:
            self._json({"error": "unknown endpoint"}, 404)

    def do_POST(self) -> None:
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                self._json({"error": "invalid JSON body"}, 400)
                return

        if u.path == "/api/config":
            apply_config(body)
            log("config updated: sources=" + ",".join(STATE["config"]["sources"]) +
                f" · cities={len(STATE['config']['cities'])} · tracks={STATE['config']['tracks']}")
            self._json({"ok": True, "config": STATE["config"]})

        elif u.path == "/api/scrape":
            with LOCK:
                if STATE["status"] in ("scraping", "scoring"):
                    self._json({"ok": False, "error": "a run is already active"}, 409)
                    return
            threading.Thread(target=run_pipeline, daemon=True).start()
            self._json({"ok": True})

        elif u.path == "/api/rescore":
            with LOCK:
                if STATE["status"] in ("scraping", "scoring"):
                    self._json({"ok": False, "error": "a run is already active"}, 409)
                    return
            threading.Thread(target=score_now, daemon=True).start()
            self._json({"ok": True})

        elif u.path == "/api/profile":
            import job_radar as jr
            data = body.get("profile") or body.get("data")
            if data is None and isinstance(body.get("text"), str):
                try:
                    data = json.loads(body["text"])
                except json.JSONDecodeError as e:
                    self._json({"ok": False, "error": f"invalid JSON: {e}"}, 400)
                    return
            if not isinstance(data, dict) or not data.get("keywords"):
                self._json({"ok": False, "error": "profile needs a 'keywords' object"},
                           400)
                return
            path = BASE / "profile.json"
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                            encoding="utf-8")
            jr.load_profile(path, warn=False)
            cfg = jr.active_profile()
            tracks = cfg.get("tracks", {})
            if tracks.get("student"):
                STATE["config"]["terms_student"] = [str(x) for x in tracks["student"]]
            if tracks.get("fulltime"):
                STATE["config"]["terms_fulltime"] = [str(x) for x in tracks["fulltime"]]
            if isinstance(cfg.get("cities"), list):
                STATE["config"]["cities"] = [str(x) for x in cfg["cities"]]
            apply_config(STATE["config"])
            log(f"profile updated: {cfg.get('name', 'unnamed')} — re-scoring…")
            threading.Thread(target=score_now, daemon=True).start()
            self._json({"ok": True, "name": cfg.get("name", "")})

        elif u.path == "/api/insights":
            try:
                self._json(insights_payload())
            except Exception as e:
                self._json({"error": str(e)}, 500)

        else:
            self._json({"error": "unknown endpoint"}, 404)

    def log_message(self, *a):  # silence request spam
        pass


def main() -> None:
    threading.Thread(target=auto_load, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"German AI Job Radar → http://localhost:{port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
