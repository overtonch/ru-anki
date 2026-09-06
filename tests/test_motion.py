"""Verbs-of-motion drill — combo-tagged generation, self-grading, spaced
re-tests of missed combinations, slice stats, "practise this", save-as-card.
LLM stubbed in conftest."""
import pytest


@pytest.fixture()
def sync_motion(monkeypatch, stub_llm):
    """Run background top-up + re-test generation inline, buffer warmed once."""
    import motion
    monkeypatch.setattr(motion, "topup_async", lambda force=False: motion._topup(force=force))
    monkeypatch.setattr(motion, "_gen_retest_async", motion._gen_retest)
    motion._topup(force=True)
    return motion


def _next(client, n=1):
    return client.get(f"/motion/next?n={n}").json()["items"]


# ---------------------------------------------------------------- generation / serve

def test_next_generates_and_serves(client, sync_motion):
    r = client.get("/motion/next?n=3").json()
    assert 1 <= len(r["items"]) <= 3
    it = r["items"][0]
    assert it["situation"] and it["highlight"] and it["answer"]
    assert it["highlight"] in it["situation"]
    assert "́" not in it["answer"]
    assert set(it["dims"]) == {"verb", "aspect", "prefix", "prep", "tense"}
    assert it["combo"] and it["combo"].count("|") == 4
    # dims/combo are canonical ids even when the model echoes the Russian pair
    import motion
    assert it["dims"]["verb"] in motion._VERB
    assert it["combo"].split("|")[0] in motion._VERB
    assert it["verb_pair"] and "/" in it["verb_pair"]
    assert it["card_id"] is None
    seen = {x["id"] for x in r["items"]}
    again = {x["id"] for x in client.get("/motion/next?n=3").json()["items"]}
    assert seen.isdisjoint(again)


def test_state_reports_buffer(client, sync_motion):
    st = client.get("/motion/state").json()
    assert st["buffer"] > 0
    assert st["level"] == 1
    assert st["seen"] == 0


# ---------------------------------------------------------------- grading / lapses

def test_wrong_answer_opens_a_lapse(client, sync_motion):
    it = _next(client)[0]
    r = client.post("/motion/grade", json={"item_id": it["id"], "verdict": "wrong"}).json()
    assert r["seen"] == 1
    assert r["open_lapses"] == 1


def test_right_answer_no_lapse(client, sync_motion):
    it = _next(client)[0]
    r = client.post("/motion/grade", json={"item_id": it["id"], "verdict": "right"}).json()
    assert r["open_lapses"] == 0
    assert r["right"] == 1


def test_grade_unknown_item_404(client, sync_motion):
    assert client.post("/motion/grade", json={"item_id": 999999, "verdict": "right"}).status_code == 404


def test_missed_combo_comes_back_as_retest(client, sync_motion):
    import motion
    # miss the same combo twice so a generated re-test batch is queued
    combo = None
    for _ in range(40):
        it = _next(client)[0]
        if combo is None:
            combo = it["combo"]
        if it["combo"] != combo:
            client.post("/motion/grade", json={"item_id": it["id"], "verdict": "right"})
            continue
        client.post("/motion/grade", json={"item_id": it["id"], "verdict": "wrong"})
        break
    # force the lapse due and pull the retest
    c = motion._c()
    c.execute("UPDATE motion_lapse SET due_pos = 1 WHERE combo = ?", (combo,))
    c.execute("UPDATE motion_lapse SET misses = 2 WHERE combo = ?", (combo,))
    c.commit()
    c.close()
    motion._gen_retest(motion._c().execute(
        "SELECT id FROM motion_lapse WHERE combo=?", (combo,)).fetchone()["id"])
    items = client.get("/motion/next?n=5").json()["items"]
    assert any(x["retest"] for x in items)


# ---------------------------------------------------------------- stats

def test_slice_stats_by_dimension(client, sync_motion):
    for _ in range(6):
        it = _next(client)[0]
        client.post("/motion/grade", json={"item_id": it["id"], "verdict": "right"})
    stats = client.get("/motion/stats").json()
    assert stats["overall"]["cards"] == 6
    assert set(stats["slices"]) == {"verb", "aspect", "prefix", "prep", "tense"}
    graded = [v for v in stats["slices"]["aspect"] if v["seen"] > 0]
    assert graded and all(v["status"] in ("seen", "practising", "learned") for v in graded)
    assert isinstance(stats["combos"], list)


def test_cards_carry_alternatives(client, sync_motion):
    it = _next(client)[0]
    assert it["alts"] and all(a["form"] and "why" in a for a in it["alts"])


def _grade_run(client, verdict, n):
    for _ in range(n):
        items = client.get("/motion/next?n=1").json()["items"]
        if not items:
            break
        client.post("/motion/grade", json={"item_id": items[0]["id"], "verdict": verdict})


def test_level_steps_up_on_a_strong_streak(client, sync_motion):
    import motion
    assert motion._level() == 1
    _grade_run(client, "right", motion._ADJUST_WINDOW * 3)
    assert motion._level() >= 2


def test_level_steps_down_when_struggling(client, sync_motion):
    import motion
    motion._set_level(3)
    c = motion._c(); c.execute("DELETE FROM motion_items WHERE verdict IS NULL"); c.commit(); c.close()
    motion._topup(force=True)                       # fresh buffer at the new level
    _grade_run(client, "wrong", motion._ADJUST_WINDOW * 4)
    assert motion._level() < 3


def test_self_correcting_cards_are_rejected():
    import motion
    good = {"situation": "The cat was in the hall. Then the cat walked into the room.",
            "highlight": "the cat walked into the room", "answer": "кошка вошла в комнату",
            "target": ["вошла в комнату"], "note": "войти = pf; в + accusative for an enclosed space.",
            "contrast": "Out of the room: вышла из комнаты.",
            "dims": {"verb": "idti", "aspect": "pf_prefix", "prefix": "v", "prep": "v_acc", "tense": "past"}}
    assert motion._row(good, good["dims"])["ok"] is True
    slop = {**good,
            "answer": "птицы летят домой",
            "note": "Wait—habitual daily action: use летают. птицы летают домой; летать = multidirectional."}
    assert motion._row(slop, good["dims"])["ok"] is False
    assert motion._row({**good, "note": "Actually, this should be ходит."}, good["dims"])["ok"] is False
    assert motion._row({**good, "answer": "он went домой"}, good["dims"])["ok"] is False


def test_verb_reference_page(client, sync_motion):
    r = client.get("/motion/verbs/idti")
    assert r.status_code == 200
    d = r.json()
    assert d["uni"] == "идти" and d["multi"] == "ходить"
    assert d["conj"]["present"]["я"] == ["идти1", "ходить1"]
    assert any(p["prefix_id"] == "pri" for p in d["prefixes"])   # «при-» mapped to our id
    assert d["mistakes"] and d["examples"]
    assert client.get("/motion/verbs/nope").status_code == 404
    # second call is a cache hit (same payload)
    assert client.get("/motion/verbs/idti").json() == d


def test_prefix_reference_page(client, sync_motion):
    r = client.get("/motion/prefixes/pri")
    assert r.status_code == 200
    d = r.json()
    assert d["prefix"] == "при-"
    assert d["what_it_does"] and d["exceptions"] and d["mistakes"]
    assert client.get("/motion/prefixes/none").status_code == 404
    assert client.get("/motion/prefixes/xx").status_code == 404


def test_taxonomy_endpoint(client):
    t = client.get("/motion/taxonomy").json()
    assert t["dims"] == ["verb", "aspect", "prefix", "prep", "tense"]
    assert any(v["value"] == "idti" for v in t["values"]["verb"])
    # every prefix in the catalog is a sliceable value with help text
    pfx = {v["value"]: v for v in t["values"]["prefix"]}
    for want in ("za", "pro", "pere", "ob", "vz", "s", "na", "do", "s_sya", "raz_sya"):
        assert want in pfx and pfx[want]["help"], want
    # the full level ladder is exposed with a note per rung
    assert t["max_level"] == len(t["levels"]) >= 10
    assert all(L["note"] for L in t["levels"])


def test_state_exposes_the_ladder(client, sync_motion):
    st = client.get("/motion/state").json()
    assert st["level"] == 1
    assert st["max_level"] >= 10
    assert "level 1" not in st["level_note"] and st["level_note"]


# ---------------------------------------------------------------- practise this

def test_learn_queues_focus_cards(client, sync_motion):
    import motion
    made = motion.learn("verb", "nesti", n=5)
    assert made == 5
    served = 0
    for _ in range(8):
        items = client.get("/motion/next?n=1").json()["items"]
        if items and items[0]["focus"]:
            served += 1
    assert served >= 1


# ---------------------------------------------------------------- save as card

def test_save_as_card_creates_production_card(client, sync_motion):
    it = _next(client)[0]
    client.post("/motion/grade", json={"item_id": it["id"], "verdict": "wrong"})
    r = client.post(f"/motion/items/{it['id']}/save", json={})
    assert r.status_code == 200
    cid = r.json()["card_id"]
    # idempotent
    assert client.post(f"/motion/items/{it['id']}/save", json={}).json()["card_id"] == cid


def test_session_suggest_returns_misses(client, sync_motion):
    missed = []
    for _ in range(4):
        it = _next(client)[0]
        client.post("/motion/grade", json={"item_id": it["id"], "verdict": "wrong"})
        missed.append(it["id"])
    right = _next(client)[0]
    client.post("/motion/grade", json={"item_id": right["id"], "verdict": "right"})
    cards = client.post("/motion/session/suggest",
                        json={"item_ids": missed + [right["id"]]}).json()["cards"]
    assert cards and all("answer" in c for c in cards)
    assert right["id"] not in {c["id"] for c in cards}
