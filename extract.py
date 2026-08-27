"""Per-video vocabulary extraction: ONE Claude call over the cleaned transcript.

This is the step the project brief wants to be a cheap, self-contained model call
(not an agent reasoning live in a chat). It reads the stored transcript, asks the
model for candidate words/phrases + contextual translations as JSON, then hands
the result to db.add_candidates (which applies the stoplist / known_lexicon
filter and dedup). Review + export stay separate.

Usage:  python extract.py <video_id> [--model claude-sonnet-5] [--dry-run]

Auth: standard Anthropic SDK resolution (ANTHROPIC_API_KEY or `ant auth login`).
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile

import anthropic

import db
from subs import transcript_text

MODEL = os.environ.get("RU_EXTRACT_MODEL", "claude-sonnet-5")

# $ per 1M tokens (input, output). Update from the claude-api skill if models change.
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

SYSTEM = """\
You are a Russian vocabulary extraction engine for a single intermediate-to-advanced learner.

You are given the auto-generated (ASR) transcript of a Russian YouTube video, as
timestamped lines. Work through it and select every word OR short phrase that a
learner at that level would plausibly need to look up.

Rules:
- Decide the span yourself. Keep collocations and set phrases whole ("как раз",
  "по большому счёту", "вам шашечки или ехать") rather than decomposing them.
  Set is_phrase=true for multi-word spans.
- Do NOT flag words that are among the ~2000 most common Russian words. When in
  doubt about something very common, skip it.
- The transcript is ASR output and contains errors. If a "word" looks like a
  transcription error rather than real vocabulary, still include it but put
  "(SUSPECT ASR)" at the start of its translation.
- sentence: the single source sentence the span occurs in, lightly cleaned of
  obvious ASR glitches so it reads naturally. Keep it short.
- span_text: the citation form is fine; it does not have to match the inflected
  form in the sentence.
- translation: the best contextual English gloss - one word or a short phrase,
  or "a / b" if genuinely ambiguous. For idioms, gloss the meaning.
- timestamp_start: the "HH:MM:SS" of the line where the span appears.

Output ONLY a JSON array, no prose, no code fence. Each element:
{"span_text","is_phrase","sentence","timestamp_start","translation"}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video_id", type=int)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--dry-run", action="store_true", help="print candidates, don't write to DB")
    args = ap.parse_args()

    c = sqlite3.connect(db.DB)
    row = c.execute("SELECT raw_subs, title FROM videos WHERE id=?", (args.video_id,)).fetchone()
    if not row:
        sys.exit(f"no video {args.video_id} - run fetch_subs.py first")
    raw, title = row
    transcript = transcript_text(raw)

    client = anthropic.Anthropic()
    with client.messages.stream(
        model=args.model,
        max_tokens=32000,
        system=SYSTEM,
        messages=[{"role": "user", "content": f"Video: {title}\n\n{transcript}"}],
    ) as stream:
        msg = stream.get_final_message()

    text = "".join(b.text for b in msg.content if b.type == "text")
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        sys.exit(f"model did not return a JSON array:\n{text[:500]}")
    items = json.loads(m.group(0))

    u = msg.usage
    pin, pout = PRICES.get(args.model, (None, None))
    cost = ""
    if pin:
        cost = f"  ~${u.input_tokens / 1e6 * pin + u.output_tokens / 1e6 * pout:.4f}"
    print(f"[{args.model}] in={u.input_tokens} out={u.output_tokens}{cost}")
    print(f"model proposed {len(items)} candidates")

    if args.dry_run:
        print(json.dumps(items, ensure_ascii=False, indent=1))
        return

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)
        path = f.name
    db.add_candidates(args.video_id, path)
    os.unlink(path)


if __name__ == "__main__":
    main()
