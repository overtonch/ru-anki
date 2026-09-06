"""Reformulation speaking drill — batch session + end-of-session cards. LLM stubbed."""


def _attempt(client, prompt_id, text):
    """Submit an attempt; grading runs as a BackgroundTask so a follow-up GET
    sees it done."""
    r = client.post("/speak/attempt", json={"prompt_id": prompt_id, "user_text": text})
    assert r.status_code == 200
    return r.json()["attempt_id"]


def test_attempt_grades_in_background(client):
    p = client.post("/speak/prompt").json()
    assert p["id"] and p["text"]
    aid = _attempt(client, p["id"], "Завтра не могу прийти извини")

    v = client.get(f"/speak/attempt/{aid}").json()
    assert v["status"] == "done"
    assert v["meaning"] == "ok"
    assert v["native"] == "Извини, завтра не получится."
    assert v["native_gloss"]
    assert "reformulations" not in v                       # single native version now
    assert len(v["corrections"]) == 2
    assert {c["severity"] for c in v["corrections"]} == {"hard", "style"}
    assert v["diff_ok"] is True
    assert v["general"]


def test_session_suggest_then_create(client):
    p1 = client.post("/speak/prompt").json()
    p2 = client.post("/speak/prompt").json()
    a1 = _attempt(client, p1["id"], "Завтра не могу прийти")
    a2 = _attempt(client, p2["id"], "Я купил новый диван")

    sug = client.post("/speak/session/suggest",
                      json={"attempt_ids": [a1, a2]}).json()["suggestions"]
    assert 1 <= len(sug) <= 5
    high = [s for s in sug if s["leverage"] == "high"]
    assert high                                            # hard corrections → high
    assert all("front" in s and "back" in s for s in sug)
    assert any(s["alternatives"] for s in high)            # alt phrasings offered

    # create the high-leverage ones, picking an alternative back for the first
    chosen = [{"front": high[0]["front"], "back": high[0]["alternatives"][0]}]
    chosen += [{"front": s["front"], "back": s["back"]} for s in high[1:]]
    made = client.post("/speak/session/cards", json={"cards": chosen}).json()
    assert made["created"] == len(chosen)
    assert made["cards"][0]["back"] == high[0]["alternatives"][0]


def test_session_production_cards_join_queue_and_are_not_orphans(client):
    p = client.post("/speak/prompt").json()
    a = _attempt(client, p["id"], "Завтра не могу")
    sug = client.post("/speak/session/suggest", json={"attempt_ids": [a]}).json()["suggestions"]
    client.post("/speak/session/cards",
                json={"cards": [{"front": sug[0]["front"], "back": sug[0]["back"]}]})

    q = client.get("/srs/queue").json()["cards"]
    prod = [c for c in q if c.get("card_type") == "production"]
    assert prod
    assert prod[0]["situation"] and prod[0]["target"]
    assert prod[0]["clip"].endswith("/tts")
    assert client.get("/srs/stats").json()["orphans"] == 0
    assert client.get("/srs/cards?filter=orphan").json()["cards"] == []
    assert len(client.get("/srs/cards?filter=production").json()["cards"]) >= 1


def test_production_card_reviews_and_reschedules(client):
    p = client.post("/speak/prompt").json()
    a = _attempt(client, p["id"], "Завтра не могу")
    sug = client.post("/speak/session/suggest", json={"attempt_ids": [a]}).json()["suggestions"]
    made = client.post("/speak/session/cards",
                       json={"cards": [{"front": sug[0]["front"], "back": sug[0]["back"]}]}).json()
    cid = made["cards"][0]["id"]

    d = client.get(f"/srs/cards/{cid}").json()
    assert d["card_type"] == "production"
    assert client.post(f"/srs/cards/{cid}/review", json={"rating": 3}).status_code == 200
    d2 = client.get(f"/srs/cards/{cid}").json()
    assert d2["is_new"] is False and d2["reps"] == 1


def test_regrade_and_stats(client):
    p = client.post("/speak/prompt").json()
    a = _attempt(client, p["id"], "Завтра не могу прийти")
    assert client.post(f"/speak/attempt/{a}/regrade").status_code == 200
    assert client.get(f"/speak/attempt/{a}").json()["status"] == "done"

    s = client.get("/speak/stats").json()
    assert s["attempts"] >= 1
    assert "aspect" in {row["category"] for row in s["by_category"]}


def test_empty_and_bad_prompt_rejected(client):
    p = client.post("/speak/prompt").json()
    assert client.post("/speak/attempt",
                       json={"prompt_id": p["id"], "user_text": " "}).status_code == 422
    assert client.post("/speak/attempt",
                       json={"prompt_id": 999999, "user_text": "привет"}).status_code == 404
