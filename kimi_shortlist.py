#!/usr/bin/env python3
"""
LLM Shortlist for the German Job Radar — powered by Kimi (Moonshot) API.

Takes the top-ranked jobs from jobs.db (regex-scored first), sends each full JD
to Kimi along with Harsath's candidate profile, and gets back a semantic fit
score + verdict + unmet hard requirements + red flags — the stuff keyword
matching can't see (e.g. "erste Erfahrung" vs "3+ Jahre deep learning").

Setup (on YOUR laptop — never paste the key into any chat):
    export MOONSHOT_API_KEY="sk-..."        # Windows: setx MOONSHOT_API_KEY "sk-..."

Usage:
    python kimi_shortlist.py                          # top 30 jobs, both tracks
    python kimi_shortlist.py --limit 15 --track student
    python kimi_shortlist.py --model kimi-k3          # premium pass on final picks
    python kimi_shortlist.py --dry-run                # no API calls, shows plan+cost

Cost (kimi-k2.6, default): ~2-5 US cents for 30 jobs (prompt caching enabled).
Output: output/llm_shortlist.md + output/llm_shortlist.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
DB = BASE / "jobs.db"
OUT = BASE / "output"
OUT.mkdir(exist_ok=True)

API_URL = "https://api.moonshot.ai/v1/chat/completions"
PRICES = {  # USD per million tokens (input, cached-input, output)
    "kimi-k2.6": {"in": 0.95, "cached": 0.16, "out": 4.00},
    "kimi-k3": {"in": 3.00, "cached": 0.30, "out": 15.00},
}

# Identical prefix on every call -> Moonshot prompt caching bills repeats at the
# cached rate (~6x cheaper). Do NOT personalize per job inside this block.
SYSTEM_PROMPT = """You are a senior technical recruiter for AI/ML/Data Science/Robotics roles in Germany.
Evaluate one job description against the candidate profile below.

CANDIDATE PROFILE — Harsath Vijayakumar:
- M.Sc. Artificial Intelligence & Robotics, Hof University of Applied Sciences (Oct 2025 – Sep 2027 expected)
- Availability: Werkstudent/internship 20 h/week from Oct 2026 (full-time during breaks). Junior full-time roles relevant from Sep 2027.
- Strong: Python, TensorFlow/Keras, scikit-learn, OpenCV/computer vision, OCR, NLP, LLMs (co-author: LLM model-merging survey, experiments on Qwen2.5; fraud-detection LLM paper), agentic AI stack (Open WebUI, NVIDIA API backend, SearXNG, fully Dockerized local agent environment), Git, Linux/Bash, Docker, SQL, Pandas/NumPy, digital twin simulation (Tecnomatix Plant Simulation/SimTalk), embedded (Raspberry Pi, Arduino, Niryo Ned2 robot arm).
- Coursework-level: ROS 2, C++, Java. Building now: PyTorch object-detection + ROS 2 perception project.
- Weak/missing: German (A2, B1 targeted early 2027), cloud platforms (AWS/Azure/GCP), Kubernetes, SLAM/motion-planning theory, PyTorch (in progress).
- Experience: software dev intern (NLP chatbot, scikit-learn, agile scrum), IT-security hackathons.
- Work authorization: Indian national, German student residence permit — working student work allowed (20 h/wk semester, full-time breaks). Post-graduation: standard German work-visa path. No sponsorship needed for student roles.

TASK: Judge REALISTIC hiring chances and genuine fit — not keyword overlap. Distinguish hard requirements (must have) from nice-to-haves. Junior/Trainee roles: judge as if the candidate applies after graduating Sep 2027. Student roles: judge as if applying now.

Reply with ONLY a JSON object, schema:
{"fit_0_100": <int>, "verdict": "<apply|maybe|skip>", "why": "<=25 words",
 "hard_requirements_unmet": ["..."], "german_required": <bool>, "red_flags": ["..."]}"""


def select_jobs(track: str, limit: int) -> list[dict]:
    """Reuse the radar's regex scorer, keep the strongest full-JD candidates."""
    import sqlite3

    import job_radar as jr

    con = sqlite3.connect(DB)
    picks: dict[str, dict] = {}
    tracks = ["student", "fulltime"] if track == "both" else [track]
    for t in tracks:
        for s in jr.score_all(con, t):
            if not s["_sufficient"] or s["fit_score"] is None:
                continue
            row = con.execute("SELECT description FROM jobs WHERE url=?",
                              (s["url"],)).fetchone()
            if not row or len(row[0]) < 800:
                continue
            cur = picks.get(s["url"])
            if cur is None or s["fit_score"] > cur["regex_fit"]:
                picks[s["url"]] = {**s, "jd": row[0], "regex_fit": s["fit_score"]}
    con.close()
    out = sorted(picks.values(), key=lambda d: -d["regex_fit"])
    return out[:limit]


def call_kimi(key: str, model: str, job: dict) -> tuple[dict, dict]:
    """One JD -> structured verdict. Returns (verdict_json, usage)."""
    user = (f"JOB\nTitle: {job['title']}\nCompany: {job['company']}\n"
            f"Location: {job['location']}\nTrack: {job.get('_track', '')}\n"
            f"Description:\n{job['jd'][:9000]}")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 400,
        "response_format": {"type": "json_object"},
    }
    for attempt in (1, 2):
        try:
            r = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json=body, timeout=90,
            )
            if r.status_code == 429 and attempt == 1:
                time.sleep(5)
                continue
            r.raise_for_status()
            data = r.json()
            txt = data["choices"][0]["message"]["content"].strip()
            j = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
            return j, data.get("usage", {})
        except Exception as e:
            if attempt == 2:
                return {"fit_0_100": -1, "verdict": "error", "why": str(e)[:120],
                        "hard_requirements_unmet": [], "german_required": None,
                        "red_flags": []}, {}
            time.sleep(3)
    return {}, {}


def cost_of(model: str, usage: dict) -> float:
    p = PRICES[model]
    u = usage or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    pin = u.get("prompt_tokens", 0)
    cout = u.get("completion_tokens", 0)
    uncached = max(pin - cached, 0)
    return (uncached * p["in"] + cached * p["cached"] + cout * p["out"]) / 1e6


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--track", choices=["student", "fulltime", "both"], default="both")
    ap.add_argument("--model", default="kimi-k2.6", choices=list(PRICES))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    jobs = select_jobs(args.track, args.limit)
    if not jobs:
        print("No full-JD candidates in jobs.db — run job_radar.py first.")
        return
    # tag tracks for the prompt
    for j in jobs:
        j["_track"] = "fulltime" if any(
            k in j["title"].lower() for k in
            ("junior", "graduate", "trainee", "absolvent")) else "student"

    p = PRICES[args.model]
    est = len(jobs) * (1600 * p["in"] + 250 * p["out"]) / 1e6
    print(f"Shortlist: {len(jobs)} jobs · model {args.model} · "
          f"est. cost ${est:.2f} (worst case, no caching)")
    for i, j in enumerate(jobs, 1):
        print(f"  {i:>2}. [{j['_track']:<8}] regex {j['regex_fit']:>3}%  {j['title'][:55]}")
    if args.dry_run:
        print("Dry run — no API calls made.")
        return

    key = os.environ.get("MOONSHOT_API_KEY", "")
    if not key:
        sys.exit("MOONSHOT_API_KEY not set. Export it first (never paste it in chats).")

    results, total_cost, errors = [], 0.0, 0
    for i, j in enumerate(jobs, 1):
        v, usage = call_kimi(key, args.model, j)
        c = cost_of(args.model, usage)
        total_cost += c
        if v.get("verdict") == "error":
            errors += 1
        results.append({**j, "llm": v, "cost_usd": round(c, 5)})
        print(f"  [{i}/{len(jobs)}] {v.get('fit_0_100', '?'):>3} "
              f"{v.get('verdict', '?'):<6} {j['title'][:45]}  (+${c:.4f})")
        time.sleep(0.3)

    results.sort(key=lambda d: -(d["llm"].get("fit_0_100") or 0))
    (OUT / "llm_shortlist.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = ["# Kimi LLM Shortlist", "",
             f"_{len(results)} jobs · model {args.model} · actual cost ${total_cost:.3f}_",
             "", "| Fit | Verdict | Job | Why | Unmet hard reqs | Link |",
             "|-----|---------|-----|-----|-----------------|------|"]
    for r in results:
        v = r["llm"]
        t = (r["title"][:45] + "…") if len(r["title"]) > 45 else r["title"]
        why = v.get("why", "")[:80]
        unmet = "; ".join(v.get("hard_requirements_unmet", [])[:3])[:80]
        lines.append(f"| {v.get('fit_0_100', '?')} | {v.get('verdict', '?')} | {t} "
                     f"({r['company'][:20]}) | {why} | {unmet} | [open]({r['url']}) |")
    (OUT / "llm_shortlist.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nDone: {len(results) - errors} scored, {errors} errors · "
          f"cost ${total_cost:.3f}\nWrote {OUT / 'llm_shortlist.md'}")


if __name__ == "__main__":
    main()
