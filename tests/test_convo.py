"""Conversation partner — spoken, turn-based Russian conversation with an
in-character LLM. LLM + Whisper stubbed."""


def test_start_from_a_scenario_gives_an_opening_line(client, db):
    r = client.post("/convo/sessions", json={"scenario_id": "uncle-job"})
    assert r.status_code == 200
    s = r.json()["session"]
    assert s["persona"] and s["situation"]
    assert s["turns"][0]["role"] == "partner" and s["turns"][0]["text"]


def test_needs_a_scenario_or_prompt(client, db):
    assert client.post("/convo/sessions", json={}).status_code == 422


def test_a_code_switched_turn_is_understood_and_gaps_logged(client, db):
    sid = client.post("/convo/sessions", json={"scenario_id": "uncle-job"}).json()["id"]
    r = client.post(f"/convo/sessions/{sid}/say-text",
                    json={"text": "Я programmer, делаю websites дома"}).json()
    assert r["reply_ru"]
    gap_en = {g["en"] for g in r["gaps"]}
    assert {"programmer", "websites"} <= gap_en          # the English words got caught
    assert not r["errors"]

    full = client.get(f"/convo/sessions/{sid}").json()
    roles = [t["role"] for t in full["turns"]]
    assert roles == ["partner", "learner", "partner"]
    learner = full["turns"][1]
    assert learner["gaps"] and "programmer" in {g["en"] for g in learner["gaps"]}


def test_say_via_audio_transcribes_then_replies(client, db, monkeypatch):
    import convo
    monkeypatch.setattr(convo.whisper_rt, "transcribe",
                        lambda src, dur=0, progress=None, language=None: [(0, 1, "привет как дела")])
    sid = client.post("/convo/sessions", json={"scenario_id": "small-talk"}).json()["id"]
    r = client.post(f"/convo/sessions/{sid}/say",
                    files={"file": ("t.webm", b"x" * 2000, "audio/webm")})
    assert r.status_code == 200
    assert r.json()["learner_text"] == "привет как дела"
    assert r.json()["reply_ru"]


def test_debrief_summarises_and_offers_cards(client, db):
    sid = client.post("/convo/sessions", json={"scenario_id": "uncle-job"}).json()["id"]
    client.post(f"/convo/sessions/{sid}/say-text", json={"text": "Привет, всё хорошо"})
    d = client.post(f"/convo/sessions/{sid}/debrief").json()
    assert d["summary"] and d["cards"]
    assert client.get(f"/convo/sessions/{sid}").json()["status"] == "ended"

    made = client.post(f"/convo/sessions/{sid}/cards", json={"cards": d["cards"]}).json()
    assert made["made"] == len(d["cards"])


def test_delete(client, db):
    sid = client.post("/convo/sessions", json={"scenario_id": "cashier"}).json()["id"]
    assert client.delete(f"/convo/sessions/{sid}").json()["deleted"] is True
    assert client.get(f"/convo/sessions/{sid}").status_code == 404
