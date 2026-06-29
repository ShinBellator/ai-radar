"""SQLite persistence for AI Radar.

Item lifecycle (the `status` column):

    NEW        -> just fetched and stored, not yet judged
    TRIAGED    -> passed pass-1 (cheap title/snippet score >= threshold),
                  waiting for a full read
    REJECTED   -> pass-1 score below threshold (kept for the record, hidden)
    EVALUATED  -> full read done; has summary, reasons, tags, read time
    READ       -> you've read it (from the UI)
    ARCHIVED   -> dismissed from the backlog (from the UI)

Fetching and evaluating are separate stages with the DB in between, so a crash
or rate-limit never makes us re-fetch or pay for the same item twice.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import (
    Integer,
    String,
    Text,
    DateTime,
    create_engine,
    event,
    select,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


# --- status constants -------------------------------------------------
NEW = "NEW"
TRIAGED = "TRIAGED"
REJECTED = "REJECTED"
EVALUATED = "EVALUATED"
READ = "READ"
ARCHIVED = "ARCHIVED"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def content_hash(url: str, title: str) -> str:
    """Stable dedup key. Same URL (across two feeds) collapses to one item."""
    basis = (url or title or "").strip().lower()
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)

    # provenance
    source: Mapped[str] = mapped_column(String(200))
    source_type: Mapped[str] = mapped_column(String(50))
    url: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # content
    title: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    raw_text: Mapped[str] = mapped_column(Text, default="")

    # pipeline state
    status: Mapped[str] = mapped_column(String(20), default=NEW, index=True)

    # evaluation results
    score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    reasons: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str] = mapped_column(Text, default="[]")  # JSON array
    read_time_minutes: Mapped[int] = mapped_column(Integer, default=0)
    model_used: Mapped[str] = mapped_column(String(100), default="")
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Database:
    """Thin wrapper so main.py and app.py stay clean."""

    def __init__(self, path: str):
        self.engine = create_engine(f"sqlite:///{path}", future=True)

        # WAL lets the 7am batch write while Streamlit reads, no lock fights.
        @event.listens_for(self.engine, "connect")
        def _set_wal(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False, future=True)

    # --- ingestion ----------------------------------------------------
    def insert_items(self, raw_items) -> int:
        """Insert normalized items, skipping ones we've already seen.

        `raw_items` is a list of fetcher.RawItem. Returns the count inserted.
        """
        if not raw_items:
            return 0

        # Compute hashes and drop in-batch duplicates first.
        by_hash: dict[str, object] = {}
        for ri in raw_items:
            h = content_hash(ri.url, ri.title)
            by_hash.setdefault(h, ri)

        with self.Session() as s:
            existing = set(
                s.scalars(
                    select(Item.content_hash).where(Item.content_hash.in_(list(by_hash)))
                ).all()
            )
            new = 0
            for h, ri in by_hash.items():
                if h in existing:
                    continue
                s.add(
                    Item(
                        source=ri.source,
                        source_type=ri.source_type,
                        url=ri.url,
                        content_hash=h,
                        title=ri.title or "(untitled)",
                        author=ri.author or "",
                        published_at=ri.published_at,
                        fetched_at=_utcnow(),
                        raw_text=ri.raw_text or "",
                        status=NEW,
                    )
                )
                new += 1
            s.commit()
        return new

    # --- pipeline reads -----------------------------------------------
    def get_by_status(self, status: str) -> list[Item]:
        with self.Session() as s:
            return list(s.scalars(select(Item).where(Item.status == status)).all())

    def items_for_deep_eval(self) -> list[Item]:
        return self.get_by_status(TRIAGED)

    # --- pipeline writes ----------------------------------------------
    def set_triage(
        self,
        item_id: int,
        score: int,
        threshold: int,
        model: str,
        reject_cap: int = 25,
    ) -> None:
        with self.Session() as s:
            item = s.get(Item, item_id)
            if item is None:
                return
            item.model_used = model
            if score >= threshold:
                item.status = TRIAGED
                item.score = int(score)
            else:
                # Rejected: cap to a low ceiling so it reads as clearly "very low".
                item.status = REJECTED
                item.score = min(int(score), reject_cap)
            s.commit()

    def set_evaluation(self, item_id: int, result: dict, model: str) -> None:
        with self.Session() as s:
            item = s.get(Item, item_id)
            if item is None:
                return
            item.score = int(result.get("score", item.score))
            item.summary = result.get("summary", "")
            item.reasons = result.get("reasons", "")
            item.tags = result.get("tags_json", "[]")
            item.read_time_minutes = int(result.get("read_time_minutes", 0) or 0)
            item.model_used = model
            item.status = EVALUATED
            item.evaluated_at = _utcnow()
            s.commit()

    def set_status(self, item_id: int, status: str) -> None:
        with self.Session() as s:
            item = s.get(Item, item_id)
            if item is None:
                return
            item.status = status
            s.commit()

    # --- UI reads -----------------------------------------------------
    def query_items(
        self,
        statuses: list[str],
        sources: list[str] | None = None,
        min_score: int = 0,
        search: str | None = None,
        limit: int = 500,
    ) -> list[Item]:
        with self.Session() as s:
            stmt = select(Item).where(Item.status.in_(statuses), Item.score >= min_score)
            if sources:
                stmt = stmt.where(Item.source.in_(sources))
            if search:
                like = f"%{search}%"
                stmt = stmt.where(Item.title.ilike(like) | Item.summary.ilike(like))
            stmt = stmt.order_by(Item.score.desc(), Item.published_at.desc()).limit(limit)
            return list(s.scalars(stmt).all())

    def distinct_sources(self) -> list[str]:
        with self.Session() as s:
            return list(s.scalars(select(Item.source).distinct().order_by(Item.source)).all())

    def status_counts(self) -> dict[str, int]:
        with self.Session() as s:
            rows = s.execute(select(Item.status, func.count()).group_by(Item.status)).all()
        return {status: count for status, count in rows}
