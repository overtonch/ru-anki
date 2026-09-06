"""Verb-aspect tags on vocab cards — lemma-keyed cache, backfill, card payload.
llm.verb_aspect is stubbed (see conftest): a "to …" gloss → impf with a с-partner."""


def _card(srs, span, gloss, sentence="Это тут."):
    return srs.create_card(sentence, span, span, False, gloss)


def test_backfill_tags_verbs_and_caches_negatives(client, db):
    import srs, aspect
    v = _card(srs, "делать", "to do")
    n = _card(srs, "стол", "table")

    r = client.post("/srs/backfill-aspects").json()
    assert r["queued"] >= 1
    # endpoint runs the backfill as a background task; the test client executes it inline

    row_v = aspect.get("делать")
    assert row_v and row_v["is_verb"] == 1 and row_v["aspect"] == "impf"
    assert row_v["partner"] == "сделать"
    row_n = aspect.get("стол")
    assert row_n and row_n["is_verb"] == 0          # negative is cached too

    del v, n


def test_card_payload_carries_aspect_block(client, db):
    import srs, aspect
    c = _card(srs, "читать", "to read")
    aspect.resolve([srs.get_card(c["id"])])

    d = client.get(f"/srs/cards/{c['id']}").json()
    a = d["aspect"]
    assert a["aspect"] == "impf" and a["label"] == "imperfective"
    assert a["other_label"] == "perfective" and a["partner"] == "считать"
    assert a["concept"] == "aspect-core"


def test_non_verb_card_has_no_aspect_block(client, db):
    import srs, aspect
    c = _card(srs, "книга", "book")
    aspect.resolve([srs.get_card(c["id"])])
    assert client.get(f"/srs/cards/{c['id']}").json()["aspect"] is None


def test_pending_shrinks_after_resolve(client, db):
    import srs, aspect
    _card(srs, "бежать", "to run")
    before = aspect.pending_count()
    assert before >= 1
    aspect.backfill()
    assert aspect.pending_count() == 0


def test_second_card_same_lemma_reuses_cache_no_llm(client, db, monkeypatch):
    import srs, aspect, llm
    c1 = _card(srs, "писать", "to write")
    aspect.resolve([srs.get_card(c1["id"])])

    calls = []
    monkeypatch.setattr(llm, "verb_aspect",
                        lambda items, model=None: calls.append(items) or [None] * len(items))
    c2 = _card(srs, "писать", "to write", sentence="Я люблю писать.")
    # a fresh row for the same lemma should not need another model call
    assert aspect.for_card(srs.get_card(c2["id"]))["partner"] == "списать"
    aspect.backfill()
    assert calls == []
