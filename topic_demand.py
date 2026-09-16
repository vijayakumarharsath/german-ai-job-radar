#!/usr/bin/env python3
"""
Deep topic-demand mining over the radar's collected JD corpus.

Reads jobs.db, keeps relevant student/fulltime roles with real JD text,
counts demand for ~40 topics (basic + emerging: RAG, vector DBs, agents,
fine-tuning, inference serving, MLOps, 3D/sensor fusion, Industry 4.0...),
splits by role segment and track, and writes output/topic_demand_report.md
with a learn-next priority list aligned to the ACTIVE profile (profile.json).
"""

from __future__ import annotations

import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import job_radar as jr  # reuse classify_track, RELEVANT_TITLE_RE, CORE_ML_RE

OUT = BASE / "output"

# ---------------- topic catalogue ----------------
# name -> (stack, [regex variants], resume_credit)  credit: 1 strong/.5 partial/0 none
TOPICS = {
    # --- LLM / GenAI stack ---
    "RAG / retrieval-augmented":  ("LLM/GenAI", [r"\brag\b", r"retrieval[\s-]*augmented"], 0.5),
    "Vector DBs":                 ("LLM/GenAI", [r"vector\s*(database|db|store)|qdrant|pinecone|weaviate|milvus|pgvector|faiss|chroma ?db"], 0.0),
    "LangChain/LlamaIndex":       ("LLM/GenAI", [r"langchain|langgraph|llama\s*index|llamaindex"], 0.5),
    "AI agents / tool calling":   ("LLM/GenAI", [r"agentic|multi[\s-]?agent|\bai[\s-]*agents?\b|agent\s*framework|function\s*calling|tool\s*calling|mcp\b"], 0.5),
    "Fine-tuning / LoRA / PEFT":  ("LLM/GenAI", [r"fine[\s-]*tun|\blora\b|\bpeft\b|adapter[\s-]*(training|tuning)"], 0.5),
    "RLHF / alignment":           ("LLM/GenAI", [r"rlhf|\bdpo\b|preference\s*(optimization|learning)"], 0.0),
    "Quantization / efficiency":  ("LLM/GenAI", [r"quantiz|pruning|distill|gguf|\bawq\b"], 0.0),
    "Local LLM serving":          ("LLM/GenAI", [r"vllm\b|text[\s-]*generation[\s-]*inference|tensorrt[\s-]*llm|ollama|llama\.cpp|openvino|hugging\s*face\s*(hub|inference)"], 0.25),
    "Multimodal / VLM":           ("LLM/GenAI", [r"multimodal|multi[\s-]*modal|vision[\s-]*language|\bvlms?\b"], 0.25),
    "Speech / ASR / TTS":         ("LLM/GenAI", [r"speech|\basr\b|whisper|\btts\b|text[\s-]*to[\s-]*speech|sprachverarbeitung"], 0.5),
    "Prompt engineering":         ("LLM/GenAI", [r"prompt\s*engineer|prompt\s*design"], 1.0),
    "EU AI Act / governance":     ("LLM/GenAI", [r"ai\s*act|ai\s*governance|responsible\s*ai|guardrail"], 0.0),
    # --- MLOps / platform ---
    "MLOps general":              ("MLOps/Cloud", [r"mlops|model\s*(registry|monitoring|deployment)|modellbereitstell|ml\s*operations"], 0.0),
    "Experiment tracking":        ("MLOps/Cloud", [r"mlflow|weights\s*&?\s*biases?|wandb|tensorboard|\bdvc\b"], 0.0),
    "Workflow orchestration":     ("MLOps/Cloud", [r"kubeflow|airflow|dagster|prefect"], 0.0),
    "Cloud ML platforms":         ("MLOps/Cloud", [r"sagemaker|vertex\s*ai|azure\s*ml|databricks"], 0.0),
    "Kubernetes":                 ("MLOps/Cloud", [r"kubernetes|\bk8s\b"], 0.0),
    "CI/CD":                      ("MLOps/Cloud", [r"ci/?cd|continuous\s*(integration|delivery)|github\s*actions|gitlab\s*ci"], 0.25),
    "APIs / microservices":       ("MLOps/Cloud", [r"fastapi|flask|\bgrpc\b|microservice|mikroservice|rest[\s-]*api"], 0.5),
    "Big data (Spark/Kafka)":     ("MLOps/Cloud", [r"\bspark\b|pyspark|kafka|\bflink\b|hadoop"], 0.0),
    "BI (Power BI/Tableau)":      ("MLOps/Cloud", [r"power\s*bi|tableau|looker|qlik"], 0.0),
    "Streamlit/Gradio demos":     ("MLOps/Cloud", [r"streamlit|gradio"], 0.0),
    # --- Robotics / perception ---
    "ROS / ROS 2":                ("Robotics/CV", [r"\bros\s*2?\b"], 1.0),
    "Nav2 / motion planning":     ("Robotics/CV", [r"\bnav2\b|moveit|motion\s*planning|bewegungsplanung|path\s*planning|pfadplanung"], 0.25),
    "SLAM":                       ("Robotics/CV", [r"\bslam\b"], 0.0),
    "Simulation (Gazebo/Isaac…)": ("Robotics/CV", [r"gazebo|isaac\s*(sim|lab)|mujo?co|pybullet|coppeliasim|\bcarla\b|webots"], 0.25),
    "Point clouds / 3D":          ("Robotics/CV", [r"point\s*cloud|\bpcl\b|open3d|3d[\s-]*(reconstruction|perception|vision)|photogrammetrie|photogrammetry|neRF|gaussian\s*splat"], 0.25),
    "Sensor fusion / Kalman":     ("Robotics/CV", [r"sensor\s*fusion|sensorfusion|\bkalman\b|sensorik|\bimus?\b|lidar|livox"], 0.5),
    "Object detection / YOLO":    ("Robotics/CV", [r"object\s*detection|objektdetektion|objekterkennung|\byolo\b|\bdetr\b|\brcnn\b|segment\s*anything"], 0.5),
    "Depth / RGB-D cameras":      ("Robotics/CV", [r"depth\s*camera|rgb[\s-]*d|realsense|stereo[\s-]*kamera|stereo\s*camera|tiefenbild"], 0.5),
    "Edge AI (Jetson/TensorRT)":  ("Robotics/CV", [r"jetson|tensorrt|deepstream|gstreamer|edge[\s-]*(ai|computing|deployment)|embedded[\s-]*ai"], 0.25),
    "AGV / AMR / autonomous":     ("Robotics/CV", [r"\bagvs?\b|\bamrs?\b|autonomous\s*(mobile|driving|vehicle|robot)|fuhrerlos|fahrwerk|drohne|uav|drone"], 0.0),
    "Manipulation / cobots":      ("Robotics/CV", [r"cobot|manipulation|greif|pick\s*(&|and)?\s*place|bin[\s-]*pick|handhabung"], 0.5),
    "Control theory / kinematics":("Robotics/CV", [r"regelungs|control\s*theory|kinematik|kinematic|motion\s*control|regelungstechnik"], 0.0),
    "Functional safety":          ("Robotics/CV", [r"funktionale\s*sicherheit|iso\s*26262|iec\s*61508|\bsafety\b"], 0.0),
    "Industry 4.0 / OPC UA / MES":("Robotics/CV", [r"industrie\s*4\.0|industry\s*4\.0|opc[\s-]*ua|\bmes\b|digital(e[nr]?)?\s*zwillin|digital\s*twin"], 1.0),
    # --- data / fundamentals ---
    "SQL / databases":            ("Data", [r"\bsql\b|postgres|mysql|\betl\b|datenbank"], 1.0),
    "Data pipelines / ETL":       ("Data", [r"data[\s-]*pipeline|datenpipeline|etl\b|\belt\b|datenintegration"], 0.5),
    "Statistics / A-B testing":   ("Data", [r"statisti|a[/\s-]*b[\s-]*test|hypothese|wahrscheinlichkeits"], 1.0),
    "Excel":                      ("Data", [r"\bexcel\b"], 1.0),
    "German language":            ("Basics", [r"\bdeutsch\b(?!land)|deutsch(?:kenntnisse?|sprachig)|deutsch[a-z]*\s+sprache|sprachkenntnisse|\bgerman\b|\bb[123]\b"], 0.5),
    "Agile / Scrum":              ("Basics", [r"agile|scrum|kanban"], 1.0),
}

COMPILED = {k: [re.compile(v, re.I) for v in vs] for k, (_, vs, _) in TOPICS.items()}


def topic_credit(name: str) -> float:
    """YOUR level for a topic: profile.topics wins, else the catalogue default.
    Read live so the web app picks up profile.json edits without a restart."""
    lvl = str(jr.active_profile().get("topics", {}).get(name, "")).lower()
    if lvl in jr.LEVEL_TO_CREDIT:
        return jr.LEVEL_TO_CREDIT[lvl]
    return TOPICS[name][2]

SEGMENTS = {
    "LLM/GenAI & NLP": re.compile(r"\bllms?\b|genai|generative|nlp|natural language|kI-k?oordinator|language model|chatbot|konversations|rag\b", re.I),
    "Computer Vision": re.compile(r"computer vision|bildverarbeitung|image|vision|videoanaly|3d|kamera", re.I),
    "Robotics & Automation": re.compile(r"robot|robotik|autonom|perception|ros\b|manipulation|drohne|agv|amr|mechatronik", re.I),
    "Data Science / Eng": re.compile(r"data science|datascience|datenanaly|data engineer|datenengineer|analytics|business intelligence|datenmanagement|data & ai", re.I),
    "Software / Platform": re.compile(r"software|python|backend|fullstack|full-stack|plattform|entwicklung", re.I),
}


def load_relevant_jobs():
    con = sqlite3.connect(BASE / "jobs.db")
    rows = con.execute(
        "SELECT source, title, company, location, description, date_posted, COALESCE(track,'') "
        "FROM jobs WHERE LENGTH(description) > 800").fetchall()
    con.close()
    keep = []
    for src, title, comp, loc, desc, dp, tr in rows:
        t = tr or classify(title, desc)
        if not t:
            continue
        if not (jr.RELEVANT_TITLE_RE.search(title) or
                sum(1 for p in jr.CORE_ML_RE if p.search(desc)) >= 3):
            continue
        keep.append({"title": title, "company": comp, "location": loc,
                     "desc": desc, "track": t})
    return keep


def classify(title: str, desc: str) -> str | None:
    return jr.classify_track(title, desc)


def seg_of(title: str, desc: str) -> str:
    text = f"{title} {desc[:1500]}"
    for name, rx in SEGMENTS.items():
        if rx.search(text):
            return name
    return "Other"


def main() -> None:
    jobs = load_relevant_jobs()
    print(f"relevant full-JD roles: {len(jobs)} "
          f"(student {sum(1 for j in jobs if j['track']=='student')}, "
          f"fulltime {sum(1 for j in jobs if j['track']=='fulltime')})")
    if not jobs:
        sys.exit("no data")

    demand = {t: 0 for t in TOPICS}
    demand_track = {t: {"student": 0, "fulltime": 0} for t in TOPICS}
    seg_count = defaultdict(int)
    demand_seg = {t: defaultdict(int) for t in TOPICS}
    seg_track = defaultdict(lambda: defaultdict(int))

    for j in jobs:
        seg = seg_of(j["title"], j["desc"])
        seg_count[seg] += 1
        seg_track[seg][j["track"]] += 1
        for t, rxs in COMPILED.items():
            if any(rx.search(j["desc"]) or rx.search(j["title"]) for rx in rxs):
                demand[t] += 1
                demand_track[t][j["track"]] += 1
                demand_seg[t][seg] += 1

    n = len(jobs)
    n_st = sum(1 for j in jobs if j["track"] == "student")
    n_ft = n - n_st
    ger = demand["German language"]

    lines = [
        "# What German companies actually demand right now",
        f"_Mined from the radar's own corpus: **{n} relevant AI/ML/Robotics/Data roles with full JD text** "
        f"({n_st} Werkstudent, {n_ft} Junior/Graduate) · sources: StepStone, Indeed, LinkedIn, Arbeitnow · "
        f"{datetime.now(timezone.utc).date()}_",
        "",
        "## Role segments in the corpus",
        "", "| Segment | Jobs | of which student / junior |", "|---|---|---|",
    ]
    for seg, cnt_s in sorted(seg_count.items(), key=lambda x: -x[1]):
        lines.append(f"| {seg} | {cnt_s} | {seg_track[seg]['student']} / {seg_track[seg]['fulltime']} |")

    stacks = ["LLM/GenAI", "MLOps/Cloud", "Robotics/CV", "Data", "Basics"]
    for stack in stacks:
        tops = sorted(((demand[t], t) for t, (s, _, cr) in TOPICS.items()
                       if s == stack and demand[t] > 0), reverse=True)
        if not tops:
            continue
        lines += ["", f"## {stack} topics", "",
                  "| Topic | Demand | % of JDs | Your level | Learn-priority |",
                  "|-------|--------|----------|------------|----------------|"]
        for cnt_t, t in tops:
            credit = topic_credit(t)
            level = "Strong" if credit >= 0.75 else ("Partial" if credit >= 0.25 else "—")
            prio = ""
            if credit < 0.75:
                tc = demand_track[t]
                ft_share = tc["fulltime"] / n_ft if n_ft else 0
                heat = cnt_t / n
                prio = ("HIGH" if heat >= 0.25 or ft_share >= 0.4 else
                        "medium" if heat >= 0.10 or ft_share >= 0.25 else "low")
            lines.append(f"| {t} | {cnt_t} | {round(100*cnt_t/n)}% | {level} | {prio} |")

    # student vs fulltime divergences
    lines += ["", "## Student vs. Junior market — what changes after graduation", "",
              "| Topic | Werkstudent | Junior/Grad | Signal |", "|-------|-------------|-------------|--------|"]
    diffs = []
    for t in TOPICS:
        st = demand_track[t]["student"] / n_st if n_st else 0
        ft = demand_track[t]["fulltime"] / n_ft if n_ft else 0
        if abs(ft - st) >= 0.12 and demand[t] >= 3:
            diffs.append((t, st, ft))
    diffs.sort(key=lambda x: -(x[2] - x[1]))
    for t, st, ft in diffs[:12]:
        arrow = "⬆ grows" if ft > st else "⬇ shrinks"
        lines.append(f"| {t} | {round(100*st)}% | {round(100*ft)}% | {arrow} |")

    lines += [
        "",
        "## Bottom line — aligned to your active profile",
        "",
        f"- **German** demanded in {round(100*ger/n)}% of all relevant JDs — the #1 filter, confirmed at scale.",
        "- Prioritize learn-next topics marked **HIGH** in the tables above (high demand now or junior-market critical).",
        "- 'Partial' = in your profile but without project evidence: one weekend-project bullet fixes each.",
        "",
        "_Generated by topic_demand.py over jobs.db — re-run after every scrape for a fresh picture._",
    ]
    report = OUT / "topic_demand_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {report}")


if __name__ == "__main__":
    main()
