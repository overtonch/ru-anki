"""Grammar catalog + progress tracking (sliceable by rule, band, or both)."""


def _seed(db, concept, right, wrong, band=1, easy=0):
    c = db.connect()
    c.executemany(
        "INSERT INTO drill_items(band, kind, skill, prompt, answer, verdict, graded_at) "
        "VALUES(?,'x',?,'p','a',?,datetime('now'))",
        [(band, concept, "right")] * right + [(band, concept, "wrong")] * wrong
        + [(band, concept, "easy")] * easy)
    c.commit()
    c.close()


def test_catalog_is_well_formed():
    import grammar
    assert len(grammar.CONCEPTS) > 120
    ids = [c.id for c in grammar.CONCEPTS]
    assert len(ids) == len(set(ids))                 # unique ids
    assert all(c.level in grammar.LEVELS for c in grammar.CONCEPTS)
    assert all(c.explain and c.title and c.hint for c in grammar.CONCEPTS)
    assert all(c.ex for c in grammar.CONCEPTS)       # every concept has examples
    lvls = {c.level for c in grammar.CONCEPTS}
    assert {"a2", "b1", "b2", "c1"} <= lvls          # spans the range


def test_concept_status_progression(db):
    import grammar
    cid = grammar.CONCEPTS[0].id
    assert grammar.concept_stats()[cid]["status"] == "new"
    _seed(db, cid, right=1, wrong=1)
    assert grammar.concept_stats()[cid]["status"] == "seen"
    _seed(db, cid, right=1, wrong=3)                 # 2/6 → practising
    assert grammar.concept_stats()[cid]["status"] == "practising"


def test_learned_needs_consistent_accuracy(db):
    import grammar
    cid = grammar.CONCEPTS[1].id
    _seed(db, cid, right=8, wrong=1)
    st = grammar.concept_stats()[cid]
    assert st["status"] == "learned" and st["pct"] >= 0.8


def test_too_easy_rests_a_concept_and_a_miss_brings_it_back(db):
    import grammar
    cid = grammar.CONCEPTS[2].id
    _seed(db, cid, right=1, wrong=0, easy=3)          # 3 "too easy", no misses
    st = grammar.concept_stats()[cid]
    assert st["status"] == "easy" and st["easy"] == 3 and st["pct"] == 1.0
    # a rested concept is (almost) never picked
    picks = [p["id"] for p in grammar.pick_concepts(80)]
    assert picks.count(cid) == 0
    # one miss and it's back in normal rotation
    _seed(db, cid, right=0, wrong=1)
    assert grammar.concept_stats()[cid]["status"] != "easy"


def test_easy_counts_as_right_for_accuracy_and_level(db):
    import grammar
    cid = grammar.CONCEPTS[3].id
    _seed(db, cid, right=0, wrong=0, easy=5)
    st = grammar.concept_stats()[cid]
    assert st["pct"] == 1.0 and st["right"] == 5
    lp = {r["level"]: r for r in grammar.level_progress()}
    assert lp[grammar.CONCEPTS[3].level]["learned"] >= 1   # rested concepts count as done


def test_level_progress_percentage(db):
    import grammar
    b1 = [c.id for c in grammar.CONCEPTS if c.level == "b1"]
    for cid in b1[:5]:
        _seed(db, cid, right=6, wrong=0)
    lp = {l["level"]: l for l in grammar.level_progress()}
    assert lp["b1"]["learned"] == 5
    assert lp["b1"]["pct"] == round(100 * 5 / lp["b1"]["total"])
    assert lp["a2"]["learned"] == 0


def test_stats_slice_by_band(db):
    import grammar
    cid = grammar.CONCEPTS[0].id
    _seed(db, cid, right=6, wrong=0, band=1)         # nails it on common words
    _seed(db, cid, right=1, wrong=5, band=4)         # shaky on rare words
    st = grammar.concept_stats(with_bands=True)[cid]
    assert st["by_band"]["1"]["pct"] == 1.0
    assert st["by_band"]["4"]["pct"] < 0.3
    # and the band summary aggregates across concepts
    bands = {b["band"]: b for b in grammar.band_progress()}
    assert bands[1]["right"] == 6 and bands[4]["wrong"] == 5


def test_pick_concepts_avoids_known_basics_and_leans_new(db):
    import grammar
    from collections import Counter
    picks = [p["id"] for p in grammar.pick_concepts(12, band_level="b1")]
    assert len(picks) == 12
    assert max(Counter(picks).values()) <= 2       # a concept appears at most twice
    basics = {c.id for c in grammar.CONCEPTS if c.basic}
    assert len(basics & set(picks)) <= 3           # school basics heavily down-weighted


def test_grammar_map_endpoint(client, db):
    import grammar
    cid = grammar.CONCEPTS[0].id
    _seed(db, cid, right=5, wrong=1, band=1)
    _seed(db, cid, right=0, wrong=2, band=3)
    d = client.get("/grammar").json()
    assert d["levels"] and d["concepts"] and d["bands"] and d["band_ranges"]
    entry = next(c for c in d["concepts"] if c["id"] == cid)
    assert entry["stats"]["seen"] == 8
    assert entry["stats"]["by_band"]["1"]["seen"] == 6
    assert d["totals"]["cards"] == 8 and d["totals"]["right"] == 5


def test_totals_count_legacy_cards_too(db):
    import grammar, store
    _seed(db, grammar.CONCEPTS[0].id, right=3, wrong=1)     # tagged to a concept
    c = store.connect()                                     # a legacy coarse skill, not in catalog
    c.executemany("INSERT INTO drill_items(band,kind,skill,prompt,answer,verdict,graded_at) "
                  "VALUES(1,'x','case:instrumental','p','a',?,datetime('now'))",
                  [("right",)] * 6 + [("wrong",)] * 2)
    c.commit(); c.close()
    t = grammar.totals()
    assert t["cards"] == 12 and t["tagged"] == 12       # both count toward the all-time total
    # but per-concept stats only see the catalog-tagged ones
    assert grammar.concept_stats()[grammar.CONCEPTS[0].id]["seen"] == 4
    # band accuracy counts everything
    assert grammar.band_progress()[0]["seen"] == 12


def test_concept_detail_endpoint(client, db):
    import grammar
    cid = grammar.CONCEPTS[2].id
    _seed(db, cid, right=2, wrong=1)
    d = client.get(f"/grammar/concepts/{cid}").json()
    assert d["title"] and d["examples"] and d["explain"]
    assert d["stats"]["seen"] == 3 and "by_band" in d["stats"]
    assert d["stats"]["mastery"] in ("new", "seen", "practising", "learned", "easy", "muted")
    assert client.get("/grammar/concepts/nope").status_code == 404


def test_mute_drops_a_concept_from_the_drill(client, db):
    import grammar
    cid = grammar.CONCEPTS[10].id
    r = client.post(f"/grammar/concepts/{cid}/mute", json={"muted": True}).json()
    assert r["muted"] is True
    assert cid in grammar.muted_ids()
    assert grammar.concept_stats()[cid]["mastery"] == "muted"
    # excluded from selection entirely
    assert cid not in {p["id"] for p in grammar.pick_concepts(80)}
    # and it counts as "done" toward the level completion %
    lvl = grammar.concept(cid).level
    lp = {x["level"]: x for x in grammar.level_progress()}
    assert lp[lvl]["learned"] >= 1
    # un-mute puts it back
    client.post(f"/grammar/concepts/{cid}/mute", json={"muted": False})
    assert cid not in grammar.muted_ids()
    assert client.post("/grammar/concepts/nope/mute", json={"muted": True}).status_code == 404
