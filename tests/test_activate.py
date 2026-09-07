"""Speaking-activation drill — introduces verb/word items, serves a prompt,
grades + reschedules, tracks the active-word count."""
import json


def _seed(db, mix=1.0):
    c = db.connect()
    for i, (v, g) in enumerate([("зависеть", "to depend"), ("думать", "to think"),
                                ("бояться", "to fear"), ("помогать", "to help")]):
        c.execute("INSERT OR IGNORE INTO activate_verbs(verb, rank, gloss, government, trap) VALUES(?,?,?,?,?)",
                  (v, i + 50, g, json.dumps([{"gov": "от + gen", "role": "x", "ex": "y"}]), None))
    for i, w in enumerate(["город", "работа", "думать", "быстрый", "решение"]):
        c.execute("INSERT OR IGNORE INTO freq(normalized_text, rank) VALUES(?,?)", (w, i + 100))
    c.commit(); c.close()
    import activate
    activate.set_settings(mix=mix, per_day=20)


def test_serves_a_verb_prompt_then_reschedules(client, db):
    _seed(db, mix=1.0)
    d = client.get("/activate/next").json()
    it = d["item"]
    assert it and it["kind"] == "verb" and it["target"]
    assert it["task"] and it["government"]                 # prompt + government present
    r = client.post(f"/activate/items/{it['id']}/grade",
                    json={"rating": 3, "task": it["task"], "government": it["government"]}).json()
    assert r["next_due"]
    # the same item shouldn't be served again immediately
    d2 = client.get("/activate/next").json()
    assert not d2["item"] or d2["item"]["id"] != it["id"]


def test_typed_attempt_is_checked(client, db):
    _seed(db, mix=1.0)
    it = client.get("/activate/next").json()["item"]
    r = client.post(f"/activate/items/{it['id']}/grade", json={
        "rating": 3, "produced": f"Я {it['target']} от родителей",
        "task": it["task"], "government": it["government"]}).json()
    assert r["check"] and r["check"]["used_target"] is True


def test_active_count_and_stats(client, db):
    _seed(db, mix=1.0)
    import activate, store
    # push one item to "active" (reps>=4, streak>=2)
    it = client.get("/activate/next").json()["item"]
    c = store.connect()
    c.execute("UPDATE activate_items SET reps=5, streak=3 WHERE id=?", (it["id"],))
    c.commit(); c.close()
    assert activate.active_count() == 1
    s = client.get("/activate/stats").json()
    assert s["active"] == 1 and s["by_kind"].get("verb", 0) >= 1
    assert client.get("/proficiency").json()["active_words"] == 1


def test_hard_government_verbs_come_first_and_can_be_skipped(client, db):
    import activate, store
    c = db.connect()
    c.execute("DELETE FROM activate_verbs")
    # easy (plain acc, no trap) is more frequent; hard (bare instr + a trap) rarer
    c.execute("INSERT INTO activate_verbs(verb, rank, gloss, government, trap, hardness) VALUES(?,?,?,?,?,?)",
              ("делать", 10, "to do", '[{"gov":"acc","role":"x","ex":"y"}]', None, 0))
    c.execute("INSERT INTO activate_verbs(verb, rank, gloss, government, trap, hardness) VALUES(?,?,?,?,?,?)",
              ("пользоваться", 200, "to use", '[{"gov":"instr (no prep)","role":"x","ex":"y"}]',
               "instrumental, no preposition", 3))
    c.commit(); c.close()
    activate.set_settings(mix=1.0, per_day=20)
    first = client.get("/activate/next").json()["item"]
    assert first["target"] == "пользоваться"          # trickier government first
    # skip it — "I already know this"
    r = client.post(f"/activate/items/{first['id']}/grade", json={"rating": 5}).json()
    got = store.connect().execute(
        "SELECT reps, interval_d FROM activate_items WHERE id=?", (first["id"],)).fetchone()
    assert got["reps"] >= 5 and got["interval_d"] >= 100    # retired far into the future


def test_hardness_ranks_real_government_traps_above_trivial_verbs(db):
    import activate
    def h(v, govs, trap="t"):
        return activate._hardness(v, [{"gov": g, "role": "x", "ex": "y"} for g in govs], trap)
    # trivial high-frequency verbs never float up
    assert h("сказать", ["dat (no prep)", "о + prep"]) <= 1
    assert h("знать", ["acc", "о + prep"]) == 0
    # the genuine traps score high — a bare instr/gen object is the worst
    assert h("пользоваться", ["instr (no prep)"]) == 4
    assert h("бояться", ["gen (no prep)", "за + acc"]) == 4
    assert h("зависеть", ["от + gen"]) == 3
    assert h("относиться", ["к + dat"]) == 3
    # plain transitive is easy
    assert h("построить", ["acc"]) == 0
    # …and a bare-case verb outranks a preposition verb outranks plain acc
    assert h("заниматься", ["instr (no prep)"]) > h("зависеть", ["от + gen"]) > h("строить", ["acc"])


def test_difficulty_auto_adjusts_toward_the_target_success_rate(client, db):
    import activate, store
    c = db.connect()
    c.execute("DELETE FROM activate_verbs")
    for i in range(30):
        c.execute("INSERT INTO activate_verbs(verb, rank, gloss, government, trap, hardness) "
                  "VALUES(?,?,?,?,?,?)", (f"гл{i}", i + 10, "to x",
                  '[{"gov":"instr (no prep)","role":"x","ex":"y"}]', "t", 3))
    c.commit(); c.close()
    activate.set_settings(level="a2", mix=1.0, per_day=0, auto=True)
    start = activate.settings()["level"]
    # ace everything — success rate 100% > the up-threshold
    for _ in range(activate._ADAPT_WINDOW + 2):
        it = client.get("/activate/next").json()["item"]
        client.post(f"/activate/items/{it['id']}/grade",
                    json={"rating": 4, "categories": []})
    moved = activate.settings()["level"]
    assert activate.LEVELS.index(moved) > activate.LEVELS.index(start)


def test_it_is_endless_with_no_daily_cap(client, db):
    _seed(db, mix=0.5)
    import activate
    activate.set_settings(per_day=0)
    seen = 0
    for _ in range(14):
        d = client.get("/activate/next").json()
        if not d["item"]:
            break
        seen += 1
        client.post(f"/activate/items/{d['item']['id']}/grade", json={"rating": 3, "categories": []})
    assert seen >= 12          # kept serving well past any 10/day cap


def test_flagged_errors_show_up_in_weak_spots(client, db):
    _seed(db, mix=1.0)
    it = client.get("/activate/next").json()["item"]
    client.post(f"/activate/items/{it['id']}/grade",
                json={"rating": 1, "categories": ["government", "aspect"]})
    s = client.get("/activate/stats").json()
    cats = {w["category"] for w in s["weak_spots"]}
    assert {"government", "aspect"} <= cats


def test_calibration_retires_verbs_the_learner_already_handles(client, db):
    import activate, store
    c = db.connect()
    c.execute("DELETE FROM activate_verbs")
    for i, v in enumerate(["пользоваться", "зависеть", "делать", "строить"]):
        c.execute("INSERT INTO activate_verbs(verb, rank, gloss, government, trap, hardness) "
                  "VALUES(?,?,?,?,?,?)", (v, i + 10, "to x",
                  '[{"gov":"instr (no prep)","role":"x","ex":"y"}]', "t", 3))
    c.commit(); c.close()

    batch = client.get("/activate/calibrate?kind=verb").json()
    assert [i["target"] for i in batch["items"]][:4]  # all four offered
    r = client.post("/activate/calibrate",
                    json={"kind": "verb", "known": ["делать", "строить"]}).json()
    assert r["retired"] == 2
    # retired verbs count as active and never resurface in a fresh batch
    assert activate.active_count() >= 2
    again = client.get("/activate/calibrate?kind=verb").json()
    assert "делать" not in [i["target"] for i in again["items"]]
    # the drill now serves one of the two it didn't retire
    it = client.get("/activate/next").json()["item"]
    assert it["target"] in ("пользоваться", "зависеть")


def test_perfect_streak_climbs_two_rungs(client, db):
    import activate, store
    c = db.connect()
    c.execute("DELETE FROM activate_verbs")
    for i in range(20):
        c.execute("INSERT INTO activate_verbs(verb, rank, gloss, government, trap, hardness) "
                  "VALUES(?,?,?,?,?,?)", (f"вербо{i}", i + 10, "to x",
                  '[{"gov":"instr (no prep)","role":"x","ex":"y"}]', "t", 3))
    c.commit(); c.close()
    activate.set_settings(level="a1", mix=1.0, per_day=0, auto=True)
    for _ in range(activate._ADAPT_WINDOW + 1):
        it = client.get("/activate/next").json()["item"]
        client.post(f"/activate/items/{it['id']}/grade", json={"rating": 4, "categories": []})
    assert activate.LEVELS.index(activate.settings()["level"]) >= 2   # jumped past a2


def test_mix_can_favour_words(client, db):
    _seed(db, mix=0.0)
    kinds = set()
    for _ in range(4):
        d = client.get("/activate/next").json()
        if not d["item"]:
            break
        kinds.add(d["item"]["kind"])
        client.post(f"/activate/items/{d['item']['id']}/grade", json={"rating": 3})
    assert "word" in kinds
