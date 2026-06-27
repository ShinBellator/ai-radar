# Scoring rubric

> This file describes **how to judge** an item, separately from your interests.
> The model returns a relevance score from 0–100. Tune the bands below to make
> the backlog stricter or looser.

Score each item from 0 to 100 for how worth-my-time it is, given the reader
preferences. Judge the *content itself*, not how popular it is.

## Score bands
- **90–100 — Must read.** Directly in my core interests, novel and substantive
  (e.g. a strong new open model, a genuinely new method, a deep practitioner
  write-up). I'd be annoyed to miss it.
- **70–89 — Worth reading.** Solidly relevant and has real technical content,
  even if not groundbreaking.
- **50–69 — Maybe.** Related and somewhat useful, but incremental, narrow, or
  thin on detail. Borderline.
- **30–49 — Probably skip.** Tangential, shallow, or mostly news/opinion with
  little technical substance.
- **0–29 — Skip.** Off-topic, hype, marketing, beginner content, or low-effort.

## Guidelines
- Reward novelty, technical depth, and credible authorship.
- Penalize hype, vagueness, marketing, and duplicate/derivative coverage.
- A paper is not automatically high-scoring — incremental papers can be a 50.
- For Reddit/forum posts, reward substantive discussion or releases; penalize
  help threads, memes, and shopping questions.
- When the provided text is too thin to judge confidently, lean lower and keep
  the summary honest about the uncertainty.

## Summary instructions (used only for items that pass triage)
- Write 2–3 sentences. First say **what** the content is about, then **why it
  may matter to me** specifically.
- Be concrete and neutral. No hype words, no "this groundbreaking...".
- If it's a paper, mention the core contribution. If it's a release, mention
  what's new and notable.

## Estimated read time
- Also estimate how long it would take me to read/absorb the item, in **whole
  minutes** (`read_time_minutes`).
- Base it on the length and density of the content: a short blog post or release
  note might be 3–5 min; a long engineering deep-dive 15–25 min; a dense paper
  read properly 30–60 min (estimate the real read, not just skimming the abstract).
- If the provided text is only an abstract/snippet, infer a sensible estimate for
  the full piece rather than for the snippet, and don't go below 1.
