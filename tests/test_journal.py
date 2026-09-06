"""Speaking journal — record → transcribe → analyse → make cards.
Whisper + the LLM analysis are stubbed; the background pipeline runs inline here.
"""
import io

import pytest


@pytest.fixture()
def stub_whisper(monkeypatch):
    import whisper_rt
    monkeypatch.setattr(whisper_rt, "transcribe",
                        lambda src, dur=0, progress=None, language="ru":
                        [(0.0, 3.0, "Сегодня я пошёл в apartment"),
                         (3.0, 6.0, "Я купил хлеб и молоко")])
    return whisper_rt


@pytest.fixture()
def sync_journal(monkeypatch):
    """Run the background transcribe→analyse pipeline inline."""
    import journal
    monkeypatch.setattr(journal, "_start", journal._run)
    return journal


def _rec(client, secs_bytes=6000):
    return client.post("/journal/sessions",
                       files={"file": ("j.webm", io.BytesIO(b"\x00" * secs_bytes), "audio/webm")})


def test_record_transcribe_analyse(client, stub_whisper, sync_journal):
    r = _rec(client)
    assert r.status_code == 200
    sid = r.json()["id"]

    d = client.get(f"/journal/sessions/{sid}").json()
    assert d["status"] == "done"
    assert "apartment" in d["transcript"]
    assert len(d["segments"]) == 2
    assert d["segments"][0]["fix"]                      # first chunk had an error
    assert d["general"]
    assert d["cards"] and all(c["front"] and c["back"] for c in d["cards"])
    assert d["cards"][0]["kind"] == "gap"


def test_short_recording_rejected(client):
    r = client.post("/journal/sessions",
                    files={"file": ("j.webm", io.BytesIO(b"\x00" * 50), "audio/webm")})
    assert r.status_code == 422


def test_make_cards_from_a_session(client, stub_whisper, sync_journal):
    sid = _rec(client).json()["id"]
    d = client.get(f"/journal/sessions/{sid}").json()

    made = client.post(f"/journal/sessions/{sid}/cards",
                       json={"cards": d["cards"]}).json()
    assert made["created"] == 2

    q = client.get("/srs/queue").json()["cards"]
    prod = [c for c in q if c.get("card_type") == "production"]
    assert len(prod) >= 2
    # journal cards carry the target highlight + a readable kind
    got = [c for c in prod if c["target"] == "в квартиру"]
    assert got and got[0]["hit"] == ["квартиру"] and got[0]["kind"] == "journal"
    assert client.get("/srs/stats").json()["orphans"] == 0


def test_history_lists_sessions(client, stub_whisper, sync_journal):
    _rec(client)
    _rec(client)
    lst = client.get("/journal/sessions").json()["sessions"]
    assert len(lst) == 2
    assert all(s["status"] == "done" for s in lst)


def test_unknown_session_404(client):
    assert client.get("/journal/sessions/999").status_code == 404
    assert client.post("/journal/sessions/999/cards", json={"cards": []}).status_code == 404
