"""One-tap "change the translation word" on the review screen."""


def _card(srs, span="спор", gloss="argument", alt="dispute; quarrel; controversy"):
    c = srs.create_card("Между ними вышел спор.", span, span, False, gloss)
    srs.update_card(c["id"], alt_meanings=alt)
    return srs.get_card(c["id"])


def test_study_view_offers_alts_split_out_of_alt_meanings(client, db):
    import srs
    c = _card(srs)
    d = client.get(f"/srs/cards/{c['id']}").json()
    assert d["tr_alts"] == ["dispute", "quarrel", "controversy"]   # split, deduped vs primary


def test_swap_promotes_a_gloss_and_keeps_the_old_one(client, db):
    import srs
    c = _card(srs)
    r = client.post(f"/srs/cards/{c['id']}/translation", json={"translation": "quarrel"})
    assert r.status_code == 200
    d = r.json()
    assert d["translation"] == "quarrel"
    # old primary folded back into alt_meanings, chosen one removed from it
    assert "argument" in d["alt_meanings"]
    assert "quarrel" not in d["tr_alts"]
    assert "argument" in d["tr_alts"]

    stored = srs.get_card(c["id"])
    assert stored["translation"] == "quarrel"


def test_swap_to_a_brand_new_custom_gloss(client, db):
    import srs
    c = _card(srs)
    d = client.post(f"/srs/cards/{c['id']}/translation",
                    json={"translation": "falling-out"}).json()
    assert d["translation"] == "falling-out"
    assert "argument" in d["alt_meanings"] and "dispute" in d["alt_meanings"]


def test_empty_translation_rejected(client, db):
    import srs
    c = _card(srs)
    assert client.post(f"/srs/cards/{c['id']}/translation",
                       json={"translation": "  "}).status_code == 422


def test_translation_options_endpoint(client, db, monkeypatch):
    import srs, main
    monkeypatch.setattr(main.llm, "gloss_options",
                        lambda *a, **k: ["row", "spat", "wrangle"])
    c = _card(srs)
    r = client.get(f"/srs/cards/{c['id']}/translation-options").json()
    assert r["options"] == ["row", "spat", "wrangle"]


def test_swap_404_for_missing_card(client, db):
    assert client.post("/srs/cards/999999/translation",
                       json={"translation": "x"}).status_code == 404
