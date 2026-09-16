"""Render docs/demand_chart.png from a fresh topic_demand_report.md.

Usage:
    python docs/make_demand_chart.py [repo_or_demo_dir] [out.png]

Reads <dir>/output/topic_demand_report.md (run topic_demand.py first),
plots topic demand % as horizontal bars and writes a PNG.
"""
import re
import sys
from pathlib import Path

import matplotlib.pyplot as plt

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "docs" / "demand_chart.png"

report = (ROOT / "output" / "topic_demand_report.md").read_text(encoding="utf-8")

rows: list[tuple[str, int, float]] = []
for line in report.splitlines():
    m = re.match(r"\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|\s*(\d+)%", line)
    if m and line.startswith("| "):
        name, count, pct = m.group(1).strip(), int(m.group(2)), float(m.group(3))
        if pct > 0:
            rows.append((name, count, pct))

corpus = 0
for line in report.splitlines():
    m = re.search(r"\*\*(\d+) relevant", line)
    if m:
        corpus = int(m.group(1))
        break

n_top = 12
rows = sorted(rows, key=lambda r: (-r[2], -r[1]))[:n_top]

primary = {"German language"}
agent_cluster = {
    "AI agents / tool calling",
    "RAG / retrieval-augmented",
    "LangChain/LlamaIndex",
    "Prompt engineering",
}

names = [r[0] for r in reversed(rows)]
vals = [r[2] for r in reversed(rows)]
colors = []
for name in names:
    if name in primary:
        colors.append("#d53e4f")
    elif name in agent_cluster:
        colors.append("#1f6feb")
    else:
        colors.append("#9aa0a6")

agent_share = sum(c for n, c, _ in rows if n in agent_cluster)
sub = f"corpus: {corpus} relevant AI/ML/Data/Robotics JDs with full text  ·  agentic-AI cluster ≈ {round(agent_share / max(corpus, 1) * 100):.0f}% of roles"

fig, ax = plt.subplots(figsize=(10, 6.6), dpi=170)
bars = ax.barh(names, vals, color=colors, height=0.72)
for bar, v in zip(bars, vals):
    ax.text(bar.get_width() + 1.2, bar.get_y() + bar.get_height() / 2,
            f"{int(v)}%", va="center", ha="left", fontsize=10, color="#3c4043")

ax.set_xlim(0, max(vals) * 1.18)
ax.set_xlabel("share of relevant JDs mentioning the skill")
fig.text(0.5, 0.988, "What German companies demand right now",
         ha="center", va="top", fontsize=15, fontweight="bold")
fig.text(0.5, 0.953, sub, ha="center", va="top", fontsize=9.5, color="#5f6368")
ax.tick_params(axis="y", labelsize=10)
ax.spines[["top", "right"]].set_visible(False)
ax.spines["left"].set_color("#c9ccd1")
ax.spines["bottom"].set_color("#c9ccd1")
ax.grid(axis="x", color="#e8eaed", linewidth=0.8)
ax.set_axisbelow(True)
fig.tight_layout(rect=(0, 0, 1, 0.90))
OUT.parent.mkdir(parents=True, exist_ok=True)
fig.savefig(OUT, facecolor="white")
print(f"wrote {OUT}  ({OUT.stat().st_size} bytes, corpus={corpus}, bars={n_top})")