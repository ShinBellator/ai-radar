"""LLM evaluation for AI Radar.

Two things live here:

1. A provider abstraction so you can switch backends from config with one key:
       - GroqProvider   (primary; free tier, Llama 3.3 70B, fast)
       - OllamaProvider (fallback; local Qwen 3 8B, or Ollama Cloud)
   Both expose `.complete(system, user) -> str` and a `.name`.

2. The Evaluator, which builds prompts from your editable prompt files plus a
   JSON output schema, and runs the two passes:
       - triage():   cheap, title + snippet  -> just a relevance score
       - evaluate(): full text -> score, summary, reasons, tags, read time
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("evaluator")


# =====================================================================
# Providers
# =====================================================================
class GroqProvider:
    def __init__(self, model: str, api_key: str):
        from groq import Groq  # imported lazily so the other backend isn't required

        if not api_key:
            raise RuntimeError("GROQ API key is empty - set it in your environment.")
        self.model = model
        self._client = Groq(api_key=api_key)

    def complete(self, system: str, user: str) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},  # ask Groq for strict JSON
        )
        return resp.choices[0].message.content

    @property
    def name(self) -> str:
        return f"groq:{self.model}"


class OllamaProvider:
    def __init__(self, model: str, host: str, api_key: str | None = None):
        import ollama  # lazy import

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        self.model = model
        self._client = ollama.Client(host=host, headers=headers)

    def complete(self, system: str, user: str) -> str:
        resp = self._client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            format="json",  # ask Ollama for strict JSON
            options={"temperature": 0.2},
        )
        return resp["message"]["content"]

    @property
    def name(self) -> str:
        return f"ollama:{self.model}"


def make_provider(llm_cfg: dict):
    """Build the provider selected by `llm.provider` in config.yaml."""
    which = llm_cfg.get("provider", "groq").lower()
    if which == "groq":
        c = llm_cfg["groq"]
        return GroqProvider(model=c["model"], api_key=os.environ.get(c["api_key_env"], ""))
    if which == "ollama":
        c = llm_cfg["ollama"]
        host = c.get("host", "http://localhost:11434")
        api_key = os.environ.get(c.get("api_key_env", ""), "") or None
        # A remote host (Ollama Cloud) requires a key; without one every call
        # 401s silently. Fail loudly up front instead of mid-batch.
        if api_key is None and not any(h in host for h in ("localhost", "127.0.0.1")):
            raise RuntimeError(
                f"Ollama host {host!r} needs an API key, but env var "
                f"{c.get('api_key_env')!r} is empty. Set it in .env."
            )
        return OllamaProvider(model=c["model"], host=host, api_key=api_key)
    raise ValueError(f"Unknown llm.provider: {which!r} (expected 'groq' or 'ollama')")


# =====================================================================
# Output schemas (kept in code so the prompt files stay about *content*)
# =====================================================================
TRIAGE_SCHEMA = '{"score": <integer 0-100>}'
EVAL_SCHEMA = (
    '{"score": <integer 0-100>, '
    '"summary": "<2-3 sentence what-and-why>", '
    '"reasons": "<one line, addressed to you: why this matches your interests>", '
    '"read_time_minutes": <integer>, '
    '"tags": ["<short topic tag>", "..."]}'
)


def _parse_json(raw: str) -> dict:
    """Tolerant JSON parse: strips code fences, falls back to the first {...}."""
    if not raw:
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
    log.warning("Could not parse LLM JSON: %.120s", raw)
    return {}


class Evaluator:
    def __init__(self, provider, preferences: str, rubric: str, max_text_chars: int = 8000):
        self.provider = provider
        self.preferences = preferences
        self.rubric = rubric
        self.max_text_chars = max_text_chars

    # --- prompt assembly ---------------------------------------------
    def _system(self, schema: str) -> str:
        return (
            "You are a personal research assistant that curates AI content for "
            "one specific reader. Use the reader's preferences and the scoring "
            "rubric below to judge each item.\n\n"
            "## Reader preferences\n"
            f"{self.preferences}\n\n"
            "## Scoring rubric\n"
            f"{self.rubric}\n\n"
            "## Output\n"
            "In any 'summary' and 'reasons' text, address the reader directly "
            "in the second person ('you', 'your'). Never use the first person "
            "('I', 'me', 'my').\n"
            f"Respond with ONLY valid JSON matching this shape: {schema}\n"
            "No markdown, no commentary, no extra keys."
        )

    # --- pass 1: cheap triage ----------------------------------------
    def triage(self, item) -> dict:
        snippet = (item.raw_text or "")[:600]
        user = (
            f"Source: {item.source}\n"
            f"Title: {item.title}\n"
            f"Snippet: {snippet}\n\n"
            "Score this item's relevance only."
        )
        data = _parse_json(self.provider.complete(self._system(TRIAGE_SCHEMA), user))
        return {"score": _clamp_score(data.get("score"))}

    # --- pass 2: full evaluation -------------------------------------
    def evaluate(self, item, full_text: str) -> dict:
        text = (full_text or item.raw_text or item.title)[: self.max_text_chars]
        user = (
            f"Source: {item.source}\n"
            f"Title: {item.title}\n"
            f"URL: {item.url}\n"
            f"Content:\n{text}\n\n"
            "Score it, summarize it, give your one-line reasons, estimate the "
            "read time in minutes, and add a few topic tags."
        )
        data = _parse_json(self.provider.complete(self._system(EVAL_SCHEMA), user))
        tags = data.get("tags", [])
        if not isinstance(tags, list):
            tags = [str(tags)]
        return {
            "score": _clamp_score(data.get("score")),
            "summary": str(data.get("summary", "")).strip(),
            "reasons": str(data.get("reasons", "")).strip(),
            "read_time_minutes": _clamp_int(data.get("read_time_minutes"), lo=1, hi=600),
            "tags_json": json.dumps([str(t) for t in tags][:6]),
        }


def _clamp_score(value) -> int:
    return _clamp_int(value, lo=0, hi=100)


def _clamp_int(value, lo: int, hi: int) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, n))
