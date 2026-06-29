"""Source adapters for AI Radar.

Every adapter returns a list of `RawItem` (one normalized shape), so the rest
of the app never cares whether something came from an API, RSS, or a scrape.

Add a new RSS feed  -> just add a line to resources.yaml (type: rss).
Add a new API source -> write one function and register it in FETCHERS.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import mktime, monotonic, sleep

import feedparser
import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

log = logging.getLogger("fetcher")

HTTP_TIMEOUT = 20.0

# Reddit's public RSS needs no auth, but it's rate-limited: a single request can
# come back with x-ratelimit-remaining: 0 and a reset of ~40s. We pace by those
# headers and retry 429s so every subreddit gets covered, while a per-run time
# budget (set from config) caps the whole crawl so it can't blow past ~30 min.
_REDDIT_MAX_WAIT = 60.0   # cap on any single rate-limit sleep
_REDDIT_RETRIES = 2       # extra 429 attempts per subreddit before skipping it
_reddit_next_ok = 0.0     # monotonic time we may hit Reddit again
_reddit_deadline = 0.0    # monotonic time the crawl must stop waiting


def _reddit_budget_left() -> float:
    return _reddit_deadline - monotonic()


def _reddit_wait() -> None:
    """Wait out the reported rate-limit window, but never past the run budget."""
    wait = min(_reddit_next_ok - monotonic(), _REDDIT_MAX_WAIT, _reddit_budget_left())
    if wait > 0:
        sleep(wait)


def _reddit_note_headers(headers) -> None:
    """Schedule the next allowed call from Reddit's x-ratelimit-* headers."""
    global _reddit_next_ok
    try:
        remaining = float(headers.get("x-ratelimit-remaining", "1"))
        reset = float(headers.get("x-ratelimit-reset", "0"))
    except (TypeError, ValueError):
        remaining, reset = 1.0, 0.0
    # Out of budget -> wait the reset (+3s so it's truly refilled); else go now.
    delay = min(reset + 3.0, _REDDIT_MAX_WAIT) if remaining < 1 else 0.0
    _reddit_next_ok = monotonic() + delay


@dataclass
class RawItem:
    source: str
    source_type: str
    url: str
    title: str
    author: str = ""
    published_at: datetime | None = None
    raw_text: str = ""


# --- date helpers -----------------------------------------------------
def _from_struct(struct_time) -> datetime | None:
    if not struct_time:
        return None
    return datetime.fromtimestamp(mktime(struct_time), tz=timezone.utc)


def _parse_date(value) -> datetime | None:
    """Best-effort parse of whatever a feed/API hands us, normalized to UTC."""
    if value is None:
        return None
    if isinstance(value, (int, float)):  # epoch seconds (reddit, HN)
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        dt = dateparser.parse(str(value))
    except (ValueError, OverflowError):
        return None
    if dt is None:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# =====================================================================
# Adapters
# =====================================================================
def fetch_rss(cfg: dict, settings: dict) -> list[RawItem]:
    """Generic RSS/Atom feed (blogs, Substack, Zenn/Qiita...)."""
    feed = feedparser.parse(cfg["url"])
    items: list[RawItem] = []
    for e in feed.entries:
        published = _from_struct(
            getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
        )
        items.append(
            RawItem(
                source=cfg["name"],
                source_type="rss",
                url=e.get("link", ""),
                title=e.get("title", ""),
                author=e.get("author", ""),
                published_at=published,
                raw_text=e.get("summary", ""),
            )
        )
    return items


def fetch_arxiv(cfg: dict, settings: dict) -> list[RawItem]:
    """arXiv Atom API, newest first, filtered by category."""
    cats = " OR ".join(f"cat:{c}" for c in cfg.get("categories", ["cs.AI"]))
    params = {
        "search_query": cats,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": cfg.get("max_results", 50),
    }
    # https + follow_redirects: arXiv now 301s the old http:// API endpoint.
    r = httpx.get(
        "https://export.arxiv.org/api/query",
        params=params,
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
    )
    r.raise_for_status()
    feed = feedparser.parse(r.text)  # arXiv returns Atom; feedparser handles it
    items: list[RawItem] = []
    for e in feed.entries:
        authors = ", ".join(a.name for a in getattr(e, "authors", [])) if hasattr(e, "authors") else ""
        items.append(
            RawItem(
                source=cfg["name"],
                source_type="arxiv",
                url=e.get("link", e.get("id", "")),
                title=e.get("title", "").replace("\n", " ").strip(),
                author=authors,
                published_at=_from_struct(getattr(e, "published_parsed", None)),
                raw_text=e.get("summary", ""),  # the abstract
            )
        )
    return items


def fetch_huggingface(cfg: dict, settings: dict) -> list[RawItem]:
    """Hugging Face daily papers via the public JSON API."""
    r = httpx.get(
        "https://huggingface.co/api/daily_papers",
        params={"limit": cfg.get("max_results", 50)},
        headers={"User-Agent": settings["user_agent"]},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    items: list[RawItem] = []
    for row in r.json():
        paper = row.get("paper", {}) or {}
        arxiv_id = paper.get("id", "")
        items.append(
            RawItem(
                source=cfg["name"],
                source_type="huggingface",
                url=f"https://huggingface.co/papers/{arxiv_id}" if arxiv_id else "",
                title=paper.get("title", row.get("title", "")),
                author=", ".join(a.get("name", "") for a in paper.get("authors", []) or []),
                published_at=_parse_date(row.get("publishedAt") or paper.get("publishedAt")),
                raw_text=paper.get("summary", ""),
            )
        )
    return items


def fetch_hackernews(cfg: dict, settings: dict) -> list[RawItem]:
    """Hacker News stories via the Algolia API, by keyword + min points."""
    items: list[RawItem] = []
    min_points = cfg.get("min_points", 0)
    for kw in cfg.get("keywords", ["AI"]):
        params = {
            "query": kw,
            "tags": "story",
            "numericFilters": f"points>={min_points}",
            "hitsPerPage": cfg.get("per_keyword", 20),
        }
        r = httpx.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params=params,
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        for hit in r.json().get("hits", []):
            obj_id = hit.get("objectID", "")
            items.append(
                RawItem(
                    source=cfg["name"],
                    source_type="hackernews",
                    url=hit.get("url") or f"https://news.ycombinator.com/item?id={obj_id}",
                    title=hit.get("title", ""),
                    author=hit.get("author", ""),
                    published_at=_parse_date(hit.get("created_at_i")),
                    raw_text=hit.get("story_text", "") or "",
                )
            )
    return items


def fetch_reddit(cfg: dict, settings: dict) -> list[RawItem]:
    """A subreddit's Atom feed at /r/<sub>/<listing>/.rss.

    Reddit's priced API now needs pre-approval that personal scripts can't get,
    and the unauthenticated `.json` endpoint returns 403. The public RSS feeds
    still work with no auth - but they're rate-limited, so we pace by the
    x-ratelimit-* headers and retry 429s (coverage first). A per-run budget
    (settings['reddit_budget_seconds']) stops the crawl waiting once it's spent,
    so any leftover subs simply skip and get caught next run via dedup.
    feedparser fetches with its own (blocked) UA, so we pull with httpx first.
    """
    global _reddit_deadline
    if _reddit_deadline == 0.0:  # first Reddit call this run: start the clock
        budget = settings.get("reddit_budget_seconds", 1320)
        _reddit_deadline = monotonic() + budget

    sub = cfg["subreddit"]
    listing = cfg.get("listing", "new")
    url = f"https://www.reddit.com/r/{sub}/{listing}/.rss"
    user_agent = settings["user_agent"]

    r = None
    for attempt in range(_REDDIT_RETRIES + 1):
        _reddit_wait()
        r = httpx.get(
            url,
            headers={"User-Agent": user_agent},
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
        )
        _reddit_note_headers(r.headers)
        # Stop if it worked, or we're out of retries / out of time budget.
        out_of_time = _reddit_budget_left() <= 0
        if r.status_code != 429 or attempt == _REDDIT_RETRIES or out_of_time:
            break

    r.raise_for_status()  # a leftover 429/other error -> fetch_all skips this sub

    feed = feedparser.parse(r.text)  # Reddit returns Atom; feedparser handles it
    items: list[RawItem] = []
    for e in feed.entries:
        published = _from_struct(
            getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
        )
        items.append(
            RawItem(
                source=cfg["name"],
                source_type="reddit",
                url=e.get("link", ""),
                title=e.get("title", ""),
                author=e.get("author", "").removeprefix("/u/"),
                published_at=published,
                raw_text=e.get("summary", ""),  # post body as HTML
            )
        )
    return items


def fetch_github_trending(cfg: dict, settings: dict) -> list[RawItem]:
    """GitHub Trending - scraped (no stable official API).

    Trending has no per-repo date, so we stamp items with 'now' (they're
    trending today by definition) which keeps them inside the 24h window.
    """
    items: list[RawItem] = []
    for spec in cfg.get("specs", [{"language": "", "since": "daily"}]):
        lang = spec.get("language", "")
        path = f"/trending/{lang}" if lang else "/trending"
        r = httpx.get(
            f"https://github.com{path}",
            params={"since": spec.get("since", "daily")},
            headers={"User-Agent": settings["user_agent"]},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for row in soup.select("article.Box-row"):
            a = row.select_one("h2 a")
            if not a:
                continue
            repo = " ".join(a.get_text().split())  # "owner / name"
            href = "https://github.com" + a.get("href", "")
            desc_el = row.select_one("p")
            desc = desc_el.get_text(strip=True) if desc_el else ""
            items.append(
                RawItem(
                    source=cfg["name"],
                    source_type="github_trending",
                    url=href,
                    title=repo,
                    author=repo.split("/")[0].strip(),
                    published_at=_now(),
                    raw_text=f"[{lang or 'all'} / {spec.get('since', 'daily')}] {desc}",
                )
            )
    return items


# Map resources.yaml `type` -> adapter function.
FETCHERS = {
    "rss": fetch_rss,
    "arxiv": fetch_arxiv,
    "huggingface": fetch_huggingface,
    "hackernews": fetch_hackernews,
    "reddit": fetch_reddit,
    "github_trending": fetch_github_trending,
}


# =====================================================================
# Orchestration
# =====================================================================
def fetch_all(resources: dict, settings: dict, lookback_hours: int) -> list[RawItem]:
    """Run every enabled source, normalize, then keep only the last N hours.

    Each source is isolated: a failure logs a warning and is skipped so the
    rest of the run still completes.
    """
    collected: list[RawItem] = []
    for src in resources.get("sources", []):
        if not src.get("enabled", True):
            continue
        adapter = FETCHERS.get(src.get("type"))
        if adapter is None:
            log.warning("Unknown source type %r for %r - skipping", src.get("type"), src.get("name"))
            continue
        try:
            got = adapter(src, settings)
            log.info("  %-28s %3d items", src.get("name", "?"), len(got))
            collected.extend(got)
        except Exception as exc:  # noqa: BLE001 - one bad source must not kill the run
            log.warning("  %-28s FAILED: %s", src.get("name", "?"), exc)

    fresh = _within_lookback(collected, lookback_hours)
    log.info("Fetched %d items, %d within last %dh", len(collected), len(fresh), lookback_hours)
    return fresh


def _within_lookback(items: list[RawItem], lookback_hours: int) -> list[RawItem]:
    cutoff = _now() - timedelta(hours=lookback_hours)
    kept = []
    for it in items:
        # No date -> treat as fresh rather than silently dropping it.
        if it.published_at is None or it.published_at >= cutoff:
            kept.append(it)
    return kept
