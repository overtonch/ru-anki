"""One-off: add the stress/ё hint (.acc line on the card Back) to Anki notes that
predate the accent feature. Idempotent — skips notes that already have it.

  ./.venv/bin/python backfill_accents.py          # do it
  ./.venv/bin/python backfill_accents.py --dry    # just show what would change
"""
import re
import sys

sys.path.insert(0, "app")

import anki          # noqa: E402
import llm           # noqa: E402
import store         # noqa: E402

DRY = "--dry" in sys.argv
BOLD = re.compile(r"<b>(.*?)</b>", re.S)
TAGS = re.compile(r"<[^>]+>")


def surface_and_context(front):
    m = BOLD.search(front or "")
    if not m:
        return None, None
    surface = TAGS.sub("", m.group(1)).strip()
    context = TAGS.sub("", front).strip()
    return surface, context


def main():
    anki.ensure_model()                      # make sure the .acc CSS is live
    ids = anki.invoke("findNotes", query="tag:ru-anki") or []
    print(f"{len(ids)} ru-anki notes")
    info = anki.invoke("notesInfo", notes=ids) or []

    todo = []          # (note_id, lemma, context, back)
    skip_phrase = skip_nobold = already = 0
    for n in info:
        back = n["fields"]["Back"]["value"]
        surface, context = surface_and_context(n["fields"]["Front"]["value"])
        if not surface:
            skip_nobold += 1
            continue
        if 'class="acc"' in back:
            already += 1
            continue
        lemma = store.lemma_key(surface)
        if " " in lemma or "-" in lemma or not lemma:
            skip_phrase += 1
            continue
        todo.append((n["noteId"], lemma, context, back))

    print(f"  {already} already done · {skip_phrase} phrases skipped · "
          f"{skip_nobold} no-bold skipped · {len(todo)} to accent")
    if not todo:
        return

    # accent in batches, reusing the cache where we can
    want = [(lemma, ctx) for _, lemma, ctx, _ in todo if not store.accent_for(lemma)]
    want = list({l: c for l, c in want}.items())

    def run(pairs):
        """pairs: [(lemma, context)]. Store any whose accent-stripped form still
        matches the lemma; return the lemmas that didn't take."""
        misses = []
        B = 8
        for i in range(0, len(pairs), B):
            batch = pairs[i:i + B]
            try:
                got = llm.accent_words([(store.yo_form(l), c) for l, c in batch])
            except Exception as e:  # noqa: BLE001
                print(f"  batch {i // B} failed: {e}")
                misses += [l for l, _ in batch]
                continue
            for (lemma, _), acc in zip(batch, got):
                bare = (acc or "").replace("́", "").lower().replace("ё", "е")
                if acc and bare == lemma.replace("ё", "е"):
                    store.set_accent(lemma, acc)
                    print(f"    {lemma} -> {acc}")
                else:
                    misses.append(lemma)
        return misses

    misses = run(want)
    if misses:
        print(f"  retrying {len(misses)} without context…")
        left = run([(l, "") for l in misses])
        if left:
            print(f"  no clean accent for: {', '.join(left)}")

    changed = 0
    for note_id, lemma, _, back in todo:
        acc = store.accent_for(lemma)
        if not acc:
            continue
        new_back = f'{back}<div class="acc">{acc}</div>'
        if DRY:
            print(f"  [dry] note {note_id}: +{acc}")
        else:
            anki.invoke("updateNoteFields",
                        note={"id": note_id, "fields": {"Back": new_back}})
        changed += 1

    print(f"{'would update' if DRY else 'updated'} {changed} notes")
    if not DRY:
        err = anki.try_sync()
        print("sync:", err or "ok")


if __name__ == "__main__":
    main()
