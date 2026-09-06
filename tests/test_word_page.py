"""The word page — "every place this word is said", now across the whole word
family and all indexed content, with each hit trimmed to a readable window."""


def _index(db, vid, lines, kind=None):
    db.replace_subtitle_lines(vid, lines)
    if kind:
        c = db.connect()
        c.execute("UPDATE videos SET kind=? WHERE id=?", (kind, vid))
        c.commit(); c.close()


def test_occurrences_span_every_form_and_the_whole_family(client, db):
    v1 = db.upsert_video("http://x.test/a", "Лекция", "video", "ru", "WEBVTT\n")
    v2 = db.upsert_video("http://x.test/b", "Роман", "text", "ru", "WEBVTT\n")
    _index(db, v1, [("00:00:01", "Он открыто презирал их всех."),
                    ("00:00:05", "Ветер трепал флаги.")], kind="video")
    _index(db, v2, [("00:00:00", "Она смотрела на всё это с холодным презрением."),
                    ("00:00:04", "Губы сложились в презрительную усмешку.")], kind="text")
    db.set_word_family("презр", ["презирать", "презрение", "презрительный"])

    d = client.get("/words/презирать").json()
    assert set(d["family"]) == {"презрение", "презрительный"}
    # by_lemma аdds up across BOTH videos and all forms
    assert d["by_lemma"]["презирать"] == 1
    assert d["by_lemma"]["презрение"] == 1
    assert d["by_lemma"]["презрительный"] == 1
    titles = {v["title"] for v in d["videos"]}
    assert titles == {"Лекция", "Роман"}
    assert {v["kind"] for v in d["videos"]} == {"video", "text"}   # book content included
    hits = [h for v in d["videos"] for h in v["hits"]]
    assert any(h["w"] == "презрение" and "**презрением**" in h["text"] for h in hits)
    assert any(h["w"] == "презрительный" and "**презрительную**" in h["text"] for h in hits)
    assert any(h["w"] == "презирать" and "**презирал**" in h["text"] for h in hits)


def test_family_false_limits_to_the_exact_lemma(client, db):
    v = db.upsert_video("http://x.test/c", "V", "video", "ru", "WEBVTT\n")
    _index(db, v, [("00:00:00", "Он презирал ложь."),
                   ("00:00:03", "В её презрении не было злобы.")])
    db.set_word_family("презр", ["презирать", "презрение"])

    full = client.get("/words/презирать").json()
    assert set(full["by_lemma"]) == {"презирать", "презрение"}

    just = client.get("/words/презирать?family=false").json()
    assert set(just["by_lemma"]) == {"презирать"}


def test_long_book_paragraph_hit_is_trimmed_to_a_window(client, db):
    v = db.upsert_video("http://x.test/d", "Книга", "text", "ru", "WEBVTT\n")
    para = ("Много лет спустя, стоя у стены в ожидании расстрела, " * 6
            + "полковник вспомнил тот далёкий вечер, когда отец взял его посмотреть на лёд, "
            + "и все презирали эту затею, " + "и снова много подробностей повсюду. " * 6)
    _index(db, v, [("00:00:00", para)])

    d = client.get("/words/презирать").json()
    h = d["videos"][0]["hits"][0]
    assert "**презирали**" in h["text"]
    assert h["text"].startswith("…") and h["text"].rstrip().endswith("…")
    assert len(h["text"]) < len(para) / 2
