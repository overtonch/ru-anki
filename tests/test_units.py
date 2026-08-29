"""Pure-function tests — no DB, no network, no LLM."""
import subs
import music
import db as ru_db
import tts


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


# ---------------------------------------------------------------- tts.py

def test_tts_key_is_stable_and_text_insensitive_to_whitespace():
    assert tts.path_for("привет   мир") == tts.path_for(" привет мир ")
    assert tts.path_for("a") != tts.path_for("b")
    assert tts.path_for("x").endswith(".m4a")
