# 🇩🇪 German AI Job Radar

**I stopped guessing what the German AI job market wants and measured it instead.**
This tool scrapes ~2,000 live German job listings, extracts the full job descriptions,
scores every relevant AI/ML/Data/Robotics role against a resume keyword profile, and
mines the corpus for skill-demand statistics.

![demand chart](docs/demand_chart.png)

> Headline finding (corpus of ~2,000 listings, Sept 2026): **German is demanded
> explicitly in ~70% of relevant AI student roles with full JD text — and 86% of
> junior roles.** The fastest-growing skill cluster for AI student roles is
> agentic AI (agents/tool-calling 13%, LangChain 11%, RAG 9%). Full methodology below.

## What it does

```
┌─────────────┐   ┌──────────────┐   ┌───────────────┐   ┌──────────────┐
│  4 sources  │ → │  SQLite DB   │ → │ resume-profile │ → │ ranked CSVs  │
│ StepStone   │   │ dedup by URL │   │ scoring (43    │   │ + market     │
│ Indeed/LinkedIn │ │ (title+co in │  │ skills, weighted) │ │ reports +   │
│ Arbeitnow API│  │  fallback)   │   │ + track split  │   │ topic mining │
│ saved HTML  │   └──────────────┘   └───────────────┘   └──────────────┘
└─────────────┘
```

- **Scrapers** — StepStone (plain `requests`, JSON-LD detail parsing), Indeed/LinkedIn
  via [`python-jobspy`](https://github.com/serpapi/python-jobspy), Arbeitnow's free
  API, and a browser-saved-HTML fallback for blocked IPs
- **Scoring** — every JD is matched against a weighted 43-skill resume profile
  (Strong / Partial / Missing per skill) → per-job fit score + "what's missing for me"
- **Two tracks** — `student` (Werkstudent/Praktikum) vs `fulltime` (Junior/Graduate),
  classified from titles + entry-level JD signals; senior roles excluded
- **Topic mining** — 40+ topic demand counts (RAG, vector DBs, ROS 2, edge AI,
  German…) split by segment and by track
- **Dual-track employers** — companies hiring both students *and* juniors
  (= Werkstudent → Übernahme pipelines)

## Quick start

```bash
pip install -r requirements.txt
python job_radar.py                              # all sources, both tracks
python job_radar.py --no-scrape                  # rescore stored jobs only
python job_radar.py --sources stepstone,fromhtml # subset of sources
python job_radar.py --no-scrape --enrich-stepstone  # upgrade teaser JDs to full text

python topic_demand.py        # skill-demand mining over the collected corpus
python cleanup_db.py          # collapse duplicate listings across sources (maintenance)
```

Outputs land in `output/`: ranked CSVs per track, market reports per track,
`topic_demand_report.md`.

Optional LLM deep-scoring (Kimi/Moonshot API, ~$0.03 per 30 jobs):

```bash
export MOONSHOT_API_KEY="sk-..."
python kimi_shortlist.py
```

## Repo layout

| File | Purpose |
|------|---------|
| `job_radar.py` | main pipeline: scrape → dedup → score → report |
| `stepstone.py` | StepStone search + JSON-LD detail scraper (verified 2026 markup) |
| `indeed_direct.py` | Indeed direct requests + saved-HTML parser (Cloudflare-aware) |
| `topic_demand.py` | topic/skill demand mining over the JD corpus |
| `kimi_shortlist.py` | optional LLM second-pass scoring |
| `cleanup_db.py` | one-time migration: rebuild canonical URL-hash keys, merge dupes |
| `data/sample_jobs.csv` | 10-row sample of the schema |

## Fair-use & legal notes

- Built for **personal, low-volume job hunting**. Sources are hit politely
  (delays, per-run caps). StepStone/Indeed/LinkedIn ToS generally prohibit
  scraping — don't run this hourly, don't republish scraped data, don't use it
  commercially.
- The DB (`jobs.db`) is **not** included — run your own scrape in minutes.

## Built by

[Harsath Vijayakumar](https://www.linkedin.com/in/vijayakumarharsath) — M.Sc.
Artificial Intelligence & Robotics student (Hof University, Germany), hunting
Werkstudent roles and building in public. Findings thread:
[LinkedIn post](#) · [GitHub profile](https://github.com/vijayakumarharsath)
