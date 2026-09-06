"""One-off: add stress marks to flow-reading chunks that don't have them yet,
then run an audit pass and print anything that still looks wrong.

    ./.venv/bin/python accent_backfill.py            # accent missing chunks
    ./.venv/bin/python accent_backfill.py --audit    # only re-audit existing
    ./.venv/bin/python accent_backfill.py --audit --fix   # apply audit fixes too
"""
import re
import sys

sys.path.insert(0, "app")
import accent  # noqa: E402
import store   # noqa: E402

AUDIT = "--audit" in sys.argv
FIX = "--fix" in sys.argv
ALL = "--all" in sys.argv        # re-accent every chunk from its plain text


def _paras(text):
    return [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def accent_missing():
    c = store.connect()
    where = "" if ALL else "WHERE text_accented IS NULL OR text_accented=''"
    rows = c.execute(
        f"SELECT session_id, seq, text FROM reading_flow_chunks {where} "
        "ORDER BY session_id, seq").fetchall()
    c.close()
    print(f"{len(rows)} chunk(s) to accent")
    for r in rows:
        sid, seq = r["session_id"], r["seq"]
        try:
            acc = accent.accent_paragraphs(_paras(r["text"]))
            joined = "\n\n".join(acc)
        except Exception as e:  # noqa: BLE001
            print(f"  session {sid} chunk {seq}: FAILED — {e}")
            continue
        c = store.connect()
        c.execute("UPDATE reading_flow_chunks SET text_accented=? WHERE session_id=? AND seq=?",
                  (joined, sid, seq))
        c.commit()
        c.close()
        print(f"  session {sid} chunk {seq}: ok ({len(joined.split())} words)")


def audit():
    c = store.connect()
    rows = c.execute(
        "SELECT session_id, seq, text_accented FROM reading_flow_chunks "
        "WHERE text_accented IS NOT NULL AND text_accented<>'' ORDER BY session_id, seq").fetchall()
    c.close()
    total = 0
    for r in rows:
        sid, seq = r["session_id"], r["seq"]
        fixes = accent.verify(_paras(r["text_accented"]))
        if not fixes:
            continue
        total += len(fixes)
        print(f"\nsession {sid} chunk {seq}:")
        for f in fixes:
            print(f"   {f.get('before')!r} -> {f.get('after')!r}   ({f.get('why','')})")
        if FIX:
            txt = r["text_accented"]
            for f in fixes:
                b, a = f.get("before"), f.get("after")
                if b and a and b in txt:
                    txt = txt.replace(b, a)
            c = store.connect()
            c.execute("UPDATE reading_flow_chunks SET text_accented=? WHERE session_id=? AND seq=?",
                      (txt, sid, seq))
            c.commit()
            c.close()
            print("   -> applied")
    print(f"\n{total} suggested fix(es) across {len(rows)} chunk(s)")


if __name__ == "__main__":
    if not AUDIT:
        accent_missing()
    audit()
