"""The speaking side of the proficiency picture — a CEFR vocabulary ladder scored
against what the Activate drill has actually made productive, tracked next to the
reading level so the gap between them is visible."""
import json


def _seed_freq(db):
    import speaking_levels
    speaking_levels._active_cache.clear()      # tests reuse the process; freq differs per db
    c = db.connect()
    # a1 band: ranks 1..549 ; give it 20 real content words
    words = ["дом", "рука", "город", "друг", "работа", "вода", "земля", "утро",
             "вечер", "дорога", "письмо", "книга", "окно", "стол", "дерево",
             "погода", "деньги", "машина", "музыка", "картина"]
    for i, w in enumerate(words):
        c.execute("INSERT OR IGNORE INTO freq(normalized_text, rank) VALUES(?,?)", (w, i + 10))
        c.execute("INSERT OR IGNORE INTO dict_ru(headword, gloss) VALUES(?,?)", (w, "x"))
    c.commit(); c.close()


def test_ladder_scores_productive_vocab_and_reports_gap(client, db):
    _seed_freq(db)
    import speaking_levels, store
    # nothing productive yet
    e = speaking_levels.estimate(reading_cefr="b1")
    a1 = next(x for x in e["levels"] if x["level"] == "a1")
    assert a1["target"] >= 15 and a1["active"] == 0
    assert e["ord"] < 0.2 and e["gap"] > 2         # reading well ahead of speaking
    assert e["next_words"]                          # words to work on are suggested

    # make three a1 words productive
    c = store.connect()
    for w in ["дом", "рука", "город"]:
        c.execute("INSERT INTO activate_items(kind, target, gloss, reps, streak) "
                  "VALUES('word',?,?,6,3)", (w, "x"))
    c.commit(); c.close()
    speaking_levels._active_cache.clear()
    e2 = speaking_levels.estimate(reading_cefr="b1")
    a1b = next(x for x in e2["levels"] if x["level"] == "a1")
    assert a1b["active"] == 3
    assert e2["ord"] > e["ord"] and e2["gap"] < e["gap"]


def test_gap_is_written_to_the_daily_snapshot(client, db):
    _seed_freq(db)
    import proficiency
    e = proficiency.snapshot(force=True)
    assert e["speaking"] and e["speaking"]["reading_ord"] is not None
    c = db.connect()
    row = c.execute("SELECT speaking_ord, reading_ord, speaking_cefr "
                    "FROM proficiency_snapshots WHERE day=?", (e["day"],)).fetchone()
    c.close()
    assert row["reading_ord"] is not None and row["speaking_cefr"]


def test_levels_endpoint(client, db):
    _seed_freq(db)
    d = client.get("/activate/levels").json()
    assert d["cefr"] and isinstance(d["levels"], list) and len(d["levels"]) == 6
    assert "gap" in d
