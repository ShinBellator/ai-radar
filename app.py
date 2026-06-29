"""AI Radar - backlog UI.

Run it:  streamlit run app.py

Read-mostly view over the same SQLite DB the nightly job writes to. Shows the
EVALUATED backlog sorted by score, with filters and per-item actions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import streamlit as st
import yaml

import db as dbmod

st.set_page_config(page_title="AI Radar", page_icon="📡", layout="wide")


@st.cache_resource
def get_db() -> dbmod.Database:
    with open("config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return dbmod.Database(config["db"]["path"])


database = get_db()


def score_color(score: int) -> str:
    if score >= 90:
        return "🟢"
    if score >= 70:
        return "🔵"
    if score >= 50:
        return "🟡"
    return "⚪"


def humanize_age(dt: datetime | None) -> str:
    if dt is None:
        return "unknown date"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return "just now"
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{int(hours // 24)}d ago"


# --- header + metrics -------------------------------------------------
st.title("📡 AI Radar")
counts = database.status_counts()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Backlog", counts.get(dbmod.EVALUATED, 0))
c2.metric("Read", counts.get(dbmod.READ, 0))
c3.metric("Archived", counts.get(dbmod.ARCHIVED, 0))
c4.metric("Rejected (low score)", counts.get(dbmod.REJECTED, 0))

# --- sidebar filters --------------------------------------------------
st.sidebar.header("Filters")
view = st.sidebar.radio(
    "Show",
    ["Backlog", "Read", "Archived", "All evaluated", "Rejected"],
    index=0,
)
status_map = {
    "Backlog": [dbmod.EVALUATED],
    "Read": [dbmod.READ],
    "Archived": [dbmod.ARCHIVED],
    "All evaluated": [dbmod.EVALUATED, dbmod.READ, dbmod.ARCHIVED],
    "Rejected": [dbmod.REJECTED],
}
statuses = status_map[view]

all_sources = database.distinct_sources()
chosen_sources = st.sidebar.multiselect("Sources", all_sources, default=[])
min_score = st.sidebar.slider("Minimum score", 0, 100, 75, step=5)
search = st.sidebar.text_input("Search title / summary")

if st.sidebar.button("🔄 Refresh"):
    st.rerun()

# --- results ----------------------------------------------------------
# Rejected items are low-scored by definition, so the score filter would hide
# them; ignore it when you're explicitly inspecting the rejected pile.
effective_min = 0 if view == "Rejected" else min_score
items = database.query_items(
    statuses=statuses,
    sources=chosen_sources or None,
    min_score=effective_min,
    search=search or None,
)

st.caption(f"{len(items)} item(s)")

for item in items:
    try:
        tags = json.loads(item.tags or "[]")
    except json.JSONDecodeError:
        tags = []

    read_time = f"~{item.read_time_minutes} min read" if item.read_time_minutes else ""
    header = f"{score_color(item.score)} **{item.score}** · {item.title}"

    with st.container(border=True):
        left, right = st.columns([0.82, 0.18])
        with left:
            st.markdown(header)
            meta = " · ".join(
                p for p in [item.source, humanize_age(item.published_at), read_time] if p
            )
            st.caption(meta)
            if item.reasons:
                st.markdown(f"*Why:* {item.reasons}")
            if item.summary:
                st.write(item.summary)
            tagline = " ".join(f"`{t}`" for t in tags)
            line = " · ".join(p for p in [tagline] if p)
            if line:
                st.markdown(line)
            if item.url:
                st.markdown(f"[Open original ↗]({item.url})")
        with right:
            if item.status != dbmod.READ:
                if st.button("✓ Mark read", key=f"read-{item.id}", use_container_width=True):
                    database.set_status(item.id, dbmod.READ)
                    st.rerun()
            if item.status != dbmod.ARCHIVED:
                if st.button("🗄 Archive", key=f"arch-{item.id}", use_container_width=True):
                    database.set_status(item.id, dbmod.ARCHIVED)
                    st.rerun()
            if item.status in (dbmod.READ, dbmod.ARCHIVED):
                if st.button("↩ Back to backlog", key=f"back-{item.id}", use_container_width=True):
                    database.set_status(item.id, dbmod.EVALUATED)
                    st.rerun()

if not items:
    st.info("Nothing here yet. Run `python main.py` to fill the backlog.")
