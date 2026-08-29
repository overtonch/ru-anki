"""Test fixtures: a throwaway SQLite DB, the FastAPI app with every boot-time
background job disabled, and the LLM engine stubbed so nothing shells out to
`claude -p`. Fast and free — safe to run before every deploy."""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))

# must be set before store / main import
_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMPDB.close()
os.environ["VOCAB_DB"] = _TMPDB.name
os.environ["RU_TEST"] = "1"
os.environ["RU_MEDIA_DIR"] = tempfile.mkdtemp(prefix="ru-anki-test-media-")

import pytest  # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    try:
        os.unlink(_TMPDB.name)
    except OSError:
        pass


@pytest.fixture()
def db():
    """The schema in the temp DB, emptied of every row before each test."""
    import store
    store.init_db()
    con = store.connect()
    con.execute("PRAGMA foreign_keys=OFF")
    tables = [r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        if t in ("freq", "dict_ru", "stoplist"):     # reference data — keep
            continue
        con.execute(f"DELETE FROM {t}")
    con.execute("DELETE FROM sqlite_sequence")
    con.commit()
    con.close()
    if hasattr(store, "_LEMMA_IDX"):
        store._LEMMA_IDX.clear()
    return store


@pytest.fixture()
def stub_llm(monkeypatch):
    """Replace every LLM entry point with a canned, deterministic response."""
    import llm

    def _span(sentence, span, model=None):
        return {"span_text": span, "is_phrase": " " in span,
                "translation": f"[{span}]", "sentence": sentence or span,
                "stressed": span, "dict_form": span + "́"}

    monkeypatch.setattr(llm, "translate_span", _span)
    monkeypatch.setattr(llm, "translate_passage", lambda t, model=None: f"EN: {t}")
    monkeypatch.setattr(llm, "explain_lyric",
                        lambda line, lyr, title="", artist="", model=None:
                        {"translation": f"EN: {line}", "gist": "the gist", "notes": []})
    monkeypatch.setattr(llm, "accent_word", lambda w, s="", model=None: w)
    monkeypatch.setattr(llm, "accent_words", lambda items, model=None: [w for w, _ in items])
    monkeypatch.setattr(llm, "dict_form", lambda w, s="", model=None: (w or "") + "́")
    monkeypatch.setattr(llm, "dict_forms",
                        lambda items, model=None: [(w or "") + "́" for w, _ in items])
    monkeypatch.setattr(llm, "word_family", lambda w, model=None: (w, [w]))
    monkeypatch.setattr(llm, "extract_candidates",
                        lambda *a, **k: ([], [], {"calls": 0, "in": 0, "out": 0,
                                                  "think": 0, "cost_est": 0.0}))
    monkeypatch.setattr(llm, "clean_sentences", lambda w, ex, model=None: list(ex))
    return llm


@pytest.fixture()
def client(db, stub_llm, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(main.backup, "snapshot_async", lambda *a, **k: None)
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def seeded_video(db):
    """One kind='video' with a short VTT transcript, indexed."""
    vtt = ("WEBVTT\n\n"
           "00:00:00.000 --> 00:00:03.000\nОн блефовал за карточным столом.\n\n"
           "00:00:03.000 --> 00:00:06.000\nЗатем он молча ушёл в ночь.\n\n"
           "00:00:06.000 --> 00:00:09.000\nВетер трепал полы его пальто.\n")
    vid = db.upsert_video("http://example.test/v1", "Test Video", "manual", "ru", vtt)
    db.replace_subtitle_lines(vid, [("00:00:00", "Он блефовал за карточным столом."),
                                    ("00:00:03", "Затем он молча ушёл в ночь."),
                                    ("00:00:06", "Ветер трепал полы его пальто.")])
    return vid
