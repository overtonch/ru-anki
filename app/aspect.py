"""Verb-aspect tags for vocabulary cards.

When a recognition card's target word is a Russian verb, the back shows which
aspect it is (imperfective / perfective / bi-aspectual) and its aspect partner,
with a link to the aspect explainer.

Aspect is a property of the lemma, not the individual card, so it's cached
lemma-keyed in `verb_aspect`. Negatives are cached too (`is_verb = 0`), so a
noun is only ever checked once. `db.pos_of` is the cheap pre-filter — obvious
non-verbs are settled with no LLM call; `llm.verb_aspect` fills aspect + partner
+ note for the words that really are verbs.
"""
import db
import llm
import store

# grammar concept the card-back "what's this?" link points at
CONCEPT = "aspect-core"

_LABEL = {"impf": "imperfective", "pf": "perfective", "both": "bi-aspectual"}
_OTHER = {"impf": "pf", "pf": "impf"}


def _bare(s):
    return store.norm((s or "").strip()).replace("́", "")


def _lemma_of(row):
    """The lemma key for a card row — its stressed dict form if we have one,
    else the normalized surface / span."""
    d = (row.get("dict_accented") or row.get("front_word") or "").strip()
    return _bare(d or row.get("normalized_text") or row.get("span_text") or "")


# --------------------------------------------------------------- lookup

def get(lemma):
    key = _bare(lemma)
    if not key:
        return None
    c = store.connect()
    r = c.execute("SELECT * FROM verb_aspect WHERE lemma=?", (key,)).fetchone()
    c.close()
    return dict(r) if r else None


def for_card(card):
    """The `aspect` block for a study/detail card, or None. Never calls the LLM —
    it only reads the cache; unresolved lemmas just come back None until a
    backfill / prewarm fills them in."""
    if not card or card.get("is_phrase"):
        return None
    row = None
    for key in (card.get("dict_accented"), card.get("front_word"),
                card.get("normalized_text"), card.get("span_text")):
        row = get(key)
        if row:
            break
    if not row or not row["is_verb"] or not row["aspect"]:
        return None
    other = _OTHER.get(row["aspect"])
    return {
        "aspect": row["aspect"],
        "label": _LABEL.get(row["aspect"], row["aspect"]),
        "other_label": _LABEL.get(other) if other else None,
        "partner": row["partner"] or None,
        "note": row["note"] or None,
        "concept": CONCEPT,
    }


# --------------------------------------------------------------- resolve

def _looks_verbal(lemma, gloss):
    if not lemma or " " in lemma:
        return False
    g = (gloss or "").strip().lower()
    if g.startswith(("to ", "to,")) or g in ("to", ""):
        pass  # a bare "to …" gloss is a strong verb signal
    try:
        if db.pos_of(lemma) == "verb":
            return True
    except Exception:  # noqa: BLE001
        pass
    return g.startswith("to ")


def _write(c, lemma, *, is_verb, aspect=None, partner=None, note=None):
    partner = (partner or "").strip() or None
    if partner in ("—", "-"):
        partner = None
    note = (note or "").strip() or None
    if note in ("—", "-"):
        note = None
    pb = _bare(partner) if partner else None
    c.execute(
        "INSERT INTO verb_aspect(lemma, is_verb, aspect, partner, partner_bare, note) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(lemma) DO UPDATE SET is_verb=excluded.is_verb, "
        "aspect=excluded.aspect, partner=excluded.partner, "
        "partner_bare=excluded.partner_bare, note=excluded.note, "
        "checked_at=datetime('now')",
        (lemma, 1 if is_verb else 0, aspect, partner, pb, note))
    return 1


def resolve(rows, model=None):
    """rows: card-row dicts (need normalized_text / span_text / translation, and
    ideally dict_accented / front_word). Writes a `verb_aspect` row for every
    distinct lemma — a verb tag for the verbs, a cached negative for the rest.
    Returns the number of lemmas written."""
    neg, ask = {}, {}
    for r in rows:
        lemma = _lemma_of(r)
        if not lemma or " " in lemma:
            continue
        gloss = (r.get("translation") or "").strip()
        if _looks_verbal(lemma, gloss):
            ask.setdefault(lemma, gloss or ask.get(lemma, ""))
        else:
            neg.setdefault(lemma, True)
    c = store.connect()
    n = 0
    for lemma in neg:
        if lemma not in ask:
            n += _write(c, lemma, is_verb=0)
    items = list(ask.items())
    for i in range(0, len(items), 40):
        chunk = items[i:i + 40]
        try:
            res = llm.verb_aspect([(lem, g) for lem, g in chunk], model=model)
        except Exception as e:  # noqa: BLE001
            print(f"[aspect] batch {i}: {e}", flush=True)
            continue
        for (lemma, _g), a in zip(chunk, res):
            if not a or not a.get("aspect"):
                n += _write(c, lemma, is_verb=0)
            else:
                n += _write(c, lemma, is_verb=1, aspect=a["aspect"],
                            partner=a.get("partner"), note=a.get("note"))
        c.commit()
    c.commit()
    c.close()
    return n


# --------------------------------------------------------------- backfill / prewarm

def _all_card_rows():
    c = store.connect()
    rows = c.execute(
        "SELECT id, span_text, normalized_text, front_word, dict_accented, "
        "translation FROM srs_cards WHERE is_phrase=0 ORDER BY id").fetchall()
    c.close()
    return [dict(r) for r in rows]


def pending_rows(force=False):
    """Card rows whose lemma isn't in the cache yet (all of them when force)."""
    rows = _all_card_rows()
    if force:
        return rows
    c = store.connect()
    have = {r["lemma"] for r in c.execute("SELECT lemma FROM verb_aspect")}
    c.close()
    return [r for r in rows if _lemma_of(r) and _lemma_of(r) not in have]


def backfill(force=False, model=None):
    rows = pending_rows(force=force)
    if not rows:
        return 0
    print(f"[aspect] resolving {len(rows)} card lemmas (force={force})…", flush=True)
    n = resolve(rows, model=model)
    print(f"[aspect] wrote {n} lemma tags", flush=True)
    return n


def pending_count(force=False):
    return len(pending_rows(force=force))
