"""Flow reading — endless LLM-generated reading that auto-tunes difficulty from
which words the reader taps as unknown. LLM stubbed in conftest."""


def test_start_returns_a_first_chunk(client, db):
    r = client.post("/reading/sessions", json={"topic": "life in New York"})
    assert r.status_code == 200
    d = r.json()
    assert d["id"] and d["chunk"]["seq"] == 1
    assert d["chunk"]["text"] and d["chunk"]["n_words"] > 0
    assert d["chunk"]["level"]


def test_needs_a_topic_or_prompt(client, db):
    assert client.post("/reading/sessions", json={}).status_code == 422


def test_next_continues_and_marks_the_previous_chunk_read(client, db):
    import store
    sid = client.post("/reading/sessions", json={"topic": "x"}).json()["id"]
    n = client.post(f"/reading/sessions/{sid}/next",
                    json={"read_seq": 1, "read_words": 40}).json()
    assert n["chunk"]["seq"] == 2
    c = store.connect()
    assert c.execute("SELECT read FROM reading_flow_chunks WHERE session_id=? AND seq=1",
                     (sid,)).fetchone()["read"] == 1
    c.close()


def test_tapping_words_lowers_the_level_estimate(client, db):
    import store
    sid = client.post("/reading/sessions", json={"topic": "x"}).json()["id"]
    before = client.get(f"/reading/sessions/{sid}").json()["rank_est"]
    # tap a pile of "unknown" words, then read the chunk
    for w in ["город", "улица", "работа", "думать", "большой", "шумный", "незнакомый", "вокруг"]:
        client.post(f"/reading/sessions/{sid}/tap",
                    json={"surface": w, "sentence": f"вот {w} тут", "chunk_seq": 1})
    client.post(f"/reading/sessions/{sid}/next", json={"read_seq": 1, "read_words": 80})
    after = client.get(f"/reading/sessions/{sid}").json()["rank_est"]
    assert after < before                       # "too many unknowns → reader knows fewer words"
    assert client.get(f"/reading/sessions/{sid}").json()["unknown_seen"] == 8


def test_tap_always_returns_a_translation(client, db):
    """A word the local dictionary doesn't have still gets an LLM gloss, and it's
    cached so the resumed session shows it too."""
    import store
    sid = client.post("/reading/sessions", json={"topic": "x"}).json()["id"]
    r = client.post(f"/reading/sessions/{sid}/tap",
                    json={"surface": "тарабарщина", "sentence": "какая-то тарабарщина"}).json()
    assert r["gloss"]                                   # not None / empty
    assert store.word_gloss_get(r["lemma"])             # cached
    u = client.get(f"/reading/sessions/{sid}").json()["unknown"]
    assert u[0]["gloss"]


def test_untap_removes_a_word(client, db):
    sid = client.post("/reading/sessions", json={"topic": "x"}).json()["id"]
    client.post(f"/reading/sessions/{sid}/tap", json={"surface": "кот"})
    assert client.get(f"/reading/sessions/{sid}").json()["unknown_seen"] == 1
    client.post(f"/reading/sessions/{sid}/untap", json={"surface": "кот"})
    assert client.get(f"/reading/sessions/{sid}").json()["unknown_seen"] == 0


def test_make_cards_from_tapped_words(client, db):
    sid = client.post("/reading/sessions", json={"topic": "x"}).json()["id"]
    client.post(f"/reading/sessions/{sid}/tap",
                json={"surface": "шумный", "sentence": "шумный город", "chunk_seq": 1})
    r = client.post(f"/reading/sessions/{sid}/cards", json={"lemmas": ["шумный"]})
    assert r.json()["made"] == 1
    assert any(c["span_text"] == "шумный"
               for c in client.get("/srs/cards?filter=all").json()["cards"])
    # marked carded, so a second call is a no-op
    assert client.post(f"/reading/sessions/{sid}/cards",
                       json={"lemmas": ["шумный"]}).json()["made"] == 0


def test_delete(client, db):
    sid = client.post("/reading/sessions", json={"topic": "x"}).json()["id"]
    assert client.delete(f"/reading/sessions/{sid}").json()["deleted"] is True
    assert client.get(f"/reading/sessions/{sid}").status_code == 404
