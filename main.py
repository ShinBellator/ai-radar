"""AI Radar - nightly orchestrator.

Run once (Windows Task Scheduler triggers it at 7am):

    python main.py

Pipeline:
    1. load config, sources, and your two prompt files
    2. fetch every enabled source, normalize, keep last 24h
    3. insert into SQLite, skipping anything already seen (dedup)
    4. pass 1 - cheap triage score on every NEW item
    5. pass 2 - full read + summary for items that cleared the threshold
       (full text pulled with trafilatura here, not during fetch)
    6. print a short report

Every step writes to the DB as it goes, so a crash/rate-limit resumes cleanly.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import trafilatura
import yaml

import db as dbmod
import fetcher
from evaluator import Evaluator, make_provider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a .env file into os.environ.

    Kept dependency-free on purpose. Existing environment variables win
    (`setdefault`), so the shell or Task Scheduler can still override a key
    per-run. Secrets live here, never in config.yaml (which is committed).
    """
    env_file = Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def extract_text(url: str, fallback: str) -> str:
    """Pull readable article text; fall back to the abstract/snippet on failure."""
    if not url:
        return fallback
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(downloaded, include_comments=False, favor_recall=True)
            if text and len(text) > len(fallback):
                return text
    except Exception as exc:  # noqa: BLE001 - extraction is best-effort
        log.debug("extract failed for %s: %s", url, exc)
    return fallback


def run() -> None:
    load_dotenv()  # pull API keys from .env into the environment first
    config = load_yaml("config.yaml")
    resources = load_yaml("resources.yaml")
    preferences = load_text(config["prompts"]["preferences"])
    rubric = load_text(config["prompts"]["rubric"])

    pipe = config["pipeline"]
    settings = {
        "user_agent": config["http"]["user_agent"],
        "reddit_budget_seconds": pipe.get("reddit_budget_minutes", 22) * 60,
    }

    database = dbmod.Database(config["db"]["path"])

    # 1-3. fetch -> dedup -> store
    log.info("Fetching sources...")
    raw_items = fetcher.fetch_all(resources, settings, pipe["lookback_hours"])
    inserted = database.insert_items(raw_items)
    log.info("Inserted %d new items (rest were duplicates).", inserted)

    provider = make_provider(config["llm"])
    log.info("LLM provider: %s", provider.name)
    evaluator = Evaluator(provider, preferences, rubric, pipe["max_text_chars"])
    threshold = pipe["triage_threshold"]
    reject_cap = pipe.get("reject_score_cap", 25)
    delay = pipe["request_delay_seconds"]

    # 4. pass 1 - triage every NEW item
    new_items = database.get_by_status(dbmod.NEW)
    log.info("Pass 1 (triage): %d items...", len(new_items))
    for item in new_items:
        try:
            result = evaluator.triage(item)
            database.set_triage(
                item.id, result["score"], threshold, provider.name, reject_cap
            )
        except Exception as exc:  # noqa: BLE001 - one bad item must not stop the batch
            log.warning("triage failed for #%s (%s): %s", item.id, item.title[:60], exc)
        time.sleep(delay)

    # 5. pass 2 - full evaluation for items that passed triage
    survivors = database.items_for_deep_eval()
    log.info("Pass 2 (deep eval): %d items above threshold %d...", len(survivors), threshold)
    for item in survivors:
        try:
            text = extract_text(item.url, item.raw_text or item.title)
            result = evaluator.evaluate(item, text)
            database.set_evaluation(item.id, result, provider.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("eval failed for #%s (%s): %s", item.id, item.title[:60], exc)
        time.sleep(delay)

    # 6. report
    counts = database.status_counts()
    log.info("Done. Status counts: %s", counts)
    log.info("Open the backlog with:  streamlit run app.py")


if __name__ == "__main__":
    run()
