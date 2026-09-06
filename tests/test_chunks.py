"""Chunk deck — function-tagged generation, self-grade with a fading per-chunk
scaffold, spaced re-tests of missed chunks, function slice stats, "practise this
function", save-as-card. LLM stubbed in conftest."""
import pytest


@pytest.fixture()
def sync_chunks(monkeypatch, stub_llm):
    import chunks
    monkeypatch.setattr(chunks, "topup_async", lambda force=False: chunks._topup(force=force))
    monkeypatch.setattr(chunks, "_gen_retest_async", chunks._gen_retest)
    chunks._topup(force=True)
    return chunks


def _next(client, n=1):
    return client.get(f"/chunks/next?n={n}").json()["items"]


# ---------------------------------------------------------------- generation / serve

def test_next_generates_and_serves(client, sync_chunks):
    r = client.get("/chunks/next?n=3").json()
    assert 1 <= len(r["items"]) <= 3
    it = r["items"][0]
    assert it["en"] and it["ru"] and it["chunk_ru"]
    assert it["chunk_ru"] in it["ru"]                      # required for the cloze stage
    assert it["fn"] and it["fn_label"]
    assert "́" not in it["ru"]
    assert it["stage_name"] in ("literal", "plain", "recall", "cloze")
    assert it["card_id"] is None
    seen = {x["id"] for x in r["items"]}
    again = {x["id"] for x in client.get("/chunks/next?n=3").json()["items"]}
    assert seen.isdisjoint(again)


def test_state_reports_progress(client, sync_chunks):
    st = client.get("/chunks/state").json()
    assert st["buffer"] > 0
    assert st["total"] == len(sync_chunks.CHUNKS)
    assert st["mastered"] == 0
    assert st["start_stage"] == 0                          # default: full English + gloss


# ---------------------------------------------------------------- scaffold staging

def test_scaffold_advances_on_a_streak_and_retreats_on_a_miss(db, stub_llm):
    import chunks
    cid = chunks.CHUNKS[0].id
    start = chunks._start_stage()
    assert chunks.chunk_stage(cid)["stage"] == start
    chunks._apply_grade(cid, "right")
    assert chunks.chunk_stage(cid)["stage"] == start           # 1 right < _ADVANCE_STREAK
    chunks._apply_grade(cid, "right")
    assert chunks.chunk_stage(cid)["stage"] == min(start + 1, chunks._MAX_STAGE)
    for _ in range(chunks._ADVANCE_STREAK):
        chunks._apply_grade(cid, "right")
    assert chunks.chunk_stage(cid)["stage"] == min(start + 2, chunks._MAX_STAGE)
    chunks._apply_grade(cid, "wrong")
    assert chunks.chunk_stage(cid)["stage"] == min(start + 1, chunks._MAX_STAGE)


def test_served_card_carries_current_stage(client, sync_chunks):
    import chunks
    it = _next(client)[0]
    client.post("/chunks/grade", json={"item_id": it["id"], "verdict": "wrong"})   # opens a lapse
    # now nudge the chunk's stage up out of band and make the lapse due
    chunks._apply_grade(it["chunk_id"], "right")
    chunks._apply_grade(it["chunk_id"], "right")
    want = chunks.chunk_stage(it["chunk_id"])["stage"]
    c = chunks._c()
    c.execute("UPDATE chunk_lapse SET due_pos=1 WHERE chunk_id=?", (it["chunk_id"],))
    c.commit()
    c.close()
    again = [x for x in client.get("/chunks/next?n=6").json()["items"] if x["chunk_id"] == it["chunk_id"]]
    assert again and again[0]["stage"] == want


def test_start_stage_setting(client, sync_chunks):
    client.post("/chunks/start-stage", json={"start_stage": 0})
    assert client.get("/chunks/state").json()["start_stage"] == 0


# ---------------------------------------------------------------- grading / lapses

def test_wrong_answer_opens_a_lapse(client, sync_chunks):
    it = _next(client)[0]
    r = client.post("/chunks/grade", json={"item_id": it["id"], "verdict": "wrong"}).json()
    assert r["seen"] == 1
    assert r["open_lapses"] == 1


def test_right_answer_no_lapse(client, sync_chunks):
    it = _next(client)[0]
    r = client.post("/chunks/grade", json={"item_id": it["id"], "verdict": "right"}).json()
    assert r["open_lapses"] == 0
    assert r["right"] == 1


def test_grade_unknown_item_404(client, sync_chunks):
    assert client.post("/chunks/grade", json={"item_id": 999999, "verdict": "right"}).status_code == 404


def test_missed_chunk_comes_back_as_retest(client, sync_chunks):
    import chunks
    cid = None
    for _ in range(40):
        it = _next(client)[0]
        if cid is None:
            cid = it["chunk_id"]
        if it["chunk_id"] != cid:
            client.post("/chunks/grade", json={"item_id": it["id"], "verdict": "right"})
            continue
        client.post("/chunks/grade", json={"item_id": it["id"], "verdict": "wrong"})
        break
    c = chunks._c()
    c.execute("UPDATE chunk_lapse SET due_pos=1, misses=2 WHERE chunk_id=?", (cid,))
    c.commit()
    lid = c.execute("SELECT id FROM chunk_lapse WHERE chunk_id=?", (cid,)).fetchone()["id"]
    c.close()
    chunks._gen_retest(lid)
    items = client.get("/chunks/next?n=5").json()["items"]
    assert any(x["retest"] for x in items)


# ---------------------------------------------------------------- stats

def test_function_slice_stats(client, sync_chunks):
    for _ in range(6):
        it = _next(client)[0]
        client.post("/chunks/grade", json={"item_id": it["id"], "verdict": "right"})
    d = client.get("/chunks/stats").json()
    assert d["overall"]["cards"] == 6
    assert {f["value"] for f in d["functions"]} == {f[0] for f in sync_chunks.FUNCTIONS}
    assert all(f["total"] > 0 for f in d["functions"])
    assert len(d["chunks"]) == len(sync_chunks.CHUNKS)
    assert sum(d["stage_hist"].values()) >= 1


def test_code_switched_cards_are_rejected(db, stub_llm):
    import chunks
    good = {"chunk_id": "nu", "en": "Well, I'm not sure that's a good idea.",
            "ru": "Ну, я не уверен, что это хорошая идея.", "chunk_en": "Well",
            "chunk_ru": "Ну", "gist": "hesitating before an opinion", "gloss": "well"}
    switched = {**good, "chunk_id": "slushay",
                "en": "Слушай, are you free this weekend?",       # Russian left in the English
                "ru": "Слушай, ты свободен в выходные?", "chunk_en": "Listen", "chunk_ru": "Слушай"}
    assert chunks._row(good)["ok"] is True
    assert chunks._row(switched)["ok"] is False
    # chunk_en that isn't a real slice of en → kept as no-highlight, card still fine
    loose = {**good, "chunk_en": "you know what I mean"}
    r = chunks._row(loose)
    assert r["ok"] is True and r["chunk_en"] is None


def test_taxonomy_endpoint(client):
    t = client.get("/chunks/taxonomy").json()
    assert any(f["value"] == "hedge" for f in t["functions"])
    assert [s["name"] for s in t["stages"]] == ["literal", "plain", "recall", "cloze"]


# ---------------------------------------------------------------- practise / save

def test_learn_queues_a_function_burst(client, sync_chunks):
    import chunks
    made = chunks.learn("react", n=5)
    assert made == 5
    served_fn = 0
    for _ in range(8):
        items = client.get("/chunks/next?n=1").json()["items"]
        if items and items[0]["focus"]:
            served_fn += 1
    assert served_fn >= 1


def test_save_as_card_is_idempotent(client, sync_chunks):
    it = _next(client)[0]
    client.post("/chunks/grade", json={"item_id": it["id"], "verdict": "wrong"})
    r = client.post(f"/chunks/items/{it['id']}/save", json={})
    assert r.status_code == 200
    cid = r.json()["card_id"]
    assert client.post(f"/chunks/items/{it['id']}/save", json={}).json()["card_id"] == cid


def test_session_suggest_returns_misses(client, sync_chunks):
    missed = []
    for _ in range(4):
        it = _next(client)[0]
        client.post("/chunks/grade", json={"item_id": it["id"], "verdict": "wrong"})
        missed.append(it["id"])
    right = _next(client)[0]
    client.post("/chunks/grade", json={"item_id": right["id"], "verdict": "right"})
    cards = client.post("/chunks/session/suggest",
                        json={"item_ids": missed + [right["id"]]}).json()["cards"]
    assert cards and all("ru" in c for c in cards)
    assert right["id"] not in {c["id"] for c in cards}
