"""Word-family info on the card back — sibling words sharing a root, so a card
for работать can remind you that you already know работа. Uses the existing
word_family table (set up for green-highlighting); this only adds the display
+ backfill-coverage layer."""


def _card(srs, span, gloss, sentence="Это тут."):
    return srs.create_card(sentence, span, span, False, gloss)


def test_family_for_card_none_when_unresolved(db):
    import store
    assert store.family_for_card("работать") is None


def test_family_for_card_excludes_self_and_sorts_known_first(client, db):
    import srs, store
    _card(srs, "работать", "to work")
    c2 = _card(srs, "рабочий", "worker")
    store.set_word_family("работа", ["работать", "работа", "рабочий", "работник"])

    fam = store.family_for_card("работать")
    assert fam["root"] == "работа"
    lemmas = [m["lemma"] for m in fam["members"]]
    assert "работать" not in lemmas                      # excludes itself
    assert set(lemmas) == {"работа", "рабочий", "работник"}
    # known members (a card exists) sort first
    known = [m for m in fam["members"] if m["known"]]
    assert {m["lemma"] for m in known} == {"рабочий"}
    assert fam["members"][0]["known"] is True
    # the known sibling's gloss comes from its own card's translation
    rab = next(m for m in fam["members"] if m["lemma"] == "рабочий")
    assert rab["gloss"] == "worker"
    del c2


def test_solo_word_has_no_family_block(client, db):
    import srs, store
    store.set_word_family("уникальный", ["уникальный"])
    assert store.family_for_card("уникальный") is None


def test_card_payload_carries_family_block(client, db):
    import srs, store
    c = _card(srs, "говорить", "to speak")
    store.set_word_family("говор", ["говорить", "разговор", "поговорить"])

    d = client.get(f"/srs/cards/{c['id']}").json()
    fam = d["family"]
    assert fam["root"] == "говор"
    assert {m["lemma"] for m in fam["members"]} == {"разговор", "поговорить"}


def test_production_and_phrase_cards_have_no_family_block(client, db):
    import srs, store
    store.set_word_family("говор", ["говорить", "разговор"])
    phrase = _card(srs, "на самом деле", "actually", sentence="На самом деле, да.")
    d = client.get(f"/srs/cards/{phrase['id']}").json()
    assert d["family"] is None


def test_lemmas_without_family_reads_srs_cards_directly(client, db):
    import srs, store
    c = _card(srs, "смотреть", "to watch")
    todo = store.lemmas_without_family()
    assert "смотреть" in todo
    store.set_word_family("смотр", ["смотреть"])
    assert "смотреть" not in store.lemmas_without_family()
    del c


def test_freq_hint_judges_by_the_commonest_family_form(db):
    import store
    c = store.connect()
    c.executemany("INSERT OR REPLACE INTO freq(normalized_text, rank) VALUES(?,?)",
                  [("презирать", 9000), ("презрение", 3200), ("презрительный", 12000)])
    c.commit(); c.close()
    store.set_word_family("презр", ["презирать", "презрение", "презрительный"])

    # the adjective on its own is rank 12000; via its family it's judged at 3200
    r = store.freq_hint("презрительный")
    assert r["rank"] == 3200
    assert "as презрение" in r["label"]
    # the commonest form doesn't get a redundant "(as …)"
    assert "(as" not in store.freq_hint("презрение")["label"]


def test_family_rank_falls_back_to_own_rank_without_a_family(db):
    import store
    c = store.connect()
    c.execute("INSERT OR REPLACE INTO freq(normalized_text, rank) VALUES('одинокий', 7777)")
    c.commit(); c.close()
    assert store.family_rank("одинокий") == (7777, "одинокий")


def test_has_family_entry(db):
    import store
    assert store.has_family_entry("одинокий") is False
    store.set_word_family("одинокий", ["одинокий"])
    assert store.has_family_entry("одинокий") is True


def _stoplist(rows):
    """rows: [(word, rank), ...]"""
    import store
    c = store.connect()
    c.executemany("INSERT OR REPLACE INTO stoplist(normalized_text, rank) VALUES(?,?)", rows)
    c.commit(); c.close()


def test_set_word_family_only_guards_true_function_words_by_rank(db):
    """The 13k-word stoplist is a raw frequency cutoff, not a 'definitely a
    function word' list — content words a learner genuinely cards (презирать,
    презрение) rank in it too (in the thousands). Only the narrow top-of-list
    band (true pronouns/conjunctions/particles) should block a family member;
    an ordinary carded word must survive even with no `keep`."""
    import store
    _stoplist([("что", 6), ("презрение", 6451)])

    store.set_word_family("презр", ["презирать", "презрение", "презрительный", "что"])
    assert store.has_family_entry("презрение")             # common, but not a function word
    assert not store.has_family_entry("что")                # genuine function word, still filtered


def test_set_word_family_keeps_a_stoplisted_seed_word(db):
    """Belt-and-suspenders: even if a seed word itself sat inside the narrow
    function-word band for some reason, `keep` guarantees its own row."""
    import store
    _stoplist([("презирать", 40)])
    store.set_word_family("презр", ["презирать", "презрение"], keep="презирать")
    assert store.has_family_entry("презирать")


def test_learn_family_no_longer_blocked_by_stoplist(client, db, monkeypatch):
    """Regression: _learn_family used to hard-skip any lemma in the (huge,
    frequency-based) stoplist, silently leaving ordinary carded words with no
    family forever — and even after that, set_word_family's own member filter
    was still dropping true siblings like презрение (the noun form)."""
    import main, srs, store
    _stoplist([("презирать", 3210), ("презрение", 6451)])
    monkeypatch.setattr(main.llm, "word_family", lambda w, model=None:
                        ("презр", ["презирать", "презрение", "презрительный"]))

    _card(srs, "презирать", "to despise")
    main._learn_family("презирать")
    fam = store.family_for_card("презирать")
    assert fam and {"презрение", "презрительный"} <= {m["lemma"] for m in fam["members"]}


def test_word_family_falls_back_to_a_fresh_call_on_a_doubled_warm_reply(monkeypatch):
    """The warm `claude -p` channel occasionally hands back a second, glued-on
    JSON object under load — word_family should recover with one plain call
    instead of leaving the lemma stuck forever."""
    import llm
    garbled = "hmm, let me reconsider — actually I'm not sure about this one"
    monkeypatch.setattr(llm, "_warm_or_oneshot", lambda *a, **k: garbled)
    calls = []

    def fake_run_claude(prompt, system, model=None, timeout=180):
        calls.append(prompt)
        return '{"root": "презр", "members": ["презирать", "презрение", "презрительный"]}', {}
    monkeypatch.setattr(llm, "run_claude", fake_run_claude)

    root, members = llm.word_family("презирать")
    assert calls, "should have fallen back to a fresh call"
    assert root == "презр"
    assert set(members) == {"презирать", "презрение", "презрительный"}
