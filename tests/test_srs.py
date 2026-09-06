"""In-app SRS engine — scheduling, queue selection, list filters, deletes."""
import datetime as dt


def _make_card(srs, span="слово", sentence="Это слово тут.", video_id=None, ts=None):
    return srs.create_card(sentence, span, span, False, "gloss",
                           video_id=video_id, timestamp=ts)


def test_new_card_good_graduates_past_ten_minutes(db):
    import srs
    c = _make_card(srs)
    after = srs.review(c["id"], 3)
    due = dt.datetime.fromisoformat(after["due"])
    assert due - dt.datetime.now(dt.timezone.utc) > dt.timedelta(hours=12)


def test_again_keeps_card_in_a_minute(db):
    import srs
    c = _make_card(srs)
    after = srs.review(c["id"], 1)
    delta = dt.datetime.fromisoformat(after["due"]) - dt.datetime.now(dt.timezone.utc)
    assert dt.timedelta(seconds=0) < delta < dt.timedelta(minutes=3)


def test_leech_gets_parked_after_repeated_failures(db):
    import srs, store
    c = _make_card(srs, span="трудное")
    ever_leeched = False
    for _ in range(srs.LEECH_LAPSES + 3):
        ever_leeched |= srs.review(c["id"], 1)["leeched"]   # fail it over and over
    assert ever_leeched is True
    row = store.connect().execute("SELECT suspended, lapses FROM srs_cards WHERE id=?",
                                  (c["id"],)).fetchone()
    assert row["suspended"] == 1 and row["lapses"] >= srs.LEECH_LAPSES
    assert not any(x["id"] == c["id"] for x in srs.queue())    # parked, not due
    assert srs.stats()["leeches"] >= 1


def test_review_endpoint_returns_fresh_preview_and_due(client, db):
    import srs
    c = _make_card(srs)
    r = client.post(f"/srs/cards/{c['id']}/review", json={"rating": 1}).json()
    assert r["card"]["due"] and r["card"]["preview"]
    assert r["leeched"] is False
    # after "Again" the Good interval on the button is short (~1d), not the 2d a
    # brand-new Good would give
    good = r["card"]["preview"]["3"]
    assert good in ("1d", "2d") or good.endswith("m")


def test_new_cards_picked_in_order_shown_shuffled_stably(db):
    import srs
    srs.set_setting("new_per_day", 10)
    ids = [_make_card(srs, span=f"слово{i}")["id"] for i in range(30)]

    q1 = [c["id"] for c in srs.queue(limit=50) if c["is_new"]]
    q2 = [c["id"] for c in srs.queue(limit=50) if c["is_new"]]

    # selection: the first 10 by creation order (not the later 20)
    assert set(q1) == set(ids[:10])
    # order: stable across reloads…
    assert q1 == q2
    # …but shuffled, not creation order (30!/(20!) makes a match astronomically unlikely)
    assert q1 != ids[:10]

    # reviewing one doesn't reshuffle the rest and doesn't pull in card #11
    srs.review(q1[0], 3)
    q3 = [c["id"] for c in srs.queue(limit=50) if c["is_new"]]
    assert q3 == [i for i in q1 if i != q1[0]]


def test_new_cards_introduced_by_learn_score(db):
    import srs
    srs.set_setting("new_per_day", 3)
    ids = {}
    for w in ("дом", "стол", "невероятный", "предотвращать"):
        ids[w] = _make_card(srs, span=w)["id"]
    # rank: shorter word -> higher score (conftest stub)
    scores = {r["id"]: (100 - len(r["span_text"]))
              for r in srs.cards_for_learn_ranking()}
    srs.set_learn_scores(scores)

    q = [c["id"] for c in srs.queue(limit=50) if c["is_new"]]
    assert set(q) == {ids["дом"], ids["стол"], ids["невероятный"]}   # top 3 by score
    assert ids["предотвращать"] not in q                             # lowest score, over budget

    # a brand-new card starts unscored and sorts last until the daily pass runs
    _make_card(srs, span="я")
    assert srs.unranked_new_count() >= 1


def _new_by_type(srs):
    q = srs.queue(limit=200)
    new = [c for c in q if c["is_new"]]
    return (sum(c["card_type"] == "production" for c in new),
            sum(c["card_type"] == "recognition" for c in new))


def test_daily_new_mix_is_20_prod_30_rec(db):
    import srs
    for i in range(40):
        _make_card(srs, span=f"слово{i}")
        srs.create_production_card(f"say thing {i}", f"Фраза номер {i}.")
    prod, rec = _new_by_type(srs)
    assert (prod, rec) == (20, 30)                       # default 50 = 20 + 30


def test_production_shortfall_backfills_with_recognition(db):
    import srs
    for i in range(60):
        _make_card(srs, span=f"слово{i}")
    for i in range(5):
        srs.create_production_card(f"say {i}", f"Фраза {i}.")
    prod, rec = _new_by_type(srs)
    assert prod == 5 and rec == 45 and prod + rec == 50


def test_recognition_shortfall_backfills_with_production(db):
    import srs
    for i in range(8):
        _make_card(srs, span=f"слово{i}")
    for i in range(60):
        srs.create_production_card(f"say {i}", f"Фраза {i}.")
    prod, rec = _new_by_type(srs)
    assert rec == 8 and prod == 42 and prod + rec == 50


def test_introduced_production_counts_against_the_daily_budget(db):
    import srs
    for i in range(40):
        _make_card(srs, span=f"слово{i}")
        srs.create_production_card(f"say {i}", f"Фраза {i}.")
    q = srs.queue(limit=200)
    prod_new = [c for c in q if c["is_new"] and c["card_type"] == "production"]
    srs.review(prod_new[0]["id"], 3)                     # introduce one production card
    prod, rec = _new_by_type(srs)
    assert prod + rec == 49                              # total budget dropped by one
    assert prod == 19


def test_preview_accepts_id_or_row(db):
    import srs
    c = _make_card(srs)
    by_id = srs.preview(c["id"])
    by_row = srs.preview(srs.get_card(c["id"]))
    assert set(by_id) == {1, 2, 3, 4}
    assert by_id == by_row


def test_preview_survives_naive_timestamps_str_and_datetime(db):
    """A naive last_review — a string OR an already-parsed datetime object —
    used to 500 the whole queue via fsrs's aware-datetime arithmetic."""
    import srs, datetime as dt
    row = {"id": 1, "fsrs_state": 2, "fsrs_step": None, "stability": 12.0,
           "difficulty": 5.0, "due": "2026-09-01 12:00:00",
           "last_review": "2026-08-25 12:00:00", "card_type": "recognition"}
    assert set(srs.preview(row)) == {1, 2, 3, 4}          # naive string
    row["last_review"] = dt.datetime(2026, 8, 25, 12, 0)  # naive datetime object
    assert set(srs.preview(row)) == {1, 2, 3, 4}
    assert srs._aware(dt.datetime(2026, 1, 1)).endswith("+00:00")


def test_queue_excludes_future_but_bundle_includes_them(db):
    import srs
    a = _make_card(srs, span="один")
    b = _make_card(srs, span="два")
    srs.review(a["id"], 3)
    ids_queue = {c["id"] for c in srs.queue(limit=50)}
    assert a["id"] not in ids_queue
    assert b["id"] in ids_queue
    bundle_ids = {c["id"] for c in srs.offline_bundle(days=7)["cards"]}
    assert a["id"] in bundle_ids
    assert b["id"] in bundle_ids


def test_a_whole_days_reviews_are_available_from_the_start(db):
    """Graduated cards due any time later today show up in the queue now, so
    reviews land in one daily batch instead of trickling in."""
    import srs, store
    later = _make_card(srs, span="позже")
    tomorrow = _make_card(srs, span="завтра")
    srs.review(later["id"], 3)
    srs.review(tomorrow["id"], 3)
    eod = dt.datetime.fromisoformat(srs._day_end_iso())
    c = store.connect()
    # one due in a few hours (still today), one due well after the day cutoff
    c.execute("UPDATE srs_cards SET fsrs_state=2, due=? WHERE id=?",
              (srs._iso(eod - dt.timedelta(hours=2)), later["id"]))
    c.execute("UPDATE srs_cards SET fsrs_state=2, due=? WHERE id=?",
              (srs._iso(eod + dt.timedelta(hours=8)), tomorrow["id"]))
    c.commit(); c.close()
    ids = {x["id"] for x in srs.queue(limit=50)}
    assert later["id"] in ids and tomorrow["id"] not in ids
    assert srs.stats()["due"] >= 1
    bundle = {x["id"]: x for x in srs.offline_bundle(days=3)["cards"]}
    assert bundle[later["id"]]["due_now"] and not bundle[tomorrow["id"]]["due_now"]


def test_refresher_includes_every_fumbled_card_not_just_the_far_future_ones(db):
    """Regression: the fumbled source used to require due > (today + ~2d), which
    silently dropped most of a heavy week's 'Again's."""
    import srs, store
    soon = _make_card(srs, span="скоро")
    far = _make_card(srs, span="далеко")
    plain = _make_card(srs, span="просто")
    for cid in (soon["id"], far["id"]):
        srs.review(cid, 1)          # fumble both
    srs.review(far["id"], 3)        # far one recovered → due days out
    # `soon` is still in a learning step (due minutes from now)
    got = {c["id"] for c in srs.missed_review_cards(days=7)}
    assert soon["id"] in got        # fumbled + due soon — was being hidden
    assert far["id"] in got         # fumbled + due far
    assert plain["id"] not in got   # never fumbled, never skipped

    # a card that is due RIGHT NOW is left to the live queue, not the refresher
    c = store.connect()
    c.execute("UPDATE srs_cards SET due=? WHERE id=?",
              (srs._iso(srs._utc() - __import__('datetime').timedelta(minutes=1)), soon["id"]))
    c.commit(); c.close()
    assert soon["id"] not in {c["id"] for c in srs.missed_review_cards(days=7)}


def test_list_filter_orphan(db, seeded_video):
    import srs
    _make_card(srs, span="сирота", video_id=None)
    _make_card(srs, span="дом", video_id=seeded_video)
    got = srs.list_cards(filt="orphan")
    assert [x["span_text"] for x in got["cards"]] == ["сирота"]
    assert srs.delete_orphan_cards() == 1
    assert srs.list_cards(filt="orphan")["total"] == 0
    assert srs.list_cards()["total"] == 1


def test_delete_cards_for_video(db, seeded_video):
    import srs
    _make_card(srs, span="ночь", video_id=seeded_video)
    _make_card(srs, span="ветер", video_id=seeded_video)
    assert srs.delete_cards_for_video(seeded_video) == 2
    assert srs.list_cards()["total"] == 0


def test_stats_defaults(db):
    import srs
    s = srs.stats()
    assert s["review_pace_s"] == 6.0
    assert s["total"] == 0
    assert s["due"] == 0
    assert s["new_backlog"] == 0 and s["new_runway_days"] == 0
    assert s["next_batch_median_rank"] is None


def test_stats_new_reserve_and_next_batch_rank(db):
    import srs, store
    c = store.connect()
    c.executemany("INSERT OR REPLACE INTO freq(normalized_text, rank) VALUES(?,?)",
                  [("альфа", 2000), ("бета", 4000), ("гамма", 6000),
                   ("дельта", 8000), ("эпсилон", 60000)])
    c.commit(); c.close()
    srs.set_setting("new_per_day", 3)
    for w in ("альфа", "бета", "гамма", "дельта", "эпсилон"):
        srs.create_card(f"Вот {w} тут.", w, w, False, w)

    s = srs.stats()
    assert s["new_backlog"] == 5
    assert s["new_per_day"] == 3
    assert s["new_runway_days"] == 2          # ceil(5 / 3)
    # next batch is the first 3 in serving order; median of their ranks
    assert s["next_batch_median_rank"] in (2000, 4000, 6000)


def test_update_card_rederives_content(db):
    import srs
    c = _make_card(srs, span="слово", sentence="Первое слово тут.")
    upd = srs.update_card(c["id"], span_text="слово",
                          sentence="Другое слово здесь.", translation="a word")
    assert upd["sentence"] == "Другое слово здесь."
    assert upd["translation"] == "a word"


def test_editing_a_word_card_into_a_phrase_updates_the_front(db):
    import srs
    card = srs.create_card("Заглянуть ей под капот и посмотреть.", "капот", "капот",
                           False, "hood")
    # it was carded as a single word; front is the word
    assert card["is_phrase"] == 0 and card["front_word"].startswith("капот")

    upd = srs.update_card(card["id"], span_text="под капот")
    assert upd["is_phrase"] == 1
    assert upd["span_text"] == "под капот"
    assert upd["normalized_text"] == "под капот"
    assert upd["front_word"] == "под капот"          # <- the bug: front used to stay "капот"
    assert upd["accented"] is None and upd["dict_accented"] is None
