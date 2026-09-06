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
    # homograph/OOV resolver: just hand back the dictionary's own guess unchanged
    monkeypatch.setattr(llm, "stress_resolve",
                        lambda items: [w for w, _ in items])
    monkeypatch.setattr(llm, "explain_lyric",
                        lambda line, lyr, title="", artist="", model=None:
                        {"translation": f"EN: {line}", "gist": "the gist", "notes": []})
    monkeypatch.setattr(llm, "accent_word", lambda w, s="", model=None: w)
    monkeypatch.setattr(llm, "accent_words", lambda items, model=None: [w for w, _ in items])
    monkeypatch.setattr(llm, "dict_form", lambda w, s="", model=None: (w or "") + "́")
    monkeypatch.setattr(llm, "dict_forms",
                        lambda items, model=None: [(w or "") + "́" for w, _ in items])
    monkeypatch.setattr(llm, "stress_forms",
                        lambda items, model=None: [((w or "") + "́", (w or "") + "́")
                                                   for w, _ in items])
    monkeypatch.setattr(llm, "word_family", lambda w, model=None: (w, [w]))
    # fake aspect: any gloss starting "to " is an imperfective with a "с"+word partner

    def _verb_aspect(items, model=None):
        out = []
        for w, g in items:
            if (g or "").strip().lower().startswith("to "):
                out.append({"aspect": "impf", "partner": "с" + (w or ""), "note": None})
            else:
                out.append(None)
        return out
    monkeypatch.setattr(llm, "verb_aspect", _verb_aspect)
    monkeypatch.setattr(llm, "gloss_options",
                        lambda w, df="", s="", cur="", model=None: [f"{w}-sense-a", f"{w}-sense-b"])
    # deterministic fake "learn-first" score: shorter word == more common == higher
    monkeypatch.setattr(llm, "learn_priority",
                        lambda items, model=None: [max(1, 100 - len(w)) for w, _ in items])
    monkeypatch.setattr(llm, "extract_candidates",
                        lambda *a, **k: ([], [], {"calls": 0, "in": 0, "out": 0,
                                                  "think": 0, "cost_est": 0.0}))
    monkeypatch.setattr(llm, "clean_sentences", lambda w, ex, model=None: list(ex))

    # --- speaking drill ---
    monkeypatch.setattr(llm, "speaking_prompt",
                        lambda recent=None, seed=None, level="b1", model=None: {
                            "text": "Tell your friend you can't make it tomorrow.", "hint": "aspect"})

    def _feedback(thought, attempt, level="b1", model=None):
        # canned: one hard + one style correction over the first two words
        words = (attempt or "").split()
        w0, w1 = (words + ["x", "y"])[:2]
        return {
            "meaning": "ok",
            "native": "Извини, завтра не получится.",
            "gloss": "Sorry, tomorrow won't work.",
            "corrections": [
                {"tier": "grammar", "category": "aspect", "original": w0,
                 "corrected": w0 + "л", "explanation": "aspect rule", "severity": "hard"},
                {"tier": "lexical", "category": "lexical", "original": w1,
                 "corrected": "по-" + w1, "explanation": "more natural", "severity": "style"},
            ],
            "diff": [{"c": 1}, {"s": " "}, {"c": 2}, {"s": " " + " ".join(words[2:])}],
            "general": "solid attempt.",
        }
    monkeypatch.setattr(llm, "speaking_feedback", _feedback)

    def _session_cards(items, model=None):
        cards = []
        for i, it in enumerate(items):
            for cr in it.get("corrections", []):
                cards.append({
                    "front": f"say: {it['thought'][:20]}", "back": cr["now"],
                    "alternatives": ([cr["now"] + " (alt)"] if cr["severity"] == "hard" else []),
                    "why": cr["explanation"],
                    "leverage": "high" if cr["severity"] == "hard" else "medium",
                })
        return {"cards": cards[:5]}
    monkeypatch.setattr(llm, "speaking_session_cards", _session_cards)

    # --- grammar drill ---
    import grammar as _grammar

    def _cid_meta(cid):
        cc = _grammar.concept(cid)
        return (cc.cat if cc else "syntax")

    def _one_drill_card(cid, i, lemma="дом"):
        cat = _cid_meta(cid)
        answer = f"Вот {lemma} тут номер {i}."
        return {
            "concept": cid, "lemma": lemma,
            "prompt": f"Use the {cid} rule here, sentence {i}.",
            "given": (["читать / прочитать", lemma] if cat == "aspect" else ["для", lemma]),
            "answer": answer, "target": [lemma],
            "note": f"{cid}: explained.",
            "contrast": "the other aspect here would be …" if cat == "aspect" else "",
        }

    def _drill_cards(concepts, rank_lo=1, rank_hi=1000, words=(), model=None):
        return {"cards": [_one_drill_card(c["id"] if isinstance(c, dict) else c, i)
                          for i, c in enumerate(concepts)]}
    monkeypatch.setattr(llm, "drill_cards", _drill_cards)

    def _drill_concept_cards(concept, n=6, words=(), model=None):
        cid = concept["id"] if isinstance(concept, dict) else str(concept)
        return {"cards": [_one_drill_card(cid, 100 + j) for j in range(n)]}
    monkeypatch.setattr(llm, "drill_concept_cards", _drill_concept_cards)

    def _drill_retest_cards(lemma, gloss, concept, n=3, model=None):
        cid = concept["id"] if isinstance(concept, dict) else str(concept)
        return {"cards": [_one_drill_card(cid, 200 + j, lemma=lemma) for j in range(n)]}
    monkeypatch.setattr(llm, "drill_retest_cards", _drill_retest_cards)

    # --- verbs of motion ---
    def _one_motion_card(spec, i):
        d = {k: str(spec.get(k, "")) for k in ("verb", "aspect", "prefix", "prep", "tense")}
        # mimic the real model, which often echoes the Russian pair back for `verb`
        d["verb"] = spec.get("verb_pair") or d["verb"]
        hl = f"the cat walked into room {i}"
        return {
            "situation": f"You just got home and sat down. Then {hl}.",
            "highlight": hl,
            "given": ["кошка", "комната"],
            "answer": f"кошка вошла в комнату {i}",
            "target": [f"вошла в комнату {i}"],
            "note": "войти = pf; в + accusative for going into an enclosed space.",
            "contrast": "Out of the room: вышла из комнаты.",
            "alts": [
                {"form": "шла в комнату", "why": "still on the way — the scene says she's already in"},
                {"form": "в комнате", "why": "location, but this is motion → в + accusative"},
            ],
            "dims": d,
        }

    def _motion_cards(combos, level=2, model=None):
        return {"cards": [_one_motion_card(c, i) for i, c in enumerate(combos)]}
    monkeypatch.setattr(llm, "motion_cards", _motion_cards)

    def _motion_focus_cards(combos, n=6, level=2, dim_hint=None, model=None):
        if isinstance(combos, dict):
            combos = [combos] * n
        return {"cards": [_one_motion_card(c, 300 + i) for i, c in enumerate(combos[:n])]}
    monkeypatch.setattr(llm, "motion_focus_cards", _motion_focus_cards)

    monkeypatch.setattr(llm, "motion_verb_reference", lambda uni, multi, en, model=None: {
        "summary": f"{uni}/{multi} — {en}.", "uni_use": "one trip now", "multi_use": "habitual",
        "conj": {"present": {"я": [f"{uni}1", f"{multi}1"], "ты": [f"{uni}2", f"{multi}2"]},
                 "past": {"он": [uni + "л", multi + "л"]}, "imperative": {"ты": [uni + "и", multi + "и"]},
                 "future_note": "по- form for the uni"},
        "prefixes": [{"prefix": "при-", "pf": "при" + uni, "impf": "при" + multi, "note": "arrive"},
                     {"prefix": "по-", "pf": "по" + uni, "impf": "", "note": "set off"}],
        "mistakes": [{"wrong": "x", "right": "y", "why": "z"}],
        "examples": [{"ru": f"Я {uni}.", "en": "I go."}]})
    monkeypatch.setattr(llm, "motion_prefix_reference", lambda prefix, meaning, model=None: {
        "what_it_does": f"{prefix} adds: {meaning}.", "usage": "prefix + в/на + acc",
        "exceptions": [{"verb": "подойти", "meaning": "to suit", "example": "мне подходит"}],
        "mistakes": [{"wrong": "a", "right": "b", "why": "c"}],
        "examples": [{"ru": f"Он {prefix}шёл.", "en": "He came."}]})

    # --- chunk deck ---
    def _one_chunk_card(spec, i):
        cid = spec["id"] if isinstance(spec, dict) else str(spec)
        ru_chunk = (spec.get("ru") if isinstance(spec, dict) else "ну…").split(" ")[0].strip("…,")
        ru = f"{ru_chunk}, я думаю, вариант {i}"
        return {"chunk_id": cid,
                "en": f"You know, option {i} — that's my take.",
                "ru": ru, "chunk_en": "you know", "chunk_ru": ru_chunk,
                "gist": "giving a casual take", "gloss": "well", "note": "opener."}

    def _chunk_cards(specs, model=None):
        return {"cards": [_one_chunk_card(s, i) for i, s in enumerate(specs)]}
    monkeypatch.setattr(llm, "chunk_cards", _chunk_cards)

    def _chunk_focus_cards(specs, n=6, fn_hint=None, model=None):
        if isinstance(specs, dict):
            specs = [specs] * n
        return {"cards": [_one_chunk_card(s, 400 + i) for i, s in enumerate(specs[:n])]}
    monkeypatch.setattr(llm, "chunk_focus_cards", _chunk_focus_cards)

    # --- speech lab ---
    monkeypatch.setattr(llm, "speech_draft", lambda topic, extra="", model=None: {
        "title": "How I learned Russian",
        "ru": ["Ну, если коротко — я начал с курса на один семестр.",
               "А потом уже сам смотрел видео и делал карточки каждый день."],
        "en": ["Well, in short — I started with a one-semester course.",
               "And then I just watched videos myself and made flashcards every day."],
        "notes": [{"ru": "если коротко", "why": "opener for a summary"},
                  {"ru": "делал карточки", "why": "made flashcards"}]})
    monkeypatch.setattr(llm, "speech_annotate", lambda ru, model=None: {
        "title": "Pasted speech", "en": ["An English rendering."],
        "notes": [{"ru": "то есть", "why": "reformulating"}],
        "register_flags": []})

    # --- speaking journal ---
    def _journal_analysis(transcript, level="b1", model=None):
        return {
            "segments": [
                {"ru": "Сегодня я пошёл в apartment", "en": "Today I went to the apartment.",
                 "fix": "Сегодня я пошёл в квартиру.",
                 "issues": [{"span": "apartment", "was": "apartment", "now": "квартиру",
                             "tier": "gap", "why": "accusative after в for motion"}]},
                {"ru": "Я купил хлеб и молоко", "en": "I bought bread and milk.",
                 "fix": "", "issues": []},
            ],
            "general": "Good flow. Watch в + accusative for going somewhere.",
            "cards": [
                {"front": "the apartment (going into it)", "back": "в квартиру",
                 "note": "в + acc for motion", "kind": "gap", "leverage": "high",
                 "target": ["квартиру"]},
                {"front": "I bought bread", "back": "Я купил хлеб",
                 "note": "", "kind": "fix", "leverage": "med", "target": ["купил"]},
            ],
        }
    monkeypatch.setattr(llm, "journal_analysis", _journal_analysis)

    # --- flow reading ---
    def _reading_flow_chunk(topic, prompt, summary, rank_est, seed_words=(),
                            grounding="", style="", model=None):
        n = (len(summary or "") % 3) + 1
        seeded = (" " + " ".join(seed_words[:2])) if seed_words else ""
        body = ("Максим медленно шёл по широкой шумной улице и думал о своей работе "
                "и о том большом незнакомом городе вокруг него каждый день. "
                "Люди спешили мимо, а он смотрел на дома и деревья и совсем "
                "не хотел никуда торопиться этим тихим серым утром.")
        return {"text": [f"Это абзац номер {n} про {topic or prompt}.{seeded} " + body],
                "summary": (summary or "") + f" [{n}]"}
    monkeypatch.setattr(llm, "reading_flow_chunk", _reading_flow_chunk)

    # --- conversation partner ---
    monkeypatch.setattr(llm, "convo_open", lambda scenario, prompt="", level="b1", model=None: {
        "persona": "Sergey Petrovich, her uncle, a blunt engineer in his 50s.",
        "situation": "In the kitchen at the dacha during a family dinner.",
        "goal": "explain your job",
        "opening_ru": "Ну, рассказывай, чем занимаешься целый день?",
        "opening_en": "So, tell me, what do you do all day?"})

    def _convo_reply(persona, situation, history, learner_text, level="b1", model=None):
        import re as _re
        eng = _re.findall(r"[A-Za-z]{3,}", learner_text or "")
        gaps = [{"en": w, "ru": w + "-ру"} for w in dict.fromkeys(eng)][:4]
        return {"reply_ru": "Понятно. А ещё что?", "reply_en": "I see. And what else?",
                "gaps": gaps, "errors": [], "drifted": False}
    monkeypatch.setattr(llm, "convo_reply", _convo_reply)

    monkeypatch.setattr(llm, "convo_debrief", lambda persona, situation, history, level="b1", model=None: {
        "summary": "You kept it going and got your point across.",
        "wins": ["stayed in Russian most of the time"],
        "focus": ["case after prepositions of place"],
        "cards": [{"ru": "Я работаю программистом.", "en": "I work as a programmer."}],
        "chunks": ["чем занимаешься"]})
    return llm


@pytest.fixture()
def client(db, stub_llm, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    import speech
    monkeypatch.setattr(main.backup, "snapshot_async", lambda *a, **k: None)
    # background speech jobs run inline so the pasted/generated speech is ready
    monkeypatch.setattr(speech, "_spawn", lambda fn, *a: fn(*a))
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
