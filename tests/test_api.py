"""Endpoint smoke tests through the FastAPI TestClient. The LLM is stubbed, so
these are fast and free — they check wiring, status codes and shapes, not model
quality."""


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert "backup" in r.json()


def test_index_and_sw_served(client):
    assert client.get("/").status_code == 200
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "SHELL" in sw.text


def test_videos_list_empty(client):
    r = client.get("/videos")
    assert r.status_code == 200 and r.json() == []


def test_watch_and_word_flags(client, seeded_video):
    r = client.get(f"/videos/{seeded_video}/watch")
    assert r.status_code == 200
    body = r.json()
    assert body["video"]["kind"] == "video"
    assert [c["text"] for c in body["cues"]][0].startswith("Он блефовал")
    assert "raw_subs" not in body["video"]


def test_translate_preview_uses_stub(client, seeded_video):
    r = client.post("/translate", json={"video_id": seeded_video, "span": "блефовать",
                                        "sentence": "Он блефовал за столом."})
    assert r.status_code == 200
    j = r.json()
    assert j["span_text"] == "блефовать" and j["translation"] == "[блефовать]"
    assert "<b>" in j["front_html"]


def test_make_card_then_queue_and_review(client, seeded_video):
    mk = client.post(f"/videos/{seeded_video}/make-card",
                     json={"span": "блефовать", "timestamp": "00:00:00",
                           "sentence": "Он блефовал за столом.",
                           "span_text": "блефовать", "translation": "to bluff",
                           "is_phrase": False})
    assert mk.status_code == 200
    card_id = mk.json()["srs_card"]["id"]

    q = client.get("/srs/queue")
    assert q.status_code == 200
    ids = [c["id"] for c in q.json()["cards"]]
    assert card_id in ids
    card = next(c for c in q.json()["cards"] if c["id"] == card_id)
    assert card["clip"] and card["preview"]

    rv = client.post(f"/srs/cards/{card_id}/review", json={"rating": 3, "elapsed_ms": 4000})
    assert rv.status_code == 200


def test_manual_card_and_detail(client):
    r = client.post("/srs/cards", json={"span": "лакуна", "note": "a film"})
    assert r.status_code == 200
    cid = r.json()["srs_card"]["id"]

    # not an orphan even though it has no video
    assert client.get("/srs/stats").json()["orphans"] == 0
    assert cid in [c["id"] for c in client.get("/srs/cards?filter=manual").json()["cards"]]

    d = client.get(f"/srs/cards/{cid}").json()
    assert d["source"] == "manual" and "issues" in d and "reps" in d
    assert d["tts"] is True                     # manual cards get spoken audio

    assert client.post("/srs/cards", json={"span": "  "}).status_code == 422


def test_bulk_fix_strips_span_stress(client, seeded_video, db):
    import srs
    c = srs.create_card("Он тут.", "сло́во", "сло́во", False, "word")
    assert "́" in srs.get_card(c["id"])["span_text"]
    n = client.post("/srs/cards/fix").json()["span_stress_stripped"]
    assert n >= 1
    assert "́" not in srs.get_card(c["id"])["span_text"]


def test_word_verdict_learned_vs_known(client, seeded_video):
    mk = client.post(f"/videos/{seeded_video}/make-card",
                     json={"span": "блефовать", "timestamp": "00:00:00",
                           "sentence": "Он блефовал за столом.",
                           "span_text": "блефовать", "translation": "to bluff",
                           "is_phrase": False})
    assert mk.status_code == 200

    states = client.get("/words/states").json()
    keys = [s["key"] for s in states["assignable"]]
    assert "learned" in keys and "known" in keys

    r = client.post("/words/блефовать/state", json={"state": "learned"})
    assert r.status_code == 200
    assert r.json()["removed_srs_cards"] == 1

    wd = client.get("/words/блефовать").json()
    assert wd["status"] == "learned" and wd["verdict"] == "learned"

    lst = client.get("/words?state=learned").json()
    assert "блефовать" in [w["lemma"] for w in lst["words"]]
    assert "блефовать" not in [w["lemma"] for w in client.get("/words?state=known").json()["words"]]

    an = client.get("/srs/analytics").json()
    assert an["word_states"].get("learned") == 1

    # undo → back to undecided
    assert client.request("DELETE", "/words/блефовать/state").status_code == 200
    assert client.get("/words/блефовать").json()["status"] in ("new", "pending")

    # bad state rejected
    assert client.post("/words/x/state", json={"state": "bogus"}).status_code == 422


def test_card_front_mode_toggle(client, seeded_video):
    mk = client.post(f"/videos/{seeded_video}/make-card",
                     json={"span": "блефовать", "timestamp": "00:00:00",
                           "sentence": "Он блефовал за столом.",
                           "span_text": "блефовать", "translation": "to bluff",
                           "is_phrase": False})
    cid = mk.json()["srs_card"]["id"]

    # default: front is the bolded sentence
    q = client.get("/srs/queue").json()["cards"]
    card = next(c for c in q if c["id"] == cid)
    assert "<b>" in card["front_html"] and card["front_mode"] == "sentence"
    assert card["sentence_html"] == card["front_html"]

    r = client.post("/settings", json={"key": "card_front", "value": "word"})
    assert r.status_code == 200 and r.json()["card_front"] == "word"

    q = client.get("/srs/queue").json()["cards"]
    card = next(c for c in q if c["id"] == cid)
    assert card["front_mode"] == "word"
    assert 'class="hw"' in card["front_html"]
    assert "<b>" in card["sentence_html"]          # sentence preserved on the back

    # reversible
    client.post("/settings", json={"key": "card_front", "value": "sentence"})
    q = client.get("/srs/queue").json()["cards"]
    card = next(c for c in q if c["id"] == cid)
    assert "<b>" in card["front_html"] and card["front_mode"] == "sentence"

    assert client.post("/settings", json={"key": "card_front", "value": "bogus"}).status_code == 422


def test_video_practice_deck(client, seeded_video):
    mk = client.post(f"/videos/{seeded_video}/make-card",
                     json={"span": "блефовать", "timestamp": "00:00:00",
                           "sentence": "Он блефовал за столом.",
                           "span_text": "блефовать", "translation": "to bluff",
                           "is_phrase": False})
    card_id = mk.json()["srs_card"]["id"]

    # shows up in the home list's per-content count
    assert client.get("/videos").json()[0]["card_count"] == 1

    p = client.get(f"/videos/{seeded_video}/study")
    assert p.status_code == 200
    body = p.json()
    assert [c["id"] for c in body["cards"]] == [card_id]
    assert "title" in body

    # practice must not touch the schedule: card is still new & due after
    before = client.get("/srs/queue").json()
    due_before = next(c for c in before["cards"] if c["id"] == card_id)
    assert due_before["is_new"]

    client.get("/videos/999999/study").status_code == 404


def test_srs_stats_and_offline_bundle(client, seeded_video):
    client.post(f"/videos/{seeded_video}/make-card",
                json={"span": "ветер", "timestamp": "00:00:06",
                      "sentence": "Ветер трепал полы пальто.",
                      "span_text": "ветер", "translation": "wind", "is_phrase": False})
    s = client.get("/srs/stats").json()
    assert s["total"] == 1 and "review_pace_s" in s
    b = client.get("/srs/offline?days=3").json()
    assert len(b["cards"]) == 1
    assert isinstance(b["media"], list)


def test_text_card_gets_tts_clip(client, db):
    import store
    vid = store.add_reading_text(
        "http://ex.test/book", "A Book", "An Author",
        [{"title": "Ch 1", "paragraphs": ["Первый абзац с редким словом.",
                                          "Второй абзац идёт следом.",
                                          "Третий абзац завершает."]}])
    r = client.post(f"/videos/{vid}/make-card", json={
        "span": "редкий", "sentence": "Первый абзац с редким словом.",
        "span_text": "редкий", "translation": "rare", "is_phrase": False,
        "timestamp": "1:00:01"})
    assert r.status_code == 200, r.text
    card_id = r.json()["srs_card"]["id"]
    view = client.get(f"/srs/cards/{card_id}").json()
    assert view["tts"] is True
    assert view["clip"] == f"/srs/cards/{card_id}/tts"
    ctx = client.get(f"/srs/cards/{card_id}/context").json()
    assert ctx["after"].startswith("Второй абзац")


def test_delete_video_keep_vs_purge(client, seeded_video):
    client.post(f"/videos/{seeded_video}/make-card",
                json={"span": "ночь", "timestamp": "00:00:03",
                      "sentence": "Он ушёл в ночь.", "span_text": "ночь",
                      "translation": "night", "is_phrase": False})
    # keep = archive: video hidden, card kept and still linked
    d = client.request("DELETE", f"/videos/{seeded_video}", params={"cards": "keep"})
    assert d.status_code == 200 and d.json()["mode"] == "keep"
    assert client.get("/videos").json() == []
    assert client.get("/videos?archived=1").json()[0]["id"] == seeded_video
    assert client.get("/srs/stats").json()["total"] == 1

    client.post(f"/videos/{seeded_video}/unhide")
    d2 = client.request("DELETE", f"/videos/{seeded_video}", params={"cards": "delete"})
    assert d2.status_code == 200 and d2.json()["cards_deleted"] == 1
    assert client.get("/srs/stats").json()["total"] == 0


def test_settings_roundtrip(client):
    r = client.post("/settings", json={"key": "new_per_day", "value": 33})
    assert r.status_code == 200
    assert client.get("/settings").json()["new_per_day"] == 33
