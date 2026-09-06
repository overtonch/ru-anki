"""The dictionary stress pass (app/accent.mark_text). The LLM correction pass is
stubbed in conftest, so accent_paragraphs here == dictionary-only."""
import accent


def test_marks_inflected_forms_and_leaves_monosyllables():
    marked, uncertain = accent.mark_text(
        "Она пишет письмо родителям, а он читал книгу вчера")
    assert "Она́" in marked and "пи́шет" in marked and "письмо́" in marked
    assert "чита́л" in marked and "кни́гу" in marked and "вчера́" in marked
    assert "он́" not in marked          # one-syllable word untouched


def test_every_multisyllable_word_gets_exactly_one_mark():
    marked, _ = accent.mark_text(
        "Максим медленно шёл по широкой шумной улице большого города")
    for w in marked.split():
        syl = sum(1 for ch in w if ch in accent._VOWELS)
        if syl >= 2 and "ё" not in w.lower():
            assert w.count("́") == 1, f"{w!r} has {w.count(chr(0x301))} marks"


def test_flags_homographs_as_uncertain():
    _, uncertain = accent.mark_text("Замок на двери был старый")
    assert any(u["reason"] == "homograph" for u in uncertain)


def test_strip_removes_marks():
    assert accent.strip("письмо́ роди́телям") == "письмо родителям"


def test_paradigm_flags_mobile_stress_only():
    голова = accent.paradigm("голова")
    assert голова and голова["pattern"] == "mobile"
    forms = {f["form"] for f in голова["forms"]}
    assert "голова́" in forms and "го́лову" in forms      # stress on ending vs stem
    assert accent.paradigm("работа") is None             # fixed stress — nothing to show
    писать = accent.paradigm("писать")
    assert писать and any("пи́шет" in f["form"] for f in писать["forms"])


def test_accent_paragraphs_keeps_structure(stub_llm):
    out = accent.accent_paragraphs(["Первый абзац о городе.", "Второй абзац о работе."])
    assert len(out) == 2 and "́" in out[0]
