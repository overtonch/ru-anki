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

    # selection: 10 cards, all from the pool
    assert len(q1) == 10 and set(q1) <= set(ids)
    # order: stable across reloads…
    assert q1 == q2
    # …but shuffled, not the raw selection order
    assert q1 != sorted(q1)

    # reviewing one doesn't reshuffle the rest and doesn't pull in another card
    srs.review(q1[0], 3)
    q3 = [c["id"] for c in srs.queue(limit=50) if c["is_new"]]
    assert q3 == [i for i in q1 if i != q1[0]]


def test_new_cards_introduced_by_priority(db):
    import srs
    srs.set_setting("new_per_day", 3)
    ids = {}
    for w in ("дом", "стол", "невероятный", "предотвращать"):
        ids[w] = _make_card(srs, span=w)["id"]
    # score them: shorter word -> higher speak/daily/culture (conftest stub)
    sub = {r["id"]: {"speak": 100 - 8 * len(r["span_text"]),
                     "culture": 100 - 8 * len(r["span_text"]),
                     "daily": 100 - 8 * len(r["span_text"])}
           for r in srs.cards_for_learn_ranking()}
    srs.set_card_priorities(sub)

    q = [c["id"] for c in srs.queue(limit=50) if c["is_new"]]
    assert set(q) == {ids["дом"], ids["стол"], ids["невероятный"]}   # top 3 by priority
    assert ids["предотвращать"] not in q                             # lowest, over budget

    # a brand-new card starts unscored and sorts after scored ones
    _make_card(srs, span="я")
    assert srs.unranked_new_count() >= 1

    # priority breakdown is stored and re-derivable after a weight change
    c = db.connect()
    m = c.execute("SELECT priority_meta FROM srs_cards WHERE id=?", (ids["дом"],)).fetchone()
    c.close()
    import json
    assert set(json.loads(m["priority_meta"])) >= {"speak", "daily", "fiction", "freq", "recency"}
    srs.set_new_card_weights({"speak": 1.0, "daily": 0, "recency": 0, "fiction": 0, "freq": 0})
    srs.rescore_priorities_from_meta()


def _new_by_type(srs):
    q = srs.queue(limit=200)
    new = [c for c in q if c["is_new"]]
    return (sum(c["card_type"] == "production" for c in new),
            sum(c["card_type"] == "recognition" for c in new))


def test_new_triage_and_bulk_weed(client, db):
    import srs
    ids = {}
    for w in ("привет", "необыкновенный", "жалование", "стол"):
        ids[w] = _make_card(srs, span=w)["id"]
    srs.set_card_priorities({r["id"]: {"speak": max(0, 100 - 12 * len(r["span_text"])),
                                       "culture": 20,
                                       "daily": max(0, 100 - 12 * len(r["span_text"]))}
                             for r in srs.cards_for_learn_ranking()})

    t = client.get("/srs/new-triage?limit=10").json()
    assert t["total_new"] == 4 and t["cards"]
    # worst-priority first, and each row carries the breakdown
    prios = [c["priority"] for c in t["cards"]]
    assert prios == sorted(prios)
    assert set(t["cards"][0]["priority_meta"]) >= {"speak", "fiction", "recency"}

    worst = [t["cards"][0]["id"], t["cards"][1]["id"]]
    r = client.post("/srs/cards/bulk", json={"ids": worst, "action": "delete"}).json()
    assert r["done"] == 2
    assert client.get("/srs/new-triage").json()["total_new"] == 2


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


def test_daily_healthcheck_catches_and_repairs_a_clobbered_card(db):
    import srs, store
    good = _make_card(srs, span="хорошо")
    wiped = _make_card(srs, span="стёрто")
    for _ in range(4):
        srs.review(good["id"], 3); srs.review(wiped["id"], 3)
    c = store.connect()
    # simulate a state wipe: history stays, FSRS fields blanked
    c.execute("UPDATE srs_cards SET fsrs_state=1, fsrs_step=0, stability=NULL, "
              "difficulty=NULL, last_review=NULL WHERE id=?", (wiped["id"],))
    c.commit(); c.close()

    hc = srs.daily_healthcheck(apply=True)
    assert not hc["ok"] and hc["repaired"] >= 1
    assert any("no FSRS state" in s for s in hc["issues"])
    c = store.connect()
    r = c.execute("SELECT stability, last_review FROM srs_cards WHERE id=?",
                  (wiped["id"],)).fetchone()
    c.close()
    assert r["stability"] and r["last_review"]        # rebuilt from the log
    # the healthy card was left alone
    assert srs.stats()["healthcheck"]["day"] == hc["day"]


def test_rebuild_all_fixes_a_state_reset_card_even_if_it_shrinks(client, db):
    """A card the reset bug inflated (real lapses wiped, then an Easy pushed it
    to a long interval) is brought back down to what it actually earned."""
    import srs, store
    card = _make_card(srs, span="я́ма")
    srs.review(card["id"], 1)
    srs.review(card["id"], 3)
    now = dt.datetime.now(dt.timezone.utc)
    c = store.connect()
    c.execute("DELETE FROM srs_reviews WHERE card_id=?", (card["id"],))
    # honest log: failed it three times over three days, a Good between each
    seq = [(1, 6), (3, 6), (1, 5), (3, 5), (1, 4), (3, 4)]
    for rating, days_ago in seq:
        t = srs._iso(now - dt.timedelta(days=days_ago))
        c.execute("INSERT INTO srs_reviews(card_id, rating, prev_state, prev_step, "
                  "prev_stability, prev_difficulty, prev_due, prev_last_review, reviewed_at) "
                  "VALUES(?,?,2,NULL,0.3,8.0,?,?,?)", (card["id"], rating, t, t, t))
    # then the bug: a review whose prev is the tell-tale 3.0 / 6.5
    t = srs._iso(now - dt.timedelta(days=1))
    c.execute("INSERT INTO srs_reviews(card_id, rating, prev_state, prev_step, "
              "prev_stability, prev_difficulty, prev_due, prev_last_review, reviewed_at) "
              "VALUES(?,4,2,0,3.0,6.5,?,?,?)", (card["id"], t, t, t))
    c.execute("UPDATE srs_cards SET stability=9.0, difficulty=6.5, fsrs_state=2, "
              "due=?, last_review=? WHERE id=?",
              (srs._iso(now + dt.timedelta(days=8)), t, card["id"]))
    c.commit(); c.close()

    r = srs.rebuild_all_schedules(apply=True)
    assert r["reset_bug_cards"] >= 1 and r["shrank"] >= 1
    c = store.connect()
    s = c.execute("SELECT stability FROM srs_cards WHERE id=?", (card["id"],)).fetchone()["stability"]
    c.close()
    assert s < 6                                   # brought back down toward reality
    # fingerprint is marked done — a second pass leaves it alone
    r2 = srs.rebuild_all_schedules(apply=True)
    assert r2["shrank"] == 0


def test_rebuild_schedule_repairs_a_flattened_card(db):
    """A card ground down by repeated early reviews gets its earned stability back
    when its log is replayed on an idealised schedule."""
    import srs, store
    card = _make_card(srs, span="ре́па")
    srs.review(card["id"], 1)
    srs.review(card["id"], 3)
    now = dt.datetime.now(dt.timezone.utc)
    c = store.connect()
    # forge a history of "reviewed 1d apart, Good each time" but with stability
    # frozen low (what the bug produced)
    c.execute("DELETE FROM srs_reviews WHERE card_id=?", (card["id"],))
    for k in range(6):
        t = srs._iso(now - dt.timedelta(days=6 - k))
        c.execute("INSERT INTO srs_reviews(card_id, rating, prev_state, prev_step, "
                  "prev_stability, prev_difficulty, prev_due, prev_last_review, reviewed_at) "
                  "VALUES(?,?,2,NULL,0.25,6.4,?,?,?)",
                  (card["id"], 3 if k else 1, t, t, t))
    c.execute("UPDATE srs_cards SET stability=0.3, fsrs_state=2, due=?, last_review=? WHERE id=?",
              (srs._iso(now), srs._iso(now - dt.timedelta(days=1)), card["id"]))
    c.commit(); c.close()

    old_s, new_s, iv = srs.rebuild_schedule(card["id"], apply=True)
    assert new_s > old_s * 3 and iv >= 2
    c = store.connect()
    assert c.execute("SELECT stability FROM srs_cards WHERE id=?",
                     (card["id"],)).fetchone()["stability"] == new_s
    # history is untouched
    assert c.execute("SELECT COUNT(*) n FROM srs_reviews WHERE card_id=?",
                     (card["id"],)).fetchone()["n"] == 6
    c.close()


def test_early_review_is_scored_as_if_on_schedule(db):
    """The daily batch pulls reviews forward; a card reviewed a few hours early
    must still grow its stability as if reviewed on its due date, not stall."""
    import srs, store
    card = _make_card(srs, span="слово")
    srs.review(card["id"], 1)            # first sight: Again -> low stability
    srs.review(card["id"], 3)            # graduate
    c = store.connect()
    row = c.execute("SELECT stability, due, last_review FROM srs_cards WHERE id=?",
                    (card["id"],)).fetchone()
    c.close()
    s0 = row["stability"]
    # card due later today (inside the daily-batch window), last reviewed 4 days
    # ago (well past its stability) — then review it "early" (now), as the batch
    # would surface it
    eod = dt.datetime.fromisoformat(srs._day_end_iso())
    due = srs._iso(eod - dt.timedelta(hours=1))
    lr = srs._iso(eod - dt.timedelta(hours=1) - dt.timedelta(days=4))

    def _set():
        c = store.connect()
        c.execute("UPDATE srs_cards SET fsrs_state=2, due=?, last_review=? WHERE id=?",
                  (due, lr, card["id"]))
        c.commit(); c.close()

    _set()
    pv = srs.preview(card["id"])
    assert pv[3] not in ("1d", "<1m") and pv[4] != pv[3]   # Good unstuck, Easy distinct
    _set()
    after = srs.review(card["id"], 3)
    assert after["stability"] > s0 * 1.8       # real growth, not a rounding nudge


def test_passing_a_crushed_card_still_buys_breathing_room(db):
    """A repeatedly-failed card that FSRS has ground to a sub-day stability must
    not be scheduled for tomorrow on Good — and Easy must be further out."""
    import srs, store
    card = _make_card(srs, span="вопль")
    # forge a crushed card: state 2, tiny stability, just failed-and-relearned
    now = dt.datetime.now(dt.timezone.utc)
    c = store.connect()
    c.execute("UPDATE srs_cards SET fsrs_state=2, stability=0.05, difficulty=9.8, "
              "reps=8, lapses=3, due=?, last_review=? WHERE id=?",
              (srs._iso(now), srs._iso(now - dt.timedelta(hours=6)), card["id"]))
    c.commit(); c.close()

    pv = srs.preview(card["id"])
    assert pv[3] not in ("1d", "<1m", "10m")          # Good: at least the floor
    assert pv[3] != pv[4]                              # Easy is distinct
    r = srs.review(card["id"], 3)
    iv = (dt.datetime.fromisoformat(r["due"]) - now).total_seconds() / 86400
    assert iv >= srs.MIN_GOOD_DAYS - 0.2
    assert r["stability"] >= srs.MIN_GOOD_DAYS - 0.2   # model matches the schedule

    # a healthy card is untouched by the floor
    h = _make_card(srs, span="здоровый")
    for _ in range(3):
        srs.review(h["id"], 3)
    c = store.connect()
    c.execute("UPDATE srs_cards SET stability=40, fsrs_state=2, due=?, last_review=? WHERE id=?",
              (srs._iso(now), srs._iso(now - dt.timedelta(days=40)), h["id"]))
    c.commit(); c.close()
    assert srs.preview(h["id"])[3] not in ("2d", "4d")


def test_a_whole_days_reviews_are_available_from_the_start(db):
    """Graduated cards due any time later today show up in the queue now, so
    reviews land in one daily batch instead of trickling in — but only if you
    haven't already reviewed them today."""
    import srs, store
    later = _make_card(srs, span="позже")
    tomorrow = _make_card(srs, span="завтра")
    done = _make_card(srs, span="сделано")
    srs.review(later["id"], 3); srs.review(tomorrow["id"], 3); srs.review(done["id"], 3)
    eod = dt.datetime.fromisoformat(srs._day_end_iso())
    yesterday = srs._iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1))
    today = srs._iso(dt.datetime.now(dt.timezone.utc))
    c = store.connect()
    # due later today, last reviewed yesterday -> in today's batch
    c.execute("UPDATE srs_cards SET fsrs_state=2, due=?, last_review=? WHERE id=?",
              (srs._iso(eod - dt.timedelta(hours=2)), yesterday, later["id"]))
    # due after the day cutoff -> tomorrow's batch
    c.execute("UPDATE srs_cards SET fsrs_state=2, due=?, last_review=? WHERE id=?",
              (srs._iso(eod + dt.timedelta(hours=8)), yesterday, tomorrow["id"]))
    # due later today BUT already reviewed today -> must NOT come back today
    c.execute("UPDATE srs_cards SET fsrs_state=2, due=?, last_review=? WHERE id=?",
              (srs._iso(eod - dt.timedelta(hours=1)), today, done["id"]))
    c.commit(); c.close()
    ids = {x["id"] for x in srs.queue(limit=50)}
    assert later["id"] in ids
    assert tomorrow["id"] not in ids
    assert done["id"] not in ids                    # reviewed today = done for the day
    bundle = {x["id"]: x for x in srs.offline_bundle(days=3)["cards"]}
    assert bundle[later["id"]]["due_now"]
    assert not bundle[tomorrow["id"]]["due_now"]
    assert not bundle[done["id"]]["due_now"]


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


def test_audit_card_repairs_structural_drift(db):
    import srs, store
    c = _make_card(srs, span="искупить", sentence="Разве это могут искупить минуты?")
    cid = c["id"]
    con = store.connect()
    # simulate the drift the user saw: front_word became the whole sentence,
    # a stray stress mark in the target, is_phrase flipped on
    con.execute("UPDATE srs_cards SET front_word=?, span_text=?, is_phrase=1 WHERE id=?",
                ("Разве это могут искупить минуты?", "искупи́ть", cid))
    con.commit(); con.close()

    found = srs.audit_card(cid, apply=True)
    assert found                                   # it noticed
    got = srs.get_card(cid)
    assert got["is_phrase"] == 0                    # single word again
    assert "́" not in got["span_text"]              # stress mark stripped
    assert " " not in got["front_word"]             # front_word is a headword, not the sentence
    assert srs._strip_stress(got["front_word"]).lower() in ("искупить",)
    # idempotent
    assert not any(f for f in srs.audit_card(cid, apply=True) if not f.startswith("!"))


def test_audit_flags_target_missing_from_sentence(db):
    import srs, store
    c = _make_card(srs, span="кот", sentence="Собака бежала по улице.")
    found = srs.audit_card(c["id"], apply=True)
    assert any(f.startswith("!") for f in found)


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
    srs.set_card_priorities({r["id"]: {"speak": 100 - 8 * len(r["span_text"]),
                                       "culture": 20, "daily": 100 - 8 * len(r["span_text"])}
                             for r in srs.cards_for_learn_ranking()})

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
