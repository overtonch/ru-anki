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


def test_history_endpoint(client, db):
    client.get("/proficiency")
    h = client.get("/proficiency/history").json()["history"]
    assert h and "known_rank" in h[0] and isinstance(h[0]["domains"], dict)
