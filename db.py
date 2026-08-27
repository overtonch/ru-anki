"""Mechanical DB helpers for the Russian vocab pipeline.

Claude does the extraction/translation/review reasoning; this script only moves
data in and out of SQLite and builds the .apkg.

Subcommands:
  add-candidates <video_id> <candidates.json>
      json: [{"span_text","is_phrase","sentence","timestamp_start","translation"}]
  filter <video_id>
      print candidates whose normalized form is in stoplist or known_lexicon
      (informational; add-candidates already skips them)
  batch <video_id>
      print the pending review batch, numbered
  decide <video_id> <decisions.json>
      json: {"known":[n,...], "garbage":[n,...], "retranslate":{"n":"new text"}}
      n = the number shown by `batch`. Everything not listed becomes
      confirmed_unknown. known -> also added to known_lexicon.
  export <video_id> <out.apkg>
      build an Anki deck from this video's confirmed_unknown candidates
      and mark them exported.
"""
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("VOCAB_DB", os.path.join(HERE, "vocab.db"))


def norm(w: str) -> str:
    return w.strip().lower().replace("ё", "е")


_MORPH = None


def _morph():
    global _MORPH
    if _MORPH is None:
        import pymorphy3
        _MORPH = pymorphy3.MorphAnalyzer()
    return _MORPH


def lemma_key(span: str) -> str:
    """Lookup key for stoplist / known_lexicon.

    Single word -> its pymorphy3 lemma (so an inflected candidate matches the
    lemma frequency list and a confirmed 'known' word covers all its forms).
    Multi-word phrase -> the ё-folded lowercased phrase (phrases are never in
    the lemma stoplist, so they always pass the filter)."""
    s = norm(span)
    toks = [t for t in re.split(r"[^а-я-]+", s) if t]
    if len(toks) == 1:
        return norm(_morph().parse(toks[0])[0].normal_form)
    return s


_WORD = None


def _tokens(text):
    global _WORD
    if _WORD is None:
        import re as _re
        _WORD = _re.compile(r"[А-Яа-яЁёA-Za-z-]+")
    return [(m.start(), m.end()) for m in _WORD.finditer(text)]


_VOW = "аяеёиыоуюэ"


def _stem(w):
    """Crude Russian stemmer: strip reflexive + infinitive markers and trailing
    inflection so a citation form and an inflected form share a prefix."""
    w = norm(w)
    for suf in ("ся", "сь"):
        if w.endswith(suf) and len(w) > 4:
            w = w[: -len(suf)]
    for suf in ("ться", " '", "ть", "ти", "чь"):
        if w.endswith(suf) and len(w) > 4:
            w = w[: -len(suf)]
            break
    while len(w) > 3 and w[-1] in _VOW + "йь":
        w = w[:-1]
    return w[:3] if len(w) < 3 else w


def bold(sentence, span, is_phrase, tag="**"):
    """Wrap the occurrence of `span` (or its inflected form) in `sentence`.

    Russian inflects, so an exact substring match usually fails. Fall back to
    matching on a stem prefix: for a single word, bold the sentence token that
    shares the span's stem; for a phrase, bold from the first such token to the
    last. If nothing matches, return the sentence unchanged.
    """
    toks = _tokens(sentence)

    def wrap(a, b):
        # snap [a, b) out to whole-word boundaries so we never bold a bare root
        for ta, tb in toks:
            if ta < b and tb > a:
                a, b = min(a, ta), max(b, tb)
        return f"{sentence[:a]}{tag}{sentence[a:b]}{tag}{sentence[b:]}"

    low_s, low_span = norm(sentence), norm(span)
    i = low_s.find(low_span)
    if i >= 0:
        return wrap(i, i + len(span))

    def hit(tok, stem):
        if len(stem) < 3:
            return False
        t = norm(tok)
        lcp = 0
        for x, y in zip(t, stem):
            if x != y:
                break
            lcp += 1
        return lcp >= (len(stem) if len(stem) <= 4 else 5)

    if not is_phrase:
        st = _stem(span)
        for a, b in toks:
            if hit(sentence[a:b], st):
                return wrap(a, b)
        return sentence

    # Phrase: stem every content word of the span (>= 3 chars, so we skip
    # prepositions / particles like за, в, не), then find sentence tokens that
    # match any of them. Bold from the first to the last such token — but only
    # if at least two matched and they sit close together, so a single weak
    # match or two scattered ones never produces a misleading bold. Anything
    # that fails this is treated as "not present" and auto-discarded upstream.
    stems = [s for s in (_stem(w) for w in span.split()) if len(s) >= 3]
    matched = [(a, b) for a, b in toks if any(hit(sentence[a:b], s) for s in stems)]
    if len(matched) >= 2:
        span_toks = sum(1 for a, b in toks if matched[0][0] <= a and b <= matched[-1][1])
        if span_toks <= max(len(stems) + 2, 5):
            return wrap(matched[0][0], matched[-1][1])
    return sentence


def con():
    c = sqlite3.connect(DB)
    c.execute("PRAGMA foreign_keys=ON")
    return c


def excluded(c, n):
    if c.execute("SELECT 1 FROM stoplist WHERE normalized_text=?", (n,)).fetchone():
        return "stoplist"
    if c.execute("SELECT 1 FROM known_lexicon WHERE normalized_text=?", (n,)).fetchone():
        return "known_lexicon"
    return None


def add_candidates(video_id, path):
    items = json.load(open(path, encoding="utf-8"))
    c = con()
    added, skipped = [], []
    for it in items:
        span = it["span_text"].strip()
        n = lemma_key(span)
        why = excluded(c, n)
        if why:
            skipped.append((span, why))
            continue
        if c.execute(
            "SELECT 1 FROM candidates WHERE video_id=? AND normalized_text=?",
            (video_id, n),
        ).fetchone():
            skipped.append((span, "dup-in-video"))
            continue
        c.execute(
            """INSERT INTO candidates(video_id, span_text, normalized_text, is_phrase,
                 sentence, timestamp_start, translation, status)
               VALUES(?,?,?,?,?,?,?, 'pending')""",
            (video_id, span, n, int(it.get("is_phrase", 0)), it["sentence"],
             it.get("timestamp_start"), it.get("translation")),
        )
        added.append(span)
    c.commit()
    print(f"added {len(added)} candidates")
    for s, w in skipped:
        print(f"  skipped: {s}  ({w})")
    c.close()


def batch(video_id):
    c = con()
    rows = c.execute(
        """SELECT id, span_text, is_phrase, sentence, translation
           FROM candidates WHERE video_id=? AND status='pending' ORDER BY id""",
        (video_id,),
    ).fetchall()
    for i, (cid, span, isph, sent, tr) in enumerate(rows, 1):
        tag = "PHRASE" if isph else "word"
        bolded = bold(sent, span, isph, "**")
        print(f"{i}. [{tag}] {span} -> {tr}")
        print(f"   {bolded}")
    print(f"\n{len(rows)} pending")
    c.close()


def decide(video_id, path):
    d = json.load(open(path, encoding="utf-8"))
    known = set(d.get("known", []))
    garbage = set(d.get("garbage", []))
    retrans = {int(k): v for k, v in d.get("retranslate", {}).items()}
    c = con()
    rows = c.execute(
        "SELECT id FROM candidates WHERE video_id=? AND status='pending' ORDER BY id",
        (video_id,),
    ).fetchall()
    idmap = {i: cid for i, (cid,) in enumerate(rows, 1)}
    for n, cid in idmap.items():
        if n in retrans:
            c.execute("UPDATE candidates SET translation=? WHERE id=?", (retrans[n], cid))
        if n in known:
            span, nt = c.execute(
                "SELECT span_text, normalized_text FROM candidates WHERE id=?", (cid,)
            ).fetchone()
            c.execute("UPDATE candidates SET status='confirmed_known' WHERE id=?", (cid,))
            c.execute(
                """INSERT INTO known_lexicon(normalized_text, span_text, first_confirmed_video_id)
                   VALUES(?,?,?) ON CONFLICT(normalized_text) DO NOTHING""",
                (nt, span, video_id),
            )
        elif n in garbage:
            c.execute("UPDATE candidates SET status='garbage' WHERE id=?", (cid,))
        else:
            c.execute("UPDATE candidates SET status='confirmed_unknown' WHERE id=?", (cid,))
    c.commit()
    s = dict(c.execute(
        "SELECT status, count(*) FROM candidates WHERE video_id=? GROUP BY status",
        (video_id,),
    ).fetchall())
    print(f"video {video_id} status counts: {s}")
    c.close()


def export(video_id, out):
    import genanki

    c = con()
    title = c.execute("SELECT title FROM videos WHERE id=?", (video_id,)).fetchone()[0]
    rows = c.execute(
        """SELECT id, span_text, sentence, translation, is_phrase FROM candidates
           WHERE video_id=? AND status='confirmed_unknown' ORDER BY id""",
        (video_id,),
    ).fetchall()
    if not rows:
        print("nothing to export (no confirmed_unknown candidates)")
        return

    model = genanki.Model(
        1607392320,
        "RU context recognition",
        fields=[{"name": "Front"}, {"name": "Back"}, {"name": "Source"}],
        templates=[{
            "name": "Recognition",
            "qfmt": '<div class="sent">{{Front}}</div>',
            "afmt": '{{FrontSide}}<hr id="answer"><div class="tr">{{Back}}</div>'
                    '<div class="src">{{Source}}</div>',
        }],
        css=".card{font-size:20px;text-align:center;color:#222;background:#fff}"
            ".sent{margin:14px}.tr{font-size:22px;color:#222}"
            ".src{margin-top:18px;font-size:13px;color:#999}"
            "b{font-weight:700;color:inherit}",
    )
    deck = genanki.Deck(2059400110, f"Russian::{title or 'video ' + str(video_id)}")
    unbolded = []
    for cid, span, sent, tr, isph in rows:
        front = bold(sent, span, isph, "\x00")
        if "\x00" not in front:
            unbolded.append(span)
        front = front.replace("\x00", "<b>", 1).replace("\x00", "</b>", 1)
        deck.add_note(genanki.Note(model=model, fields=[front, tr or "", title or ""]))
    genanki.Package(deck).write_to_file(out)
    c.executemany(
        "UPDATE candidates SET exported_at=datetime('now') WHERE id=?",
        [(r[0],) for r in rows],
    )
    c.commit()
    print(f"wrote {len(rows)} cards -> {out}")
    if unbolded:
        print(f"WARNING: {len(unbolded)} cards have no bolded target: {unbolded}")
    c.close()


CMDS = {
    "add-candidates": lambda a: add_candidates(int(a[0]), a[1]),
    "batch": lambda a: batch(int(a[0])),
    "decide": lambda a: decide(int(a[0]), a[1]),
    "export": lambda a: export(int(a[0]), a[1]),
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    CMDS[sys.argv[1]](sys.argv[2:])
