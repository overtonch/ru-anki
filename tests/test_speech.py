"""Speech Lab — generate / paste a longer piece, async audio build (stubbed off
under RU_TEST), practice logging. LLM + TTS stubbed."""


def test_paste_a_speech(client):
    r = client.post("/speeches/paste", json={
        "ru": "Ну, если честно, русский я учу уже несколько лет.\n\nСначала был курс, потом видео."})
    assert r.status_code == 200
    sid = r.json()["id"]
    d = client.get(f"/speeches/{sid}").json()
    assert d["source"] == "pasted"
    assert d["ru"].startswith("Ну, если честно")
    assert d["en"]                                   # annotate stub filled it in
    assert d["notes"]                                # key chunks pulled out
    assert d["status"] == "ready"                    # audio step no-ops in tests
    assert client.get("/speeches").json()["speeches"][0]["id"] == sid


def test_generate_a_speech(client):
    r = client.post("/speeches/generate", json={"topic": "how I got into Russian", "extra": "a course, then YouTube"})
    sid = r.json()["id"]
    d = client.get(f"/speeches/{sid}").json()
    assert d["title"] == "How I learned Russian"
    assert "семестр" in d["ru"] or "курс" in d["ru"]
    assert d["topic"] and "YouTube" in d["topic"]
    assert d["status"] == "ready"


def test_empty_paste_is_rejected(client):
    assert client.post("/speeches/paste", json={"ru": "  "}).status_code == 422
    assert client.post("/speeches/generate", json={"topic": ""}).status_code == 422


def test_practice_logging(client):
    sid = client.post("/speeches/paste", json={"ru": "Короче, я просто много слушаю и повторяю."}).json()["id"]
    r1 = client.post(f"/speeches/{sid}/practiced").json()
    r2 = client.post(f"/speeches/{sid}/practiced").json()
    assert r1["practice_count"] == 1 and r2["practice_count"] == 2
    assert client.get(f"/speeches/{sid}").json()["last_practiced"]


def test_delete(client):
    sid = client.post("/speeches/paste", json={"ru": "Тестовая речь для удаления, вот такая."}).json()["id"]
    assert client.delete(f"/speeches/{sid}").json()["deleted"] is True
    assert client.get(f"/speeches/{sid}").status_code == 404


def test_audio_404_when_not_built(client):
    sid = client.post("/speeches/paste", json={"ru": "Речь без аудио, потому что тесты."}).json()["id"]
    assert client.get(f"/speeches/{sid}/audio").status_code == 404   # tts unavailable under RU_TEST


def test_llm_json_parser_tolerates_raw_newlines_in_strings():
    import llm
    raw = '{"title": "T", "ru": "первый абзац.\n\nвторой абзац.", "notes": []}'
    d = llm._parse_obj(raw)
    assert d["ru"] == "первый абзац.\n\nвторой абзац."


def test_paragraph_arrays_are_joined(client):
    # the real prompt returns ru/en as arrays; speech._norm_ru joins them
    import speech
    assert speech._norm_ru(["one", "two"]) == "one\n\ntwo"
    assert speech._norm_ru("plain string") == "plain string"


def test_tts_hq_splits_sentences_and_paragraphs():
    import tts_hq
    parts = tts_hq.split_sentences("Привет. Как дела?\n\nВторой абзац тут. И ещё одно.")
    assert parts == ["Привет.", "Как дела?", None, "Второй абзац тут.", "И ещё одно."]
    assert tts_hq.split_sentences("just english, no cyrillic") == []


def test_suggestions_lead_with_core_and_drop_done_topics(client):
    d = client.get("/speeches/suggestions").json()["suggestions"]
    assert d and all({"id", "label", "core"} <= set(s) for s in d)
    # core topics are offered before the rest
    assert d[0]["core"] is True
    done = d[0]["id"]
    sid = client.post("/speeches/suggest", json={"id": done}).json()["id"]
    assert client.get(f"/speeches/{sid}").json()["title"]           # generated
    d2 = client.get("/speeches/suggestions").json()["suggestions"]
    assert done not in {s["id"] for s in d2}                        # not offered again


def test_suggest_surprise_me_picks_one(client):
    r = client.post("/speeches/suggest", json={})
    assert r.status_code == 200
    d = client.get(f"/speeches/{r.json()['id']}").json()
    assert d["status"] == "ready" and d["topic"]


def test_suggest_unknown_topic_rejected(client):
    assert client.post("/speeches/suggest", json={"id": "nope"}).status_code == 422


def test_tts_hq_backend_prefers_elevenlabs_when_keyed(monkeypatch):
    import tts_hq
    monkeypatch.delenv("RU_TEST", raising=False)   # exercise real selection logic
    monkeypatch.setattr(tts_hq, "EL_KEY", "sk_fake")
    assert tts_hq.backend() == "elevenlabs" and tts_hq.available() is True
    # no key -> never dead-ends on "elevenlabs"; falls back to the local voice
    monkeypatch.setattr(tts_hq, "EL_KEY", "")
    assert tts_hq.backend("elevenlabs") in ("silero", "none")


def test_tts_hq_ignores_elevenlabs_key_without_the_allow_flag(monkeypatch):
    """ELEVENLABS_API_KEY alone must NOT enable metered TTS — needs the explicit
    RU_TTS_ALLOW_ELEVENLABS opt-in, so it can't silently run up a bill."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_real_looking")
    monkeypatch.delenv("RU_TTS_ALLOW_ELEVENLABS", raising=False)
    import importlib
    import tts_hq
    importlib.reload(tts_hq)
    try:
        assert tts_hq.EL_KEY == "" and tts_hq.has_elevenlabs() is False
    finally:
        importlib.reload(tts_hq)


def test_tts_hq_never_calls_out_under_test():
    import tts_hq
    assert tts_hq.backend() == "none" and tts_hq.backend("elevenlabs") == "none"
