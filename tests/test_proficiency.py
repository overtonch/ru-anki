"""The internal proficiency model — global level estimate + per-domain fluency,
with a daily history for the stats graphs."""


def test_overview_has_headline_metrics_and_domains(client, db):
    d = client.get("/proficiency").json()
    assert d["known_rank"] and d["known_words"] and d["cefr"]
    assert isinstance(d["domains"], list) and len(d["domains"]) >= 8
    ids = {x["id"] for x in d["domains"]}
    assert {"food", "politics", "scifi"} <= ids
    # a snapshot for today was written
    assert any(h["day"] == d["day"] for h in d["history"])


def test_topics_are_grouped_into_domains(client, db):
    d = client.get("/reading/topics").json()
    assert d["domains"] and all(g["topics"] for g in d["domains"])


def test_reading_session_is_classified_into_a_domain(client, db):
    sid = client.post("/reading/sessions",
                      json={"prompt": "a recipe with rare mushrooms and spices"}).json()["id"]
    s = client.get(f"/reading/sessions/{sid}").json()
    assert s["domain"] == "food"


def test_reading_moves_the_domain_estimate_and_snapshot(client, db):
    import proficiency
    sid = client.post("/reading/sessions", json={"topic": "The strangest animals of the Russian far north"}).json()["id"]
    # lots of taps → weaker than assumed in this domain
    for w in ["город", "улица", "работа", "думать", "большой", "шумный", "незнакомый", "вокруг", "мимо", "серый"]:
        client.post(f"/reading/sessions/{sid}/tap", json={"surface": w, "chunk_seq": 1})
    client.post(f"/reading/sessions/{sid}/next", json={"read_seq": 1, "read_words": 200})
    dom = proficiency._domain_state().get("nature")
    assert dom and dom["words_read"] >= 200 and dom["taps"] >= 8

    over = client.get("/proficiency").json()
    nature = next(x for x in over["domains"] if x["id"] == "nature")
    assert nature["started"] and nature["words_read"] >= 200
    assert nature["comprehension"] is not None


def _seed_ref(db):
    """A little freq / dict reference data (the test DB ships these tables empty)."""
    import books
    c = db.connect()
    # mark the 30 commonest AK lemmas as "known" via freq rank, and give the next
    # 40 a dict_ru entry so they can surface as real word gaps
    top = sorted(books.freq("anna_karenina").items(), key=lambda kv: -kv[1])
    for i, (lem, _) in enumerate(top[:30]):
        c.execute("INSERT OR IGNORE INTO freq(normalized_text, rank) VALUES(?,?)", (lem, i + 1))
    for lem, _ in top[30:120]:
        c.execute("INSERT OR IGNORE INTO dict_ru(headword, gloss) VALUES(?,?)", (lem, "x"))
    c.commit(); c.close()


def test_anna_karenina_readiness(client, db):
    import proficiency
    _seed_ref(db)
    proficiency._book_cache.clear()
    r = proficiency.book_readiness("anna_karenina")
    assert r and 0 < r["coverage"] < 1
    assert r["verdict"] and r["feels_like"]
    assert r["top_gaps"] and all(g["count"] >= 1 for g in r["top_gaps"])
    d = client.get("/proficiency").json()
    assert d["book"]["book"] == "anna_karenina"
    h = client.get("/proficiency/history").json()["history"]
    assert any(x.get("ak_coverage") is not None for x in h)


def test_book_endpoint_and_making_cards_from_gaps(client, db):
    import proficiency
    _seed_ref(db)
    proficiency._book_cache.clear()
    gaps = client.get("/reading/books/anna_karenina").json()["top_gaps"]
    assert gaps
    lemmas = [g["lemma"] for g in gaps[:3]]
    made = client.post("/reading/books/anna_karenina/cards", json={"lemmas": lemmas}).json()
    assert made["made"] >= 1
    names = {c["span_text"] for c in client.get("/srs/cards?filter=all").json()["cards"]}
    assert set(lemmas) & names


def test_religion_domain_exists(client, db):
    d = client.get("/reading/topics").json()
    ids = {g["id"] for g in d["domains"]}
    assert "religion" in ids
    rel = next(g for g in d["domains"] if g["id"] == "religion")
    assert rel["topics"] and rel["form"] == "essay"
    sid = client.post("/reading/sessions",
                      json={"prompt": "the teachings of Advaita Vedanta and Vivekananda"}).json()["id"]
    assert client.get(f"/reading/sessions/{sid}").json()["domain"] == "religion"


def test_history_endpoint(client, db):
    client.get("/proficiency")
    h = client.get("/proficiency/history").json()["history"]
    assert h and "known_rank" in h[0] and isinstance(h[0]["domains"], dict)
