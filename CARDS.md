# Card format

The authoritative spec for what an SRS card should look like. `srs_cards.format_ver`
records which version a card was last built/refined to. The daily pass
(`_maybe_reformat_imminent` in `app/main.py`) brings the soon-to-be-introduced
cards up to the current version before you ever see them; new cards are refined
right after creation (`_refine_new_card_async` in `_commit_card`).

## v2 (current) — since 2026-09-01

**Front** — unchanged from v1. The dictionary form of the word (or the common
form of a phrase), with stress, per the `card_front` setting ('word' | 'sentence').
`front_word` / `accented` / `dict_accented` are never touched by a reformat.

**Back**, top to bottom:

1. **`translation`** — the ONE word or short phrase to recall. The single
   cleanest translation that fits the vast majority of contexts you'll meet the
   word in. No slashes, no lists, no parenthetical alternatives.
   - Default: the general, most-frequent sense (спор → "argument").
   - Exception (rare): when the word is used here in a marked / idiomatic /
     slang / technical way the general sense wouldn't convey, `translation`
     becomes that narrow sense and `meaning_contextual = 1`. The card shows a
     "· in this context" cue, and `alt_meanings` then **must lead with the
     general meaning**.

2. **`alt_meanings`** — smaller text under the bold. Other senses, concise,
   `'; '`-separated, plus any common set phrase / idiom (written `фраза — meaning`).
   2–5 senses max. Empty only if the word genuinely has one sense.

3. **`sentence`** — the context where the word was found, trimmed to a single
   clause / short sentence (~14 words). Must still contain a form of the target
   word (verified with `anki.front_html`); if the trim can't be verified the
   original sentence is kept. The full original context is preserved in
   `sentence_full` so the trim is reversible.

### Validation (`srs.cards_failing_v2`)

A v2 card is re-run by the daily pass if: `translation` is empty, contains
`/` `;` or ` or `, or is > 40 chars; or `sentence` is > 160 chars.

### Backfill / re-run

- `POST /srs/reformat-cards?limit=&force=` — `force=1` re-runs cards already on v2.
- Batched through `llm.card_meanings` (10 per call).

## v1 (legacy)

Big-bold `translation` was often several senses slash-separated; context was the
raw source sentence/paragraph (reading cards ran 600–2000 chars). Kept in
`sentence_full` after a reformat.
