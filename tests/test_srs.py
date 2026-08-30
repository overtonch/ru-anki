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


def test_preview_accepts_id_or_row(db):
    import srs
    c = _make_card(srs)
    by_id = srs.preview(c["id"])
    by_row = srs.preview(srs.get_card(c["id"]))
    assert set(by_id) == {1, 2, 3, 4}
    assert by_id == by_row


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


def test_update_card_rederives_content(db):
    import srs
    c = _make_card(srs, span="слово", sentence="Первое слово тут.")
    upd = srs.update_card(c["id"], span_text="слово",
                          sentence="Другое слово здесь.", translation="a word")
    assert upd["sentence"] == "Другое слово здесь."
    assert upd["translation"] == "a word"
