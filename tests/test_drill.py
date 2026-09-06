"""Grammar drill — concept-driven generation, band movement, spaced re-tests,
end-of-session suggestions, save-as-card. LLM stubbed (conftest); `freq` seeded
per-test.
"""
import pytest

_NOUNS = ["вопрос", "минута", "закон", "ответ", "сила", "город", "рука", "слово",
          "земля", "вода", "работа", "место", "жизнь", "голова", "глаз",
          "друг", "враг", "книга", "стол", "дверь", "окно", "дорога", "лес"]
_VERBS = ["думать", "стоять", "бояться", "держать", "решать", "решить", "давать",
          "дать", "брать", "взять", "ходить", "идти", "писать", "читать",
          "помогать", "помочь", "звонить", "ждать", "любить", "видеть"]
_ADJS = ["красный", "сильный", "новый", "важный", "старый", "белый", "чёрный",
         "добрый", "злой", "полный", "трудный", "лёгкий"]
_WORDS = [(w, 180 + i * 6) for i, w in enumerate(_NOUNS + _VERBS + _ADJS)]


@pytest.fixture()
def freq_band(db):
    c = db.connect()
    c.executemany("INSERT OR REPLACE INTO freq(normalized_text, rank) VALUES(?,?)", _WORDS)
    c.commit()
    c.close()
    return db


@pytest.fixture()
def sync_topup(monkeypatch, stub_llm, freq_band):
    """Run background top-up + re-test generation inline (and warm the buffer once)."""
    import drill
    monkeypatch.setattr(drill, "topup_async", lambda force=False: drill._topup(force=force))
    monkeypatch.setattr(drill, "_gen_retest_async", drill._gen_retest)
    drill._topup(force=True)
    return drill


def _one(client):
    return client.get("/drill/next?n=1").json()["items"][0]


def _concept_id(n=0):
    import grammar
    return grammar.CONCEPTS[n].id


# ---------------------------------------------------------------- generation / serve

def test_next_generates_and_serves(client, freq_band, sync_topup):
    r = client.get("/drill/next?n=3").json()
    assert 1 <= len(r["items"]) <= 3
    it = r["items"][0]
    assert it["prompt"] and it["answer"] and it["note"]
    assert it["concept"] and it["concept_title"]              # tagged with a catalog concept
    assert it["concept_level"] in ("a2", "a2plus", "b1", "b2", "c1", "c2")
    assert isinstance(it["given"], list) and it["given"]
    assert it["card_id"] is None
    assert all("́" not in x["answer"] for x in r["items"])
    seen = {x["id"] for x in r["items"]}
    assert seen.isdisjoint({x["id"] for x in client.get("/drill/next?n=3").json()["items"]})


def test_buffer_refills_as_it_drains(client, freq_band, sync_topup):
    served = sum(bool(client.get("/drill/next?n=1").json()["items"]) for _ in range(25))
    assert served >= 22


def test_generation_covers_varied_concepts(client, freq_band, sync_topup):
    concepts = set()
    for _ in range(20):
        for it in client.get("/drill/next?n=3").json()["items"]:
            concepts.add(it["concept"])
    assert len(concepts) >= 6                                 # not stuck on one rule


# ---------------------------------------------------------------- band movement

def test_streak_moves_band_up(client, freq_band, sync_topup):
    band0 = client.get("/drill/state").json()["band"]
    moved = False
    for _ in range(5):
        moved |= client.post("/drill/grade",
                             json={"item_id": _one(client)["id"], "verdict": "right"}).json()["moved"]
    st = client.get("/drill/state").json()
    assert moved and st["band"] == band0 + 1 and st["right_streak"] == 0


def test_misses_move_band_down(client, freq_band, sync_topup):
    import srs
    srs.set_setting("drill_band", 2)
    for _ in range(2):
        g = client.post("/drill/grade",
                        json={"item_id": _one(client)["id"], "verdict": "wrong"}).json()
    assert g["moved"] and client.get("/drill/state").json()["band"] == 1


def test_band_move_leaves_a_bridge_no_stall(client, freq_band, sync_topup):
    for _ in range(5):
        client.post("/drill/grade", json={"item_id": _one(client)["id"], "verdict": "right"})
    assert client.get("/drill/next?n=3").json()["items"]      # no dead stall after the move


def test_grade_unknown_item_404(client, db):
    assert client.post("/drill/grade", json={"item_id": 999, "verdict": "right"}).status_code == 404


def test_regrade_is_idempotent_for_streak(client, freq_band, sync_topup):
    it = _one(client)
    client.post("/drill/grade", json={"item_id": it["id"], "verdict": "right"})
    s1 = client.get("/drill/state").json()["right_streak"]
    client.post("/drill/grade", json={"item_id": it["id"], "verdict": "right"})
    assert client.get("/drill/state").json()["right_streak"] == s1


def test_too_easy_is_not_a_miss_and_counts_toward_the_streak(client, freq_band, sync_topup):
    it = _one(client)
    r = client.post("/drill/grade", json={"item_id": it["id"], "verdict": "easy"}).json()
    assert r["open_lapses"] == 0
    assert r["right_streak"] >= 1 or r["moved"]


def test_third_too_easy_rests_the_rule_and_surfaces_it(client, freq_band, sync_topup, db):
    it = _one(client)
    cid = it["concept"]
    c = db.connect()          # two "too easy" already on the books for this rule
    c.executemany("INSERT INTO drill_items(band, kind, skill, prompt, answer, verdict, graded_at) "
                  "VALUES(1,'x',?,'p','a','easy',datetime('now'))", [(cid,), (cid,)])
    c.commit(); c.close()
    r = client.post("/drill/grade", json={"item_id": it["id"], "verdict": "easy"}).json()
    assert r["eased"]                                     # crossed the threshold → told the UI
    import grammar
    assert grammar.concept_stats()[cid]["status"] == "easy"
    assert cid not in {p["id"] for p in grammar.pick_concepts(80)}


# ---------------------------------------------------------------- lapses / re-tests

def test_a_miss_comes_back_as_a_retest(client, freq_band, sync_topup):
    it = _one(client)
    client.post("/drill/grade", json={"item_id": it["id"], "verdict": "wrong"})
    assert client.get("/drill/state").json()["open_lapses"] == 1
    saw = None
    for _ in range(14):
        for x in client.get("/drill/next?n=1").json()["items"]:
            if x.get("retest"):
                saw = x
    assert saw and saw["concept"] == it["concept"]


def test_retest_right_retires_the_lapse(client, freq_band, sync_topup):
    it = _one(client)
    client.post("/drill/grade", json={"item_id": it["id"], "verdict": "wrong"})
    retest = None
    for _ in range(14):
        for x in client.get("/drill/next?n=1").json()["items"]:
            if x.get("retest"):
                retest = x
        if retest:
            break
    assert retest
    r = client.post("/drill/grade", json={"item_id": retest["id"], "verdict": "right"}).json()
    assert r.get("retest") is True and r["moved"] is False
    assert client.get("/drill/state").json()["open_lapses"] == 0


def test_repeat_miss_escalates_to_targeted_cards(client, freq_band, sync_topup):
    import drill
    it = _one(client)
    concept = it["concept"]
    client.post("/drill/grade", json={"item_id": it["id"], "verdict": "wrong"})
    c = drill._c()
    mid = c.execute("INSERT INTO drill_items(band, lemma, kind, skill, prompt, answer) "
                    "VALUES(1,?,?,?,'p','a')", (it["lemma"], it["kind"], concept)).lastrowid
    c.commit(); c.close()
    client.post("/drill/grade", json={"item_id": mid, "verdict": "wrong"})
    lap = drill._c().execute("SELECT * FROM drill_lapse WHERE skill=?", (concept,)).fetchone()
    assert lap["misses"] >= 2
    made = drill._c().execute("SELECT COUNT(*) n FROM drill_items WHERE retest_for=?",
                              (lap["id"],)).fetchone()["n"]
    assert made >= 1


# ---------------------------------------------------------------- "learn this rule"

def test_learn_concept_queues_focus_cards(client, freq_band, sync_topup):
    import drill
    cid = _concept_id(5)
    assert drill.learn_concept(cid, n=4) >= 1
    # a focus card jumps ahead of the regular buffer and is tagged
    got = None
    for _ in range(3):
        for x in client.get("/drill/next?n=1").json()["items"]:
            if x.get("focus"):
                got = x
        if got:
            break
    assert got and got["concept"] == cid


def test_learn_endpoint(client, freq_band, sync_topup):
    import drill
    monkey = client
    cid = _concept_id(3)
    r = client.post(f"/grammar/concepts/{cid}/learn")
    assert r.status_code == 200 and r.json()["status"] == "generating"
    assert client.post("/grammar/concepts/not-a-real-id/learn").status_code == 404


# ---------------------------------------------------------------- end-of-session

def test_session_suggest_returns_missed_should_knows(client, freq_band, sync_topup):
    ids, wrong = [], set()
    for i in range(8):
        it = _one(client)
        ids.append(it["id"])
        v = "wrong" if i % 2 else "right"
        if v == "wrong":
            wrong.add(it["id"])
        client.post("/drill/grade", json={"item_id": it["id"], "verdict": v})
    sug = client.post("/drill/session/suggest", json={"item_ids": ids}).json()["cards"]
    assert sug and len(sug) <= 8
    assert {s["id"] for s in sug} <= wrong


def test_session_suggest_skips_above_level(client, freq_band, sync_topup):
    import drill
    c = drill._c()
    hid = c.execute(
        "INSERT INTO drill_items(band, lemma, kind, skill, prompt, answer, verdict, graded_at) "
        "VALUES(5,'редкий','case',?,'hard','ответ','wrong',datetime('now'))",
        (_concept_id(0),)).lastrowid
    c.commit(); c.close()
    assert client.post("/drill/session/suggest", json={"item_ids": [hid]}).json()["cards"] == []


# ---------------------------------------------------------------- save as card

def test_save_creates_production_card(client, freq_band, sync_topup, monkeypatch):
    import main
    monkeypatch.setattr(main, "_sync_soon", lambda *a, **k: None)
    it = _one(client)
    r = client.post(f"/drill/items/{it['id']}/save").json()
    assert r["card_id"] and r["back"] == it["answer"] and r["front"] == it["prompt"]
    q = client.get("/srs/queue").json()["cards"]
    prod = [c for c in q if c.get("card_type") == "production" and c["id"] == r["card_id"]]
    assert prod and prod[0]["given"] == it["given"] and prod[0]["situation"] == it["prompt"]
    assert prod[0]["concept_title"] == it["concept_title"]
    assert client.get("/srs/stats").json()["orphans"] == 0
    r2 = client.post(f"/drill/items/{it['id']}/save").json()
    assert r2["card_id"] == r["card_id"]


def test_save_unknown_item_404(client, db):
    assert client.post("/drill/items/999/save").status_code == 404


# ---------------------------------------------------------------- generic tts

def test_generic_tts_endpoint(client, monkeypatch, tmp_path):
    import main
    f = tmp_path / "clip.m4a"
    f.write_bytes(b"\x00" * 800)
    monkeypatch.setattr(main.tts, "synthesize", lambda text: str(f))
    r = client.get("/tts", params={"q": "привет мир"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mp4"
    assert client.get("/tts", params={"q": "  "}).status_code == 422
