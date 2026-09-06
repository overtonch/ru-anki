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
