"""Pure-function tests — no DB, no network, no LLM."""
import subs
import music
import db as ru_db
import tts
import lrcfix
import llm


# ---------------------------------------------------------------- subs.py

VTT = """WEBVTT

00:00:01.000 --> 00:00:04.000
Привет, как дела?

00:00:04.000 --> 00:00:07.000
У меня всё хорошо.

00:00:04.000 --> 00:00:07.000
У меня всё хорошо.
"""

SRT = """1
00:00:01,000 --> 00:00:03,500
Первая строка.

2
00:00:03,500 --> 00:00:06,000
Вторая строка.
"""


def test_caption_cues_vtt():
    cues = subs.caption_cues(VTT)
    assert [c["text"] for c in cues] == ["Привет, как дела?", "У меня всё хорошо."]
    assert cues[0]["s"] == 1.0
    assert cues[0]["e"] == 4.0          # display end = next cue start


def test_caption_cues_srt():
    cues = subs.caption_cues(SRT)
    assert [c["text"] for c in cues] == ["Первая строка.", "Вторая строка."]
    assert cues[1]["s"] == 3.5


def test_new_text_cues_dedupes_plain():
    rows = subs.new_text_cues(VTT)
    assert [t for _, t in rows] == ["Привет, как дела?", "У меня всё хорошо."]


def test_extraction_text_has_timestamps():
    out = subs.extraction_text(VTT)
    assert out.startswith("[00:00:01]")


# ---------------------------------------------------------------- music.py

def test_parse_artist_title_dash():
    assert music.parse_artist_title("Земфира — Искала (Official Video)") == ("Земфира", "Искала")
    assert music.parse_artist_title("Кино - Кукушка [Lyrics]") == ("Кино", "Кукушка")


def test_parse_artist_title_quotes_and_fallback():
    assert music.parse_artist_title('Сплин «Выхода нет»') == ("Сплин", "Выхода нет")
    assert music.parse_artist_title("Кукушка", "Кино") == ("Кино", "Кукушка")


def test_is_apple_music_and_track_id():
    u = "https://music.apple.com/us/album/кукушка/1333012313?i=1333012320"
    assert music.is_apple_music(u)
    assert music.apple_track_id(u) == "1333012320"
    assert music.apple_track_id("https://music.apple.com/ru/song/name/999") == "999"
    assert not music.is_apple_music("https://youtube.com/watch?v=x")


def test_lrc_to_cues():
    lrc = "[ar:Кино]\n[00:10.00]Первая строка\n[00:14.50]Вторая строка\n[00:20.00]\n"
    cues = music.lrc_to_cues(lrc, total=30)
    assert [c[2] for c in cues] == ["Первая строка", "Вторая строка"]
    assert cues[0][0] == 10.0
    assert cues[0][1] == 14.5            # ends where the next line starts


def test_plain_to_cues_spreads_evenly():
    cues = music.plain_to_cues("a\nb\nc\nd", total=40)
    assert len(cues) == 4
    assert cues[0][0] == 0.0
    assert cues[-1][1] == 40.0


def test_lrc_to_cues_compresses_when_overshooting_duration():
    # LRC timed as if the track were ~250s; the audio is 200s
    lrc = "\n".join(f"[{i // 60:02d}:{i % 60:02d}.00]line {i}" for i in range(2, 252, 10))
    cues = music.lrc_to_cues(lrc, total=200)
    assert cues[0][0] < 5                      # intro lead-in kept
    assert cues[-1][0] < 200                   # last line now inside the track
    # a well-fitting LRC is left untouched
    good = music.lrc_to_cues("[00:02.00]a\n[01:00.00]b\n[02:00.00]c", total=200)
    assert good[-1][0] == 120.0


def test_pick_youtube_prefers_official_and_duration():
    results = [
        {"id": "cover", "title": "Кукушка (acoustic cover)", "channel": "SomeGuy", "duration": 240, "url": "u1"},
        {"id": "official", "title": "Кукушка", "channel": "Группа КИНО", "duration": 242, "url": "u2"},
        {"id": "wrong", "title": "Кукушка remix", "channel": "DJ", "duration": 500, "url": "u3"},
    ]
    assert music.pick_youtube(results, "Кино", "Кукушка", 240)["id"] == "official"


def test_pick_youtube_rejects_when_nothing_close():
    results = [{"id": "x", "title": "y", "channel": "z", "duration": 999, "url": "u"}]
    assert music.pick_youtube(results, "A", "B", 200) is None


# ---------------------------------------------------------------- db.py

def test_norm_and_lemma():
    assert ru_db.norm("  Ёлка ") == "елка"
    assert ru_db.lemma_key("иголок") == ru_db.lemma_key("иголка")
    assert ru_db.lemma_key("блефуешь") == "блефовать"


def test_bold_marks_inflected_forms():
    out = ru_db.bold("Он блефовал за столом.", "блефовать", False, "**")
    assert "**блефовал**" in out
    out2 = ru_db.bold("Достал иголок из подушки.", "иголка", False, "**")
    assert "**иголок**" in out2


def test_bold_phrase():
    out = ru_db.bold("Он сошёл с ума от радости.", "сойти с ума", True, "**")
    assert out.count("**") == 2


# ---------------------------------------------------------------- llm._parse_obj

def test_parse_obj_clean():
    assert llm._parse_obj('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}


def test_parse_obj_prose_and_fence():
    assert llm._parse_obj('Here you go:\n```json\n{"x": true}\n```') == {"x": True}


def test_parse_obj_trailing_comma():
    assert llm._parse_obj('{"a": [1, 2,], "b": {"c": 3},}') == {"a": [1, 2], "b": {"c": 3}}


def test_parse_obj_missing_comma_between_elements():
    # the failure seen in the speaking drill: no comma between two array objects
    bad = ('{ "reformulations": [ {"text": "раз", "register": "neutral"}\n'
           '  {"text": "два", "register": "formal"} ], "meaning": "ok" }')
    got = llm._parse_obj(bad)
    assert [r["text"] for r in got["reformulations"]] == ["раз", "два"]


def test_parse_obj_stops_at_first_object():
    assert llm._parse_obj('{"a": 1}\n{"b": 2}') == {"a": 1}


def test_speak_levels_present():
    assert set(llm.SPEAK_LEVELS) == {"a2", "a2plus", "b1", "b2", "c1"}


def test_speak_scene_seed_varies():
    import random
    seeds = {llm.speak_scene_seed(random.Random(i)) for i in range(60)}
    assert len(seeds) > 40                       # combinatorial, not a fixed rotation
    assert any("surprise me" in s for s in seeds)
    assert any(s.startswith("moment:") for s in seeds)   # place sometimes omitted


def test_speak_drops_punctuation_only_corrections():
    import speak
    fb = {
        "corrections": [
            {"original": "я думаю что", "corrected": "я думаю, что",   # punctuation only
             "severity": "style", "category": "other"},
            {"original": "делал", "corrected": "сделал",               # real fix
             "severity": "hard", "category": "aspect"},
        ],
        "diff": [{"c": 1}, {"s": " весь день "}, {"c": 2}, {"s": " всё."}],
    }
    speak._drop_punct_only(fb)
    assert len(fb["corrections"]) == 1
    assert fb["corrections"][0]["corrected"] == "сделал"
    # the dropped correction's text is folded back into a verbatim run; the real
    # one is renumbered to index 1
    rebuilt = "".join(d.get("s", "") if "s" in d else "делал" for d in fb["diff"])
    assert rebuilt == "я думаю что весь день делал всё."
    assert [d for d in fb["diff"] if "c" in d] == [{"c": 1}]


# ---------------------------------------------------------------- lrcfix.py

def test_lrcfix_deltas_and_slope_flat_offset():
    # lyrics run a constant 2s early vs the transcript; each line has its own words
    words = ["солнце ветер море берег", "город дождь асфальт неон",
             "поезд рельсы север утро", "костёр дорога звёзды дым"]
    lyrics = [(t, lrcfix._toks(w)) for t, w in zip((10, 20, 30, 40), words)]
    segs = [(t + 2.0, lrcfix._toks(w)) for t, w in zip((10, 20, 30, 40), words)]
    m = lrcfix._deltas(lyrics, segs)
    assert len(m) == 4
    assert all(abs(d - 2.0) < 0.01 for _t, _s, d in m)
    assert abs(lrcfix._slope([t for t, _, _ in m], [d for _, _, d in m])) < 1e-6


def test_lrcfix_slope_detects_drift():
    xs = [0, 30, 60, 90]
    ys = [0.0, 2.0, 4.0, 6.0]        # +2s per 30s
    assert lrcfix._slope(xs, ys) > 0.06


# ---------------------------------------------------------------- tts.py

def test_tts_key_is_stable_and_text_insensitive_to_whitespace():
    assert tts.path_for("привет   мир") == tts.path_for(" привет мир ")
    assert tts.path_for("a") != tts.path_for("b")
    assert tts.path_for("x").endswith(".m4a")
