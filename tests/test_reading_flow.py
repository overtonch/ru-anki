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


def test_reading_without_tapping_does_not_move_the_global_level(client, db):
    """Continuing past a part with zero taps is ambiguous (comprehension vs.
    skimming) — it must not feed the global estimate."""
    import srs
    srs.set_setting("known_rank", "3000")
    sid = client.post("/reading/sessions", json={"topic": "x"}).json()["id"]
    # read four parts, never tap a word
    for seq in range(1, 5):
        client.post(f"/reading/sessions/{sid}/next",
                    json={"read_seq": seq, "read_words": 140})
    assert srs.get_setting("known_rank") == "3000"        # untouched
    # the session's own rank_est also didn't drift upward on zero taps
    assert client.get(f"/reading/sessions/{sid}").json()["rank_est"] <= 3000


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


def test_piece_is_fixed_length_and_ends(client, db):
    import reading_flow
    d = client.post("/reading/sessions", json={"topic": "a landowner returns home"}).json()
    sid = d["id"]
    assert d["chunk"]["part"] == 1 and d["chunk"]["total"] == reading_flow.PARTS
    assert not d["chunk"]["done"]
    seq = 1
    for _ in range(reading_flow.PARTS + 2):          # keep asking well past the end
        r = client.post(f"/reading/sessions/{sid}/next",
                        json={"read_seq": seq, "read_words": 130}).json()["chunk"]
        seq = r["part"]
    assert seq == reading_flow.PARTS                 # never generated a 6th part
    full = client.get(f"/reading/sessions/{sid}").json()
    assert full["done"] and full["status"] == "done"
    assert len(full["chunks"]) == reading_flow.PARTS
    assert full["title"]


def test_sequel_is_a_linked_new_session(client, db):
    import reading_flow
    sid = client.post("/reading/sessions", json={"topic": "the clockmaker"}).json()["id"]
    for s in range(1, reading_flow.PARTS + 1):
        client.post(f"/reading/sessions/{sid}/next", json={"read_seq": s, "read_words": 130})
    r = client.post(f"/reading/sessions/{sid}/sequel")
    assert r.status_code == 200
    new_sid = r.json()["id"]
    assert new_sid != sid
    seq = client.get(f"/reading/sessions/{new_sid}").json()
    assert seq["parent_id"] == sid and seq["chunks"] and not seq["done"]
    assert any(x["parent_id"] == sid for x in client.get("/reading/sessions").json()["sessions"])


def test_chunk_audio_is_synthesised_and_cached(client, db, stub_tts):
    import store
    sid = client.post("/reading/sessions", json={"topic": "город"}).json()["id"]
    r = client.get(f"/reading/sessions/{sid}/chunks/1/audio")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mp4"
    c = store.connect()
    ap = c.execute("SELECT audio_path FROM reading_flow_chunks WHERE session_id=? AND seq=1",
                   (sid,)).fetchone()["audio_path"]
    c.close()
    assert ap                                         # path cached on the chunk
    assert any(ch.get("has_audio") for ch in client.get(f"/reading/sessions/{sid}").json()["chunks"])


def test_delete(client, db):
    sid = client.post("/reading/sessions", json={"topic": "x"}).json()["id"]
    assert client.delete(f"/reading/sessions/{sid}").json()["deleted"] is True
    assert client.get(f"/reading/sessions/{sid}").status_code == 404
